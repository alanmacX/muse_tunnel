"""Isolated real-receiver-schema checks. Never connects to or sends on QQ."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch


async def run(root):
    import httpx
    import api, store, qq_collect as qq
    from fastapi.testclient import TestClient
    receiver = Path(os.environ.get('QQ_RECEIVER_ROOT', '/home/ubuntu/qq-course-listener'))
    if not receiver.exists():
        receiver = Path(__file__).resolve().parents[2] / '67bot/qq-laya-bot'
    sys.path.insert(0, str(receiver))
    from src.store import Store
    api.init()
    source = Store(root / 'source.db')
    config = {'account': 456, 'groups': {123: 'Test course'}, 'db': source.path,
              'status_url': 'http://127.0.0.1:18081/status'}
    status = {'account': 456, 'healthy': True, 'qq_online': True, 'onebot_connected': True}
    readonly = qq.source_db(config)
    try: readonly.execute('DELETE FROM raw_events')
    except sqlite3.OperationalError: pass
    else: raise AssertionError('source was writable')
    readonly.close()
    timestamp = int(time.time())
    image = b'\x89PNG\r\n\x1a\n' + b'original-image-fixture'
    document = b'%PDF-1.4\noriginal-document-fixture\n%%EOF'
    requests = []
    fail_image = False

    def handler(request):
        requests.append(request)
        assert request.method == 'GET'
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        if request.url.path == '/retry' and fail_image:
            return httpx.Response(503)
        if request.url.path == '/redirect':
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        if request.url.path == '/large':
            return httpx.Response(200, headers={'content-length': str(qq.MAX_BYTES + 1)})
        if request.url.path == '/html':
            return httpx.Response(200, headers={'content-type': 'text/html'}, content=b'login')
        return httpx.Response(200, content=document if request.url.path.startswith('/ftn_handler/') else image)

    def event(seq, text=None, segments=None):
        segments = segments if segments is not None else [{'type': 'text', 'data': {'text': text or f'text-{seq}'}}]
        return {'post_type': 'message', 'self_id': 456, 'message_type': 'group', 'sub_type': 'normal',
                'group_id': 123, 'user_id': 789, 'time': timestamp + seq,
                'message_seq': seq, 'message_id': -1000 - seq, 'sender': {'user_id': 789, 'nickname': 'Teacher'},
                'message': segments, 'raw_message': ''.join(
                    s['data']['text'] if s['type'] == 'text' else '[CQ:' + s['type'] + ',url=' +
                    str(s['data'].get('url', '')).replace('&', '&amp;') + ']' for s in segments)}

    def add(message, origin='live'):
        source.ingest(message, origin, 456, frozenset({123}), 'snowluma')

    def rows(source_id=None):
        with api.connection() as c:
            return [dict(r) for r in c.execute('SELECT * FROM events WHERE source=\'qq\'' +
                (' AND source_id=?' if source_id else '') + ' ORDER BY seq', (source_id,) if source_id else ())]

    def payload(seq):
        return json.loads(rows(f'456:123:{seq}')[-1]['payload'])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        async def collect(s=status):
            return await qq.collect(config, client, s)

        # Only DNS is mocked; allowlist, redirects, stream limits and archive code are real.
        dns = [(2, 1, 6, '', ('1.1.1.1', 443))]
        with patch.object(qq.socket, 'getaddrinfo', return_value=dns):
            add(event(1, 'https://example.edu/course?q=complete&chapter=2'))
            for seq in range(2, 8): add(event(seq))
            add(event(8, segments=[{'type': 'reply', 'data': {'id': -1001}},
                {'type': 'json', 'data': {'data': json.dumps({'app': 'course', 'meta': {'title': '全部保留'}})}},
                {'type': 'text', 'data': {'text': '{"keep":"exact spelling"}'}}]))
            add(event(9, segments=[{'type': 'image', 'data': {
                'file': 'lesson.png', 'url': 'https://multimedia.nt.qq.com.cn/image?rkey=fixture-private-secret', 'size': len(image)}}]))
            add(event(10, segments=[{'type': 'file', 'data': {
                'name': 'lesson.pdf', 'file_id': 'lesson-file',
                'url': 'https://szfile.ftn.qq.com/ftn_handler/ABCDEF0123456789/?fname=document&authkey=fixture-private-secret'}}]))
            assert await collect() == (10, 10)
            assert payload(1)['text'].endswith('?q=complete&chapter=2')
            assert payload(8)['text'] == '{"keep":"exact spelling"}'
            assert payload(8)['reply_to'][0]['source_id'] == '456:123:1'
            assert payload(8)['cards'][0]['data']['meta']['title'] == '全部保留'
            assert payload(9)['attachments'][0]['sha256'] == hashlib.sha256(image).hexdigest()
            assert payload(10)['attachments'][0]['sha256'] == hashlib.sha256(document).hexdigest()
            assert 'fixture-private-secret' not in json.dumps(rows())
            assert 'ABCDEF0123456789' not in json.dumps(rows())
            assert not any('priority' in json.loads(r['payload']) for r in rows())

            # Provider raw dedup plus history/live logical dedup; cached media stays identical.
            add(event(1, 'https://example.edu/course?q=complete&chapter=2'))
            history = deepcopy(event(9, segments=[{'type': 'image', 'data': {
                'file': 'lesson.png', 'url': 'https://multimedia.nt.qq.com.cn/image?rkey=fixture-private-secret', 'size': len(image)}}]))
            history.pop('post_type'); history.pop('self_id'); history['real_id'] = history['message_id']
            add(history, 'history')
            assert await collect() == (1, 0)
            assert len(rows('456:123:9')) == 1
            assert await collect() == (0, 0)
            assert len(rows('availability')) == 1

            # Offline import leaves the durable cursor; receiver continues persisting a burst.
            config['db'] = root / 'temporarily-unavailable.db'
            assert await collect() == (None, None)
            for seq in range(11, 216): add(event(seq))
            config['db'] = source.path
            assert await collect() == (205, 205)  # spans the 200-row source page
            assert payload(215)['text'] == 'text-215'
            assert {r['source_id'] for r in rows() if json.loads(r['payload'])['kind'] == 'message'} == {
                f'456:123:{seq}' for seq in range(1, 216)}

            # Observed edit creates a revision; recall keeps both original and stable relation.
            add(event(2, 'changed text'))
            add({'post_type': 'notice', 'notice_type': 'group_recall', 'self_id': 456,
                 'group_id': 123, 'sender_id': 789, 'operator_id': 789, 'message_id': -1002,
                 'message_seq': 2, 'time': timestamp + 300})
            await collect()
            assert len(rows('456:123:2')) == 2 and payload(2)['text'] == 'changed text'
            recall = [json.loads(r['payload']) for r in rows() if json.loads(r['payload'])['kind'] == 'message_recall'][-1]
            assert recall['target_source_id'] == '456:123:2'
            assert recall['sender_id'] == '789'

            # Retry is persisted across collector instances and supersedes the failed revision.
            fail_image = True
            add(event(216, segments=[{'type': 'image', 'data': {'file': 'retry.png', 'url': 'https://multimedia.nt.qq.com.cn/retry'}}]))
            await collect()
            assert payload(216)['attachment_status'] == 'incomplete'
            with api.connection() as c:
                assert c.execute('SELECT count(*) FROM qq_retry').fetchone()[0] == 1
                c.execute("UPDATE qq_retry SET retry_after='2000-01-01T00:00:00+00:00'")
            fail_image = False
            assert await collect() == (0, 1)
            assert payload(216)['attachment_status'] == 'available'
            with api.connection() as c: assert c.execute('SELECT count(*) FROM qq_retry').fetchone()[0] == 0

            add(event(217, segments=[{'type': 'reply', 'data': {'id': -1218}}]))
            await collect()
            assert payload(217)['reply_to'][0]['resolution'] == 'unresolved'
            assert await collect() == (0, 0)
            add(event(218))
            await collect()
            assert payload(217)['reply_to'][0]['source_id'] == '456:123:218'
            assert await collect() == (0, 0)

            add({'post_type': 'notice', 'notice_type': 'group_upload', 'group_id': 123, 'user_id': 789,
                 'time': timestamp, 'file': {'id': 'lesson-file', 'name': 'lesson.pdf', 'size': len(document)}})
            assert await collect() == (1, 1)
            assert json.loads(rows()[-1]['payload'])['attachments'][0]['sha256'] == hashlib.sha256(document).hexdigest()

            # Full checkpoint replay never re-emits already processed older edits.
            before = len(rows())
            store.state_set('qq', 'raw_cursor', '0')
            await collect()
            assert len(rows()) == before and payload(2)['text'] == 'changed text'

            for suffix in ('redirect', 'large', 'html'):
                try: await qq.download(client, 'https://multimedia.nt.qq.com.cn/' + suffix, 'bad.bin')
                except RuntimeError: pass
                else: raise AssertionError('unsafe download accepted: ' + suffix)
            try: await qq.download(client, 'http://127.0.0.1/private', 'bad.bin')
            except RuntimeError: pass
            else: raise AssertionError('private URL accepted')
            with patch.object(qq.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
                try: await qq.trusted_url('https://multimedia.nt.qq.com.cn/private')
                except RuntimeError: pass
                else: raise AssertionError('private DNS accepted')

            # Missing original URL is a recorded unresolved attachment, not an empty success.
            add({'post_type': 'notice', 'notice_type': 'group_upload', 'self_id': 456, 'group_id': 123,
                 'user_id': 789, 'time': timestamp, 'file': {'id': 'opaque-id', 'name': 'no-url.zip', 'size': 12}})
            await collect()
            with api.connection() as c:
                assert c.execute('SELECT count(*) FROM qq_retry').fetchone()[0] == 1
            missing = json.loads(rows()[-1]['payload'])
            assert missing['kind'] == 'source_status' and not missing['available']

    # Authenticated raw binary delivery and complete pending pagination/receipt/daily paths.
    web = TestClient(api.app)
    token = Path(os.environ['MUSE_TUNNEL_KEY_FILE']).read_text()
    headers = {'Authorization': 'Bearer ' + token}
    for seq, expected in ((9, image), (10, document)):
        path = payload(seq)['attachments'][0]['download_path']
        assert web.get(path).status_code == 401
        response = web.get(path, headers=headers)
        assert response.status_code == 200 and response.content == expected
    store.refresh_daily()
    events, cursor, upper, pages = [], 0, None, 0
    while True:
        params = {'after_seq': cursor, 'limit': 13}
        if upper is not None: params['upper_seq'] = upper
        page = web.get('/v1/feed', params=params, headers=headers).json()
        events.extend(page['items']); cursor = page['next_cursor']; upper = page['upper_seq']; pages += 1
        if not page['has_more']: break
    assert len(events) == len(rows()) and pages > 1
    for start in range(0, len(events), 100):
        receipt = {'items': [{'event_id': e['event_id'], 'hash': e['hash']} for e in events[start:start+100]]}
        assert web.post('/v1/receipts', json=receipt, headers=headers).status_code == 200
        assert web.post('/v1/receipts', json=receipt, headers=headers).status_code == 200
    assert web.get('/v1/feed', headers=headers).json()['items'] == []
    day = __import__('datetime').datetime.now(api.LOCAL).date().isoformat()
    daily = web.get('/v1/daily/' + day, headers=headers).json()
    assert daily['source_counts']['qq'] == len(events)
    assert web.post('/v1/daily/' + day + '/audit', headers=headers, json={
        'revision': daily['revision'], 'manifest_hash': daily['manifest_hash']}).status_code == 200
    search = web.get('/v1/tools/search', params={'source': 'qq', 'conversation': '123'}, headers=headers)
    assert search.status_code == 200 and search.json()['items']
    state = web.get('/v1/sources', headers=headers).json()['sources']['qq']
    assert state['details']['attachment_failures'] == 1
    source.close()
    print(json.dumps({'result': 'PASS', 'isolated_message_ids': 218, 'events': len(events), 'feed_pages': pages,
        'binary_assets_checked': 2, 'expected_missing_url_retries': 1,
        'checks': ['text_exact', 'burst_205', 'read_only_source', 'live_history_dedup', 'edit_revision', 'reply',
                   'structured_card', 'recall', 'offline_replay', 'media_retry', 'original_bytes', 'auth',
                   'url_redirect_dns_size_guards', 'credential_redaction', 'pending_pagination', 'receipts', 'daily_audit']}))


if __name__ == '__main__':
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='check-qq-') as tmp:
        root = Path(tmp)
        key = root / 'key'; key.write_text(secrets.token_urlsafe(48)); key.chmod(0o600)
        os.environ.update(MUSE_TUNNEL_DB=str(root / 'ledger.db'), MUSE_TUNNEL_KEY_FILE=str(key))
        asyncio.run(run(root))
