"""Read-only recent notice attachments and current-course material files."""
import asyncio
import html
import ipaddress
import json
import os
import re
import socket
import tempfile
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx
from api import DB, LOCAL, connection, digest, now
from chaoxing_health import ReadIssue, check_login, diagnostic, json_response, read_request
from doc_text import cached_extract, extract_document_text
from media import MAX_BYTES, archive_file
from store import ingest, state_get, state_set
from vendor import chaoxing

FORBIDDEN_HINTS = ('禁止下载', '不允许下载', '无权限下载', '不可下载', 'forbid')
_dns_cache: dict[tuple[str, int], bool] = {}


def _is_cdn_host(host: str) -> bool:
    return (host == 'cldisk.com' or host.endswith('.cldisk.com')
            or host == 'xuexi365.com' or host.endswith('.xuexi365.com'))


def _is_direct_chaoxing_file_url(url: str) -> bool:
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    host = (p.hostname or '').lower()
    path = p.path.lower()
    return ((_is_cdn_host(host) and '/download/' in path)
            or (host.endswith('.ananas.chaoxing.com') and '/download/' in path))


def trusted_url(url, auth=False):
    p = urlsplit(url)
    if p.scheme == 'http':
        url = 'https://' + url.split('://', 1)[1]
        p = urlsplit(url)
    host = (p.hostname or '').lower()
    allowed = host.endswith('.chaoxing.com') or host == 'chaoxing.com'
    if not auth:
        allowed = allowed or _is_cdn_host(host)
    if p.scheme != 'https' or not allowed or p.username or p.password or p.port not in (None, 443):
        raise ValueError('untrusted_attachment_host')
    cache_key = (host, id(socket.getaddrinfo))
    if cache_key not in _dns_cache:
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise ValueError('dns_lookup_failed') from None
        if not infos or any(not ipaddress.ip_address(info[4][0]).is_global for info in infos):
            raise ValueError('non_public_attachment_address')
        if len(_dns_cache) > 256:
            _dns_cache.clear()
        _dns_cache[cache_key] = True
    return url


async def authenticated_get(client, url, params=None, endpoint='material_list', page=None):
    for _ in range(6):
        url = trusted_url(url, auth=True)
        r = await read_request(client, 'GET', url, endpoint, page=page, params=params, follow_redirects=False)
        params = None
        if r.is_redirect:
            url = urljoin(str(r.url), r.headers['location'])
            continue
        return r
    raise ReadIssue('too_many_source_redirects', endpoint, page=page)


def extract_text(path):
    text, _ = extract_document_text(Path(path))
    return text


def _compact_state_asset(asset: dict) -> dict:
    """Keep source_state lean; extracted_text lives once in asset_texts + event payload."""
    return {k: v for k, v in asset.items() if k not in ('extracted_text', 'text_truncated')}


def _rehydrate_asset(cached: dict) -> dict:
    path = DB.parent / 'media' / cached['asset_id']
    asset = dict(cached)
    asset.update(cached_extract(path, cached['asset_id'], cached.get('mime_type', ''), cached.get('filename', '')))
    return asset


def _json_download_target(data: dict) -> str | None:
    for key in ('url', 'download', 'downPath', 'downloadUrl', 'http'):
        val = data.get(key)
        if isinstance(val, str) and val.startswith(('http://', 'https://')):
            return val
    nested = data.get('data')
    if isinstance(nested, dict):
        return _json_download_target(nested)
    if isinstance(nested, list) and nested and isinstance(nested[0], dict):
        return _json_download_target(nested[0])
    return None


