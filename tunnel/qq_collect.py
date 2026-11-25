"""Pull the local, durable QQ receiver into the shared ledger. No QQ write APIs.

The source DB is opened mode=ro. Only approved Tencent CDN GETs download media.
Source cursor and media retry jobs live in the tunnel's private SQLite state.
"""
import asyncio
import fcntl
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import tempfile
from urllib.parse import urlsplit, urlunsplit, urljoin

import httpx
from api import DB, canonical, connection, digest, now
from media import archive_file, MAX_BYTES
from sanitize import clean
from store import ingest, state_get, state_set, update_status, refresh_daily

SOURCE = 'qq'
CONFIG = Path(os.environ.get('MUSE_QQ_CONFIG', str(DB.parent / 'qq-source.json')))
PAGE_SIZE = 200
RETRY_SECONDS = 300
SECRET_KEYS = {'rkey', 'skey', 'pskey', 'p_skey', 'authkey', 'accesskey', 'session_key'}


def clean_cq(value):
    def replace(match):
        parts = []
        for param in match[2].lstrip(',').split(','):
            key, separator, encoded = param.partition('=')
            if not separator: continue
            decoded = encoded.replace('&#91;', '[').replace('&#93;', ']').replace('&#44;', ',').replace('&amp;', '&')
            safe = public(decoded, key.lower())
            safe = str(safe).replace('&', '&amp;').replace('[', '&#91;').replace(']', '&#93;').replace(',', '&#44;')
            if key.lower() not in SECRET_KEYS: parts.append(key + '=' + safe)
        return '[CQ:' + match[1] + (',' + ','.join(parts) if parts else '') + ']'
    return re.sub(r'\[CQ:([\w-]+)([^\]]*)\]', replace, value)