async def download(svc, url, name, authenticated=False, *, bare_client=None):
    root = DB.parent / 'chaoxing-files'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    clean_name = re.sub(r'[^\w. ()\u4e00-\u9fff-]', '_', Path(name or '').name)[:180].strip(' ._') or 'attachment.bin'
    folder = Path(tempfile.mkdtemp(dir=root))
    path = folder / clean_name
    owned_bare = bare_client is None
    if owned_bare:
        bare_client = httpx.AsyncClient(
            timeout=40,
            follow_redirects=False,
            headers={'User-Agent': chaoxing.CHAOXING_DESKTOP_UA, 'Referer': 'https://pan-yz.chaoxing.com/'},
        )
    try:
        for _ in range(8):
            url = trusted_url(url, auth=authenticated)
            client = svc._client if authenticated else bare_client
            async with client.stream('GET', url, follow_redirects=False) as r:
                if r.is_redirect:
                    target = urljoin(str(r.url), r.headers['location'])
                    # Session cookies must never be forwarded to the storage CDN.
                    target = trusted_url(target)
                    authenticated = authenticated and (urlsplit(target).hostname or '').lower().endswith('.chaoxing.com')
                    url = target
                    continue
                if authenticated:
                    check_login(r, 'attachment_download')
                r.raise_for_status()
                ctype = r.headers.get('content-type', '').lower()
                disp = r.headers.get('content-disposition', '').lower()
                if 'text/html' in ctype and 'attachment' not in disp:
                    raise ValueError('download_returned_html')
                if 'application/json' in ctype and 'attachment' not in disp and Path(clean_name).suffix.lower() != '.json':
                    raw_json = await r.aread()
                    try:
                        meta = json.loads(raw_json[:65536])
                    except ValueError:
                        raise ValueError('download_api_rejected') from None
                    if isinstance(meta, dict):
                        if authenticated:
                            check_login(r, 'attachment_download', meta)
                        next_url = _json_download_target(meta)
                        if next_url:
                            next_url = trusted_url(next_url)
                            authenticated = authenticated and (urlsplit(next_url).hostname or '').lower().endswith('.chaoxing.com')
                            url = next_url
                            continue
                        msg = ' '.join(str(meta.get(k) or '') for k in ('msg', 'message', 'errorMsg')).lower()
                        if meta.get('forbidDownload') or any(h in msg for h in FORBIDDEN_HINTS):
                            raise ValueError('attachment_download_forbidden')
                    raise ValueError('download_api_rejected')
                expected_len = int(r.headers.get('content-length') or 0)
                if expected_len > MAX_BYTES:
                    raise ValueError('attachment_too_large')
                size = 0
                with path.open('wb') as f:
                    path.chmod(0o600)
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError('attachment_too_large')
                        f.write(chunk)
                if expected_len > 0 and size == 0:
                    raise ValueError('download_empty_body')
                if expected_len > 0 and size != expected_len and not r.headers.get('content-encoding'):
                    raise ValueError('download_size_mismatch')
                asset = archive_file(str(path.resolve()), [root], filename_hint=clean_name, extract=True)
                if asset is None:
                    raise ValueError('attachment_archive_failed')
                if asset.get('status') == 'too_large':
                    raise ValueError('attachment_too_large')
                return asset
        raise ValueError('too_many_attachment_redirects')
    finally:
        if owned_bare:
            await bare_client.aclose()
        path.unlink(missing_ok=True)
        try:
            folder.rmdir()
        except OSError:
            pass


class Materials(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.depth = 0
        self.time = False
        self.feed(text)
        m = re.search(r'id="totalPages"\s+value="(\d+)"', text)
        self.pages = int(m[1]) if m else None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'ul':
            if self.row is not None:
                self.depth += 1
            elif 'dataBody_td' in a.get('class', '').split():
                self.row = {
                    'name': a.get('dataname', ''),
                    'id': a.get('id', ''),
                    'type': a.get('type', ''),
                    'object_id': a.get('objectid') or a.get('resid') or '',
                    'modified': '',
                }
                self.depth = 1
        if self.row is not None:
            if tag == 'li':
                self.time = 'dataBody_time_stu' in a.get('class', '')
            if 'onclick' in a and 'toOpen(' in a['onclick']:
                values = re.findall(r"'([^']*)'", a['onclick'])
                if len(values) > 6:
                    self.row['enc'] = values[6]

    def handle_endtag(self, tag):
        if tag == 'li':
            self.time = False
        if tag == 'ul' and self.row is not None:
            self.depth -= 1
            if self.depth == 0:
                self.rows.append(self.row)
                self.row = None

    def handle_data(self, data):
        if self.row is not None and self.time:
            self.row['modified'] += data.strip()


def modified_time(value):
    value = re.sub(r'\s+', ' ', value.strip())
    try:
        if re.match(r'^\d{4}-', value):
            return datetime.fromisoformat(value).replace(tzinfo=LOCAL)
        d = datetime.strptime(str(datetime.now(LOCAL).year) + '-' + value, '%Y-%m-%d %H:%M').replace(tzinfo=LOCAL)
        if d > datetime.now(LOCAL) + timedelta(days=1):
            d = d.replace(year=d.year - 1)
        return d
    except ValueError:
        return None


async def course_materials(svc, course, cutoff, *, bare_client=None):
    cid, clazz, cpi, name = course
    folders = [('0', '')]
    seen = set()
    files = []
    while folders:
        folder, enc = folders.pop(0)
        if folder in seen:
            continue
        seen.add(folder)
        if len(seen) > 200:
            raise ValueError('material_folder_limit')
        page = 1
        page_hashes = set()
        while True:
            params = {'courseid': cid, 'clazzid': clazz, 'cpi': cpi, 'ut': 's', 'page': page}
            if folder != '0':
                params.update(dataId=folder, enc=enc)
            r = await authenticated_get(svc._client, 'https://mooc2-ans.chaoxing.com/mooc2-ans/coursedata/stu-datalist', params, page=page)
            parsed = Materials(r.text)
            if parsed.pages is None:
                raise ReadIssue('material_response_unverified', 'material_list', r.status_code, page)
            fingerprint = digest(parsed.rows)
            if page > 1 and fingerprint in page_hashes:
                raise ReadIssue('material_pagination_not_advancing', 'material_list', r.status_code, page)
            page_hashes.add(fingerprint)
            for item in parsed.rows:
                if not item['id'].isdigit():
                    continue
                if item['type'] in ('folder', '0'):
                    folders.append((item['id'], item.get('enc', '')))
                    continue
                stamp = modified_time(item['modified'])
                if stamp and stamp < cutoff:
                    continue
                item_name = item['name']
                if item_name and item.get('type') and not Path(item_name).suffix and re.fullmatch(r'[a-zA-Z0-9]{1,8}', item['type']):
                    item_name = f"{item_name}.{item['type'].lower()}"
                identity = 'material:' + cid + ':' + clazz + ':' + item['id']
                cache = state_get('chaoxing', identity)
                asset = None
                if cache:
                    cached = json.loads(cache)
                    cached_asset = cached.get('asset') or {}
                    if cached.get('modified') == item['modified'] and cached_asset.get('asset_id') and (DB.parent / 'media' / cached_asset['asset_id']).exists():
                        asset = _rehydrate_asset(cached_asset)
                if asset is None:
                    dl_params = {'dataId': item['id'], 'classId': clazz, 'cpi': cpi, 'courseId': cid, 'ut': 's'}
                    url = str(httpx.URL('https://mooc1.chaoxing.com/coursedata/downloadData', params=dl_params))
                    try:
                        asset = await download(svc, url, item_name or item['name'], True, bare_client=bare_client)
                        state_set('chaoxing', identity, json.dumps({'modified': item['modified'], 'asset': _compact_state_asset(asset)}, ensure_ascii=False))
                    except Exception as e:
                        obj_id = (item.get('object_id') or '').strip()
                        if obj_id and re.fullmatch(r'[0-9a-fA-F]{16,64}', obj_id):
                            try:
                                fallback_url = 'https://d0.ananas.chaoxing.com/download/' + obj_id
                                asset = await download(svc, fallback_url, item_name or item['name'], False, bare_client=bare_client)
                                state_set('chaoxing', identity, json.dumps({'modified': item['modified'], 'asset': _compact_state_asset(asset)}, ensure_ascii=False))
                            except Exception:
                                pass
                        if asset is None:
                            issue = diagnostic(e, 'media', 'material_download')
                            asset = {'status': 'not_downloaded', 'filename': item_name or item['name'],
                                     'reason': issue['code'], 'diagnostic': issue}
                payload = {
                    'source': 'chaoxing', 'kind': 'course_material', 'source_id': item['id'], 'course_id': cid,
                    'conversation': name, 'title': item['name'], 'modified_at_display': item['modified'],
                    'date_basis': 'source_display_with_year_inferred_if_missing',
                    'sent_at': stamp.isoformat() if stamp else None,
                    'attachments': [asset], 'attachment_status': asset['status'], 'synthetic': False,
                }
                ingest('chaoxing', identity, payload, payload['sent_at'])
                files.append(payload)
            if page >= parsed.pages:
                break
            page += 1
            if page > 100:
                raise ValueError('material_page_limit')
    return files


def _cloud_filename(cloud: dict) -> str:
    raw = str(cloud.get('name') or cloud.get('fileName') or '').strip()
    suffix = str(cloud.get('suffix') or '').strip().lstrip('.').lower()
    if raw and suffix and not Path(raw).suffix and re.fullmatch(r'[a-z0-9]{1,8}', suffix):
        return f'{raw}.{suffix}'
    return raw or (f'attachment.{suffix}' if suffix else 'attachment.bin')


def _cloud_url(cloud: dict) -> str | None:
    url = cloud.get('downPath') or cloud.get('downloadUrl')
    if url:
        return str(url)
    obj_id = str(cloud.get('objectId') or '').strip()
    if obj_id and re.fullmatch(r'[0-9a-fA-F]{16,64}', obj_id):
        return 'https://d0.ananas.chaoxing.com/download/' + obj_id
    if cloud.get('fileId'):
        return 'https://d0.cldisk.com/download/' + quote(str(cloud['fileId']), safe='')
    return None


def _parse_attachment_list(data: dict) -> tuple[list | None, bool]:
    items = data.get('attachment')
    if not items:
        return [], True
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            return None, False
    if not isinstance(items, list):
        return None, False
    return items, True


def notice_references(data):
    refs = []
    images = data.get('imgs') or data.get('img_data') or []
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except ValueError:
            images = [images]
    if isinstance(images, dict):
        images = [images]
    for item in images:
        if isinstance(item, str):
            refs.append((item, 'image.jpg'))
        elif isinstance(item, dict):
            url = item.get('imgUrl') or item.get('url') or item.get('imgurl')
            if url:
                refs.append((url, item.get('name') or 'image.jpg'))
    rtf = data.get('rtf_content') or ''
    for url in re.findall(r'<img[^>]+src=["\x27]([^"\x27]+)', rtf, re.I):
        refs.append((html.unescape(url), 'image.jpg'))
    for href, label in re.findall(r'<a[^>]+href=["\x27]([^"\x27]+)["\x27][^>]*>(.*?)</a>', rtf, re.I | re.S):
        clean_href = html.unescape(href).strip()
        if _is_direct_chaoxing_file_url(clean_href):
            plain_label = re.sub(r'<[^>]+>', '', html.unescape(label)).strip()
            refs.append((clean_href, plain_label or 'attachment.bin'))

    attachments, ok = _parse_attachment_list(data)
    for a in (attachments or []) if ok else []:
        if not isinstance(a, dict):
            continue
        if str(a.get('attachmentType')) == '8' and isinstance(a.get('att_notice'), dict):
            refs.extend(notice_references(a['att_notice']))
            continue
        if str(a.get('attachmentType')) == '25' and isinstance(a.get('att_web'), dict):
            web_url = str(a['att_web'].get('url') or '').strip()
            if _is_direct_chaoxing_file_url(web_url):
                refs.append((web_url, str(a['att_web'].get('title') or 'attachment.bin')))
            continue
        cloud = a.get('att_clouddisk')
        if isinstance(cloud, dict):
            if cloud.get('isfile') is False:
                continue
            url = _cloud_url(cloud)
            if url:
                refs.append((url, _cloud_filename(cloud)))
    return list(dict.fromkeys(refs))


def assignment_card(a):
    if not isinstance(a, dict):
        return None
    atype = str(a.get('attachmentType') or '')
    if atype == '25':
        card = a.get('att_web')
        if not isinstance(card, dict):
            return None
        ew = card.get('examOrWork')
        if ew in ('work', 'exam', 'test') and card.get('examOrWorkId'):
            kind = ('assignment_reference' if ew == 'work' else
                    'exam_reference' if ew == 'exam' else 'quiz_reference')
            return {
                'kind': kind, 'status': 'reference_only', 'title': card.get('title') or card.get('atypeName') or '',
                'course_id': str(card.get('courseId') or ''), 'class_id': str(card.get('clazzId') or ''),
                'target_id': str(card['examOrWorkId']), 'reference_text': card.get('content') or '',
                'file_download_expected': False,
            }
        web_url = str(card.get('url') or '').strip()
        if _is_direct_chaoxing_file_url(web_url):
            return None
        if web_url or card.get('title') or card.get('content'):
            return {
                'kind': 'web_link_reference', 'status': 'reference_only', 'title': card.get('title') or '',
                'reference_text': card.get('content') or '', 'file_download_expected': False,
            }
    if atype == '8' and isinstance(a.get('att_notice'), dict):
        emb = a['att_notice']
        return {
            'kind': 'notice_reference', 'status': 'reference_only', 'title': emb.get('title') or '',
            'target_id': str(emb.get('idCode') or emb.get('id') or ''), 'reference_text': emb.get('content') or '',
            'file_download_expected': False,
        }
    if isinstance(a.get('att_clouddisk'), dict) and a['att_clouddisk'].get('isfile') is False:
        cloud = a['att_clouddisk']
        return {
            'kind': 'folder_reference', 'status': 'reference_only', 'title': cloud.get('name') or '',
            'target_id': str(cloud.get('fileId') or cloud.get('resid') or ''),
            'reference_text': cloud.get('parentPath') or '', 'file_download_expected': False,
        }
    return None


def notice_cards(data):
    items, ok = _parse_attachment_list(data)
    if not ok:
        return []
    return [card for a in items if (card := assignment_card(a)) is not None]


def forbidden_notice_attachments(data):
    items, ok = _parse_attachment_list(data)
    if not ok:
        return []
    out = []
    for a in items:
        if not isinstance(a, dict):
            continue
        cloud = a.get('att_clouddisk')
        if isinstance(cloud, dict) and cloud.get('isfile') is not False and not _cloud_url(cloud) and cloud.get('forbidDownload'):
            out.append(_cloud_filename(cloud))
    return out


def unsupported_notice_attachments(data):
    items, ok = _parse_attachment_list(data)
    if not ok:
        return 1
    count = 0
    for a in items:
        if assignment_card(a) is not None:
            continue
        if isinstance(a, dict) and str(a.get('attachmentType')) == '25' and isinstance(a.get('att_web'), dict):
            if _is_direct_chaoxing_file_url(str(a['att_web'].get('url') or '')):
                continue
        if isinstance(a, dict) and isinstance(a.get('att_clouddisk'), dict):
            cloud = a['att_clouddisk']
            if _cloud_url(cloud) or cloud.get('forbidDownload'):
                continue
        count += 1
    return count


async def collect_media(svc, data, force=False):
    cutoff = datetime.now(LOCAL) - timedelta(days=7)
    cutoff_day = (cutoff.date() - timedelta(days=1)).isoformat()
    with connection() as c:
        rows = c.execute(
            "SELECT payload FROM events e WHERE source='chaoxing' AND source_id LIKE 'inbox:%' "
            "AND source_day>=? AND payload!='null' "
            "AND seq=(SELECT MAX(seq) FROM events WHERE source=e.source AND source_id=e.source_id)",
            (cutoff_day,),
        ).fetchall()
    recent = []
    for (raw_payload,) in rows:
        parsed = json.loads(raw_payload)
        if parsed and parsed.get('sent_at') and datetime.fromisoformat(parsed['sent_at']) >= cutoff:
            recent.append(parsed)
    signature = digest([(p['source_id'], p['title'], p['text'], p['raw'].get('rtf_content')) for p in recent])
    last = state_get('chaoxing', 'media_last_attempt') or state_get('chaoxing', 'media_last_success')
    previous = json.loads(state_get('chaoxing', 'media_status') or '{}')
    delay = 1800 if previous.get('available') else 300
    if not force and last and (datetime.now(LOCAL).timestamp() - float(last) < delay) and state_get('chaoxing', 'media_notice_signature') == signature:
        return previous

    failures = []
    files = []
    images = 0
    async with httpx.AsyncClient(
        timeout=40,
        follow_redirects=False,
        headers={'User-Agent': chaoxing.CHAOXING_DESKTOP_UA, 'Referer': 'https://pan-yz.chaoxing.com/'},
    ) as bare_client:
        for notice in recent:
            notice_fp = digest([notice['source_id'], notice['title'], notice['text'],
                                notice['raw'].get('rtf_content'), notice['raw'].get('attachment')])
            done_key = 'notice-done:' + notice['source_id']
            cached_done = json.loads(state_get('chaoxing', done_key) or 'null')
            if (not force and isinstance(cached_done, dict) and cached_done.get('fp') == notice_fp
                    and all((DB.parent / 'media' / aid).exists() for aid in cached_done.get('asset_ids', []))):
                images += cached_done.get('images', 0)
                continue
            try:
                r = await authenticated_get(
                    svc._client,
                    'https://notice.chaoxing.com/pc/notice/' + quote(notice['source_id'], safe='') + '/getNoticeDetail',
                    {'sendTag': notice['raw'].get('sendTag', 0)},
                    endpoint='notice_detail',
                )
                value = json_response(r, 'notice_detail', code='notice_detail_unverified')
                if not value.get('status') or not isinstance(value.get('msg'), dict):
                    raise ValueError('notice_detail_unverified')
                detail = value['msg']
                assets = []
                cards = notice_cards(detail)
                for url, name in notice_references(detail):
                    try:
                        key = 'notice-file:' + digest([notice['source_id'], url, name])
                        cached = json.loads(state_get('chaoxing', key) or 'null')
                        if cached and cached.get('asset_id') and (DB.parent / 'media' / cached['asset_id']).exists():
                            asset = _rehydrate_asset(cached)
                        else:
                            host = (urlsplit(url).hostname or '').lower()
                            need_auth = host.endswith('.chaoxing.com') and not host.endswith('.ananas.chaoxing.com')
                            asset = await download(svc, url, name, authenticated=need_auth, bare_client=bare_client)
                            state_set('chaoxing', key, json.dumps(_compact_state_asset(asset), ensure_ascii=False))
                        assets.append(asset)
                    except Exception as e:
                        issue = diagnostic(e, 'media', 'notice_attachment')
                        assets.append({'status': 'not_downloaded', 'filename': name, 'reason': issue['code'], 'diagnostic': issue})
                for fname in forbidden_notice_attachments(detail):
                    issue = diagnostic(ValueError('attachment_download_forbidden'), 'media', 'notice_attachment')
                    assets.append({'status': 'not_downloaded', 'filename': fname, 'reason': issue['code'], 'diagnostic': issue})
                if unsupported_notice_attachments(detail):
                    issue = diagnostic(ValueError('unsupported_attachment_reference'), 'media', 'notice_attachment')
                    assets.append({'status': 'not_downloaded', 'reason': 'unsupported_attachment_reference', 'diagnostic': issue})
                if any(a['status'] != 'available' for a in assets):
                    failures.extend(
                        {'diagnostic': a.get('diagnostic') or diagnostic(ValueError(a.get('reason')), 'media', 'notice_attachment'),
                         'filename': a.get('filename')}
                        for a in assets if a['status'] != 'available'
                    )
                notice_imgs = sum(a.get('mime_type', '').startswith('image/') for a in assets)
                images += notice_imgs
                att_status = ('available' if assets and all(a['status'] == 'available' for a in assets)
                              else 'incomplete' if assets else 'linked_cards_only' if cards else 'none_detected')
                payload = {
                    'source': 'chaoxing', 'kind': 'notice_detail', 'source_id': notice['source_id'],
                    'title': notice['title'], 'text': detail.get('content') or notice['text'],
                    'rtf_content': detail.get('rtf_content'), 'sent_at': notice['sent_at'],
                    'attachments': assets, 'linked_cards': cards, 'attachment_status': att_status,
                    'synthetic': False,
                }
                ingest('chaoxing', 'notice-detail:' + notice['source_id'], payload, payload['sent_at'])
                if att_status in ('available', 'linked_cards_only', 'none_detected'):
                    state_set('chaoxing', done_key, json.dumps({
                        'fp': notice_fp,
                        'asset_ids': [a['asset_id'] for a in assets if a.get('asset_id')],
                        'images': notice_imgs,
                    }))
            except Exception as e:
                failures.append({'diagnostic': diagnostic(e, 'media', 'notice_detail')})

        courses = []
        cutoff_term = chaoxing.ChaoxingService._current_semester_cutoff(datetime.now(LOCAL))
        for channel in data.get('channelList', []):
            if str(channel.get('cataid')) != '100000002':
                continue
            content = channel.get('content') or {}
            row = ((content.get('course') or {}).get('data') or [{}])[0]
            if str(content.get('isFiled', '0')) == '1' or not chaoxing.ChaoxingService._course_is_current(content, row, cutoff_term):
                continue
            cid = str(row.get('id') or '')
            clazz = str(channel.get('key') or '')
            if cid and clazz:
                courses.append((cid, clazz, str(channel.get('cpi') or ''), content.get('name') or cid))
        for course in courses:
            try:
                files.extend(await course_materials(svc, course, cutoff, bare_client=bare_client))
            except Exception as e:
                failures.append({'diagnostic': diagnostic(e, 'media', 'material_list')})

    if data.get('hasMore'):
        failures.append({'diagnostic': diagnostic(ReadIssue('course_list_pagination_required', 'course_list'), 'media', 'course_list')})
    for f in files:
        if f['attachment_status'] != 'available':
            failures.append({'diagnostic': f['attachments'][0]['diagnostic'], 'filename': f.get('title')})

    result = {
        'notices_checked': len(recent), 'courses_checked': len(courses), 'recent_materials': len(files),
        'downloaded_materials': sum(f['attachment_status'] == 'available' for f in files),
        'notice_images': images, 'failures': failures,
        'available': not failures and all(f['attachment_status'] == 'available' for f in files),
        'reason': 'read_ok' if not failures else 'partial_media_collection',
        'window_start': cutoff.isoformat(),
    }
    state_set('chaoxing', 'media_status', json.dumps(result, ensure_ascii=False))
    state_set('chaoxing', 'media_last_attempt', str(datetime.now(LOCAL).timestamp()))
    state_set('chaoxing', 'media_notice_signature', signature)
    if result['available']:
        state_set('chaoxing', 'media_last_success', str(datetime.now(LOCAL).timestamp()))
    return result