def public(value, field=None):
    """Keep content, but never expose signed transport URLs or auth fields."""
    value = clean(value)
    if isinstance(value, dict):
        return {k: public(v, str(k).lower()) for k, v in value.items() if str(k).lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [public(v, field) for v in value]
    if isinstance(value, str):
        if field == 'raw_message':
            return clean_cq(value)
        if field in ('url', 'file', 'download_url', 'downloadurl', 'image_url', 'thumb_url') and value.startswith(('http://', 'https://')):
            try:
                p = urlsplit(value)
                path = re.sub(r'(?i)(/ftn_handler/)[^/]+', r'\1[redacted]', p.path)
                return urlunsplit((p.scheme, p.hostname or '', path, '', ''))
            except ValueError:
                return '[invalid_transport_url]'
        if field not in ('text', 'raw_message') and value[:1] in ('{', '['):
            try:
                return canonical(public(json.loads(value)))
            except (ValueError, TypeError):
                pass
    return value


def stamp(value):
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def load_config():
    if not CONFIG.is_file():
        raise RuntimeError('qq_not_configured')
    if CONFIG.stat().st_mode & 0o077:
        raise RuntimeError('qq_config_permissions')
    c = json.loads(CONFIG.read_text())
    c['account'] = int(c['account'])
    c['groups'] = {int(g): str(name) for g, name in c['groups'].items()}
    if c['account'] <= 0 or not c['groups'] or any(g <= 0 for g in c['groups']):
        raise RuntimeError('qq_invalid_allowlist')
    c['db'] = Path(c['db'])
    if not c['db'].is_absolute() or c['db'].is_symlink():
        raise RuntimeError('qq_invalid_db_path')
    # No user-provided endpoint or credential is accepted here.
    c['status_url'] = 'http://127.0.0.1:18081/status'
    return c


def init_jobs():
    with connection() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS qq_retry(
          digest TEXT PRIMARY KEY, source_rowid INTEGER NOT NULL, attempts INTEGER NOT NULL,
          retry_after TEXT NOT NULL, error TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS qq_records(
          source_id TEXT PRIMARY KEY, digest TEXT NOT NULL, source_rowid INTEGER NOT NULL)''')
        c.execute('CREATE TABLE IF NOT EXISTS qq_processed(digest TEXT PRIMARY KEY)')


def source_db(config):
    if not config['db'].is_file():
        raise RuntimeError('qq_source_db_missing')
    c = sqlite3.connect(config['db'].as_uri() + '?mode=ro', uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    return c


def resolve(c, account, group, message_id):
    rows = c.execute('''SELECT seq FROM provider_ids WHERE account=? AND group_id=? AND message_id=?
                       GROUP BY seq LIMIT 2''', (account, group, str(message_id))).fetchall()
    return int(rows[0][0]) if len(rows) == 1 else None


def identity(account, group, seq):
    return f'{account}:{group}:{seq}'


def normalize(row, c, config):
    raw = json.loads(row['payload'])
    account, group = int(row['account']), int(row['group_id'])
    if account != config['account'] or group not in config['groups']:
        return None
    message = raw.get('post_type') in ('message', 'message_sent') or (
        row['source'] == 'history' and raw.get('message_type') == 'group')
    seq = int(raw.get('message_seq') or 0) if message else 0
    if message and seq <= 0:
        raise RuntimeError('qq_message_sequence_missing')
    notice = raw.get('notice_type') or raw.get('post_type') or 'unknown'
    original_id = str(raw.get('message_id') or '')
    notice_seq = int(raw.get('message_seq') or 0) if notice == 'group_recall' else 0
    target_seq = seq or notice_seq or (resolve(c, account, group, original_id) if original_id else None)
    source_id = identity(account, group, seq) if message else f'{account}:{group}:notice:{row["digest"]}'
    segments = raw.get('message', [])
    if not isinstance(segments, list):
        segments = [{'type': 'text', 'data': {'text': str(segments)}}]
    # Unify only documented history/live envelope differences, retain all content.
    canonical_raw = dict(raw)
    if message:
        canonical_raw.update(post_type='message', self_id=account)
        canonical_raw.pop('real_id', None)
    reply = []
    cards = []
    for segment in segments:
        data = segment.get('data') or {}
        if segment.get('type') == 'reply':
            reply_seq = int(data.get('seq') or data.get('message_seq') or 0) or resolve(c, account, group, data.get('id'))
            reply.append({'original_message_id': str(data.get('id') or ''),
                          'source_id': identity(account, group, reply_seq) if reply_seq else None,
                          'resolution': 'resolved' if reply_seq else 'unresolved'})
        if segment.get('type') in ('json', 'xml', 'ark', 'lightapp', 'forward', 'node'):
            body = data.get('data', data)
            if isinstance(body, str):
                try: body = json.loads(body)
                except ValueError: pass
            cards.append({'type': segment['type'], 'data': public(body)})
    payload = {'source': SOURCE, 'kind': 'message' if message else 'message_recall' if notice == 'group_recall' else 'notice',
        'source_id': source_id, 'original_message_id': original_id,
        'conversation_id': str(group), 'conversation': config['groups'][group], 'account_id': str(account),
        'message_sequence': seq or None, 'sender_id': str(raw.get('user_id') or raw.get('sender_id') or ''),
        'sender': public(raw.get('sender') or {'user_id': raw.get('user_id') or raw.get('sender_id')}),
        'sent_at': stamp(raw.get('time')), 'text': ''.join(str((s.get('data') or {}).get('text', ''))
            for s in segments if s.get('type') == 'text'),
        'segments': public(segments), 'reply_to': reply, 'cards': cards,
        'raw': public(canonical_raw), 'synthetic': False}
    if notice == 'group_recall':
        payload.update(recalled=True, target_source_id=identity(account, group, target_seq) if target_seq else None,
                       target_original_message_id=original_id, relation_status='resolved' if target_seq else 'unresolved')
    return source_id, payload, raw


def media_refs(raw):
    result = []
    def visit(value):
        if isinstance(value, list):
            for item in value: visit(item)
        elif isinstance(value, dict):
            if value.get('type') in ('image', 'file', 'record', 'video'):
                result.append({'type': value['type'], 'data': value.get('data') or {}})
            elif value.get('type') in ('node', 'forward'):
                data = value.get('data') or {}
                for key in ('content', 'message', 'messages'): visit(data.get(key))
            elif 'message' in value:
                visit(value['message'])
    visit(raw.get('message'))
    if raw.get('notice_type') == 'group_upload' and isinstance(raw.get('file'), dict):
        result.append({'type': 'file', 'data': raw['file']})
    return result


async def trusted_url(url):
    p = urlsplit(url)
    host = (p.hostname or '').lower()
    allowed = (host in {'multimedia.nt.qq.com', 'multimedia.nt.qq.com.cn', 'grouptalk.c2c.qq.com'}
               or host.endswith('.qpic.cn') or host.endswith('.ftn.qq.com'))
    if p.scheme not in ('http', 'https') or not allowed or p.username or p.password or p.port not in (None, 80, 443):
        raise RuntimeError('qq_untrusted_media_url')
    # Only known QQ HTTP media links are upgraded; credentials are never added.
    url = urlunsplit(('https', host, p.path, p.query, ''))
    addresses = await asyncio.to_thread(socket.getaddrinfo, host, 443, 0, socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise RuntimeError('qq_nonpublic_media_address')
    return url


async def download(client, url, name):
    root = (DB.parent / 'qq-downloads').resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = re.sub(r'[^\w. ()-]', '_', Path(name).name)[:180] or 'qq-attachment.bin'
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        path = Path(tmp) / name
        for _ in range(6):
            url = await trusted_url(url)
            async with client.stream('GET', url, follow_redirects=False) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers['location'])
                    continue
                r.raise_for_status()
                if 'text/html' in r.headers.get('content-type', ''):
                    raise RuntimeError('qq_media_returned_html')
                if int(r.headers.get('content-length') or 0) > MAX_BYTES:
                    raise RuntimeError('qq_media_too_large')
                size = 0
                with path.open('wb') as f:
                    path.chmod(0o600)
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise RuntimeError('qq_media_too_large')
                        f.write(chunk)
                asset = archive_file(str(path), [root])
                if not asset or asset['status'] != 'available':
                    raise RuntimeError('qq_media_archive_failed')
                return asset
        raise RuntimeError('qq_media_redirect_limit')


def error_code(exc):
    if isinstance(exc, RuntimeError) and re.fullmatch(r'qq_[a-z_]+', str(exc)):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f'qq_media_http_{exc.response.status_code}'
    return 'qq_' + type(exc).__name__


async def archive_attachments(raw, row, client):
    attachments = []
    for index, ref in enumerate(media_refs(raw)):
        data = ref['data']
        key = f'media:{row["digest"]}:{index}'
        file_id = data.get('file_id') or data.get('id') if ref['type'] == 'file' else None
        shared_key = f'file:{row["account"]}:{row["group_id"]}:{file_id}' if file_id else None
        existing = state_get(SOURCE, key) or (state_get(SOURCE, shared_key) if shared_key else None)
        cached = json.loads(existing) if existing else None
        if cached and cached.get('status') == 'available' and (DB.parent / 'media' / cached['asset_id']).is_file():
            # Renew the immutable blob lease through the same archive mechanism.
            renewed = archive_file(str((DB.parent / 'media' / cached['asset_id']).resolve()), [(DB.parent / 'media').resolve()])
            cached = {**renewed, 'filename': cached['filename']}
            attachments.append({'type': ref['type'], 'original_metadata': public(data), **cached})
            continue
        fallback = data.get('file') if not str(data.get('file', '')).startswith(('http://', 'https://')) else None
        name = str(data.get('name') or data.get('file_name') or fallback or f'{ref["type"]}.bin')
        url = data.get('url') or (data.get('file') if str(data.get('file', '')).startswith(('http://', 'https://')) else None)
        try:
            if not url: raise RuntimeError('qq_original_media_url_missing')
            asset = await download(client, url, name)
            state_set(SOURCE, key, canonical(asset))
            if shared_key: state_set(SOURCE, shared_key, canonical(asset))
        except Exception as exc:
            asset = {'status': 'unavailable', 'error': error_code(exc), 'original_available': False}
        attachments.append({'type': ref['type'], 'original_metadata': public(data), **asset})
    return attachments


async def process(row, src, config, client, reprocess=False):
    with connection() as c:
        if not reprocess and c.execute('SELECT 1 FROM qq_processed WHERE digest=?', (row['digest'],)).fetchone():
            c.execute('UPDATE qq_records SET source_rowid=? WHERE digest=?', (row['source_rowid'], row['digest']))
            return 0
    item = normalize(row, src, config)
    if item is None: return 0
    source_id, payload, raw = item
    # Do not resurrect a consumer-approved retired unchanged source record.
    with connection() as c:
        latest = c.execute('SELECT source_rowid FROM qq_records WHERE source_id=?', (source_id,)).fetchone()
        if latest and latest[0] > row['source_rowid']:
            c.execute('DELETE FROM qq_retry WHERE digest=?', (row['digest'],))
            return 0
        retired = c.execute('''SELECT r.raw_hash FROM events e JOIN retention r ON r.seq=e.seq
            WHERE e.source=? AND e.source_id=? AND r.state IN ('quarantined','purged')
            AND e.seq=(SELECT max(v.seq) FROM events v WHERE v.source=e.source AND v.source_id=e.source_id)
            ORDER BY e.revision DESC LIMIT 1''', (SOURCE, source_id)).fetchone()
    if retired and retired['raw_hash'] == digest(payload['raw']): return 0
    payload['attachments'] = await archive_attachments(raw, row, client)
    failed = [a['error'] for a in payload['attachments'] if a['status'] != 'available']
    payload['attachment_status'] = 'incomplete' if failed else 'available' if payload['attachments'] else 'none_detected'
    inserted = ingest(SOURCE, source_id, payload, payload['sent_at'])
    with connection() as c:
        previous_rows = c.execute('SELECT digest FROM qq_records WHERE source_id=?', (source_id,)).fetchall()
        for previous_row in previous_rows:
            if previous_row[0] != row['digest']:
                c.execute('DELETE FROM qq_retry WHERE digest=?', (previous_row[0],))
        c.execute('INSERT OR REPLACE INTO qq_records VALUES (?,?,?)', (source_id, row['digest'], row['source_rowid']))
        c.execute('INSERT OR IGNORE INTO qq_processed VALUES (?)', (row['digest'],))
        if failed:
            previous = c.execute('SELECT attempts FROM qq_retry WHERE digest=?', (row['digest'],)).fetchone()
            c.execute('INSERT OR REPLACE INTO qq_retry VALUES (?,?,?,?,?)', (row['digest'], row['source_rowid'],
                (previous[0] if previous else 0) + 1,
                (datetime.now(timezone.utc) + timedelta(seconds=RETRY_SECONDS)).isoformat(), canonical(failed)))
        else: c.execute('DELETE FROM qq_retry WHERE digest=?', (row['digest'],))
    return int(inserted)


async def collect(config=None, client=None, source_status=None):
    lock_path = DB.parent / 'qq-import.lock'
    with lock_path.open('a') as lock:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0, 0
        return await _collect(config, client, source_status)


async def _collect(config=None, client=None, source_status=None):
    """Source-owned ingestion; consumer polling/receipt/daily code is unchanged."""
    init_jobs()
    owned = client is None
    if owned: client = httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
    src = None
    try:
        config = config or load_config()
        src = source_db(config)
        account = config['account']
        cursor = int(state_get(SOURCE, 'raw_cursor') or 0)
        upper = src.execute('SELECT coalesce(max(rowid),0) FROM raw_events').fetchone()[0]
        anchor = src.execute('SELECT digest FROM raw_events WHERE rowid=?', (cursor,)).fetchone()
        saved_anchor = state_get(SOURCE, 'raw_anchor')
        reset = cursor > upper or bool(cursor and saved_anchor and (not anchor or anchor[0] != saved_anchor))
        if reset:
            cursor = 0
            with connection() as c: c.execute('UPDATE qq_records SET source_rowid=0')
        observed = inserted = 0
        with connection() as c:
            due = c.execute('SELECT digest FROM qq_retry WHERE retry_after<=? ORDER BY retry_after LIMIT 100', (now(),)).fetchall()
        for job in due:
            row = src.execute('SELECT rowid source_rowid,* FROM raw_events WHERE digest=?', (job['digest'],)).fetchone()
            if row is not None: inserted += await process(row, src, config, client, reprocess=True)
            else:
                with connection() as c:
                    c.execute('UPDATE qq_retry SET error=?,retry_after=? WHERE digest=?',
                        (canonical(['qq_retry_source_missing']),
                         (datetime.now(timezone.utc) + timedelta(seconds=RETRY_SECONDS)).isoformat(), job['digest']))
        # Freeze source upper bound; checkpoint only after durable ingest and retry enqueue.
        while cursor < upper:
            rows = src.execute('SELECT rowid source_rowid,* FROM raw_events WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?',
                               (cursor, upper, PAGE_SIZE)).fetchall()
            if not rows: raise RuntimeError('qq_source_cursor_gap')
            for row in rows:
                inserted += await process(row, src, config, client)
                cursor = row['source_rowid']; observed += 1
                with connection() as c:
                    c.executemany('INSERT OR REPLACE INTO source_state VALUES (?,?,?)',
                        [(SOURCE, 'raw_cursor', str(cursor)), (SOURCE, 'raw_anchor', row['digest'])])
        # Resolve recall/reply links whose target arrived later; only re-ingest if changed.
        with connection() as c:
            unresolved = c.execute('''SELECT e.source_id,m.digest FROM events e
                JOIN qq_records m ON m.source_id=e.source_id WHERE e.source='qq'
                AND e.seq=(SELECT max(v.seq) FROM events v WHERE v.source=e.source AND v.source_id=e.source_id)
                AND e.payload!='null' AND (json_extract(e.payload,'$.relation_status')='unresolved'
                OR EXISTS(SELECT 1 FROM json_each(e.payload,'$.reply_to') WHERE json_extract(value,'$.resolution')='unresolved'))
                LIMIT 100''').fetchall()
        if unresolved:
            for pending in unresolved:
                row = src.execute('SELECT rowid source_rowid,* FROM raw_events WHERE digest=?', (pending['digest'],)).fetchone()
                if row is not None: inserted += await process(row, src, config, client, reprocess=True)
        coverage = {}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        for group in config['groups']:
            row = src.execute('''SELECT count(*),min(sent_at),max(sent_at),min(seq),max(seq)
                FROM messages WHERE account=? AND group_id=?''', (account, group)).fetchone()
            state = src.execute('SELECT state FROM sync_state WHERE account=? AND group_id=?', (account, group)).fetchone()
            coverage[str(group)] = {'observed_messages': row[0], 'oldest_sent_at': stamp(row[1]),
                'newest_sent_at': stamp(row[2]), 'min_sequence': row[3], 'max_sequence': row[4],
                'checkpoint': json.loads(state[0]) if state else None, 'requested_since': cutoff}
            checkpoint = coverage[str(group)]['checkpoint'] or {}
            coverage[str(group)]['pre_join_history'] = checkpoint.get('pre_join_history', 'unknown')
            coverage[str(group)]['seven_day_coverage'] = ('unavailable_before_join'
                if checkpoint.get('pre_join_history') == 'unavailable' else
                'observed_to_cutoff' if stamp(row[1]) and stamp(row[1]) <= cutoff else 'not_established')
        if source_status is None:
            try:
                response = await client.get(config['status_url'])
                if response.status_code not in (200, 503): raise RuntimeError('qq_status_http_error')
                source_status = response.json()
            except Exception as exc:
                source_status = {'healthy': False, 'qq_online': False, 'error': error_code(exc)}
        if source_status.get('account') not in (None, account): raise RuntimeError('qq_status_account_mismatch')
        with connection() as c:
            failures = c.execute('SELECT count(*) FROM qq_retry').fetchone()[0]
            errors = [dict(r) for r in c.execute('SELECT error,count(*) count FROM qq_retry GROUP BY error')]
        connected = bool(source_status.get('qq_online') and source_status.get('onebot_connected'))
        healthy = bool(source_status.get('healthy')) and not failures
        details = {'stale_after_seconds': 300, 'observed_raw_rows': observed, 'new_versions': inserted,
            'cursor': cursor, 'source_upper_rowid': upper, 'source_cursor_reset': reset,
            'last_successful_import_at': now(), 'history': coverage, 'requested_history_days': 7,
            'attachment_failures': failures, 'attachment_errors': errors,
            'receiver': public(source_status), 'components': {
                'cache': {'available': True, 'reason': 'durable_source_read_ok'},
                'transport': {'available': connected, 'reason': 'qq_online' if connected else 'qq_offline'},
                'history': {'available': bool(source_status.get('healthy')),
                    'reason': ('visible_since_join' if any(h['pre_join_history'] == 'unavailable' for h in coverage.values())
                               else 'visible_history_checked') if source_status.get('healthy') else 'upstream_history_incomplete'},
                'media': {'available': not failures, 'reason': 'archived' if not failures else 'attachments_pending',
                          'failed_events': failures, 'errors': errors}},
            'edits': 'revisions_when_observed', 'recalls': 'source_notices_with_original_id_and_resolved_sequence',
            'scope': {'account_id': str(account), 'groups': list(map(str, config['groups']))}}
        update_status(SOURCE, 'available' if healthy else 'degraded', healthy,
                      'sync_ok' if healthy else 'qq_history_or_attachment_incomplete' if connected else 'qq_upstream_unavailable', details)
        state_set(SOURCE, 'last_success', details['last_successful_import_at'])
        state_set(SOURCE, 'last_details', canonical(details))
        return observed, inserted
    except Exception as exc:
        previous = json.loads(state_get(SOURCE, 'last_details') or '{}')
        update_status(SOURCE, 'unavailable', False, error_code(exc),
            {**previous, 'stale_after_seconds': 300, 'failure_stage': 'qq_import', 'cursor': state_get(SOURCE, 'raw_cursor'),
             'components': {**previous.get('components', {}), 'cache': {'available': False, 'reason': error_code(exc)}},
             'last_successful_import_at': state_get(SOURCE, 'last_success'), 'requested_history_days': 7})
        return None, None
    finally:
        if src is not None: src.close()
        if owned: await client.aclose()


async def main():
    from api import init
    os.umask(0o077); init()
    result = await collect(); refresh_daily()
    print(json.dumps({'qq_counts': result, 'checked_at': now()}))


if __name__ == '__main__': asyncio.run(main())
