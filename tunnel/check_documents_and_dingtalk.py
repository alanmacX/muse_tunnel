"""Isolated verification for DingTalk & Chaoxing document capture, memory/storage savings, WAL offset replay, and specific diagnostics."""
import asyncio
import hashlib
import io
import json
import os
import secrets
import socket
import sqlite3
import struct
import tempfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx


def make_docx(paragraphs: list[str], table_rows: list[list[str]]) -> bytes:
    buf = io.BytesIO()
    body = ''.join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paragraphs)
    if table_rows:
        trs = ''.join(
            '<w:tr>' + ''.join(f'<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>' for c in row) + '</w:tr>'
            for row in table_rows
        )
        body += f'<w:tbl>{trs}</w:tbl>'
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', '<Types/>')
        zf.writestr('word/document.xml', xml)
    return buf.getvalue()


def make_xlsx(sheet_name: str, rows: list[list[str]]) -> bytes:
    buf = io.BytesIO()
    shared = []
    shared_idx = {}
    row_xml = []
    for r_idx, row in enumerate(rows, 1):
        cells = []
        for c_idx, val in enumerate(row):
            col = chr(ord('A') + c_idx)
            if val not in shared_idx:
                shared_idx[val] = len(shared)
                shared.append(val)
            cells.append(f'<c r="{col}{r_idx}" t="s"><v>{shared_idx[val]}</v></c>')
        row_xml.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    ss_xml = '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + ''.join(
        f'<si><t>{s}</t></si>' for s in shared
    ) + '</sst>'
    wb_xml = f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="{sheet_name}" sheetId="1"/></sheets></workbook>'
    ws_xml = f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{"".join(row_xml)}</sheetData></worksheet>'
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('xl/workbook.xml', wb_xml)
        zf.writestr('xl/sharedStrings.xml', ss_xml)
        zf.writestr('xl/worksheets/sheet1.xml', ws_xml)
    return buf.getvalue()


def make_pptx(slides: dict[int, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('ppt/presentation.xml', '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>')
        for num, text in slides.items():
            zf.writestr(f'ppt/slides/slide{num}.xml',
                        f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>{text}</a:t></p:sld>')
    return buf.getvalue()


def make_encrypted_dingtalk_db(root: Path, aes_key: bytes, messages: list[dict]):
    from Crypto.Cipher import AES
    plain = root / 'plain_dt.db'
    with sqlite3.connect(plain) as c:
        c.execute('PRAGMA page_size=4096')
        c.execute('PRAGMA journal_mode=DELETE')
        c.execute('CREATE TABLE tbconversation (cid TEXT PRIMARY KEY, title TEXT, unreadCount INT, lastModify INT)')
        c.execute('CREATE TABLE tbuser_profile_v2 (uid INT PRIMARY KEY, nick TEXT)')
        c.execute('CREATE TABLE tbmsg_001 (mid INT PRIMARY KEY, cid TEXT, senderId INT, contentType INT, content TEXT, createdAt INT)')
        c.execute("INSERT INTO tbconversation VALUES ('cid_cs', '计算机学院2024级', 0, 1000)")
        c.execute("INSERT INTO tbuser_profile_v2 VALUES (10001, '李老师')")
        for m in messages:
            c.execute('INSERT INTO tbmsg_001 VALUES (?,?,?,?,?,?)',
                      (m['mid'], m['cid'], m['senderId'], m['contentType'], json.dumps(m['content'], ensure_ascii=False), m['createdAt']))
    raw_bytes = plain.read_bytes()
    assert len(raw_bytes) % 4096 == 0
    cipher = AES.new(aes_key, AES.MODE_ECB)
    acct = root / 'DingTalk' / 'abcd1234_v3'
    (acct / 'DBFiles').mkdir(parents=True, exist_ok=True)
    enc_db = acct / 'DBFiles' / 'dingtalk.db'
    enc_db.write_bytes(cipher.encrypt(raw_bytes))
    key_file = root / 'DingTalk' / '.aes_key'
    key_file.write_text(aes_key.hex())
    key_file.chmod(0o600)
    plain.unlink()
    return acct, enc_db, key_file


with tempfile.TemporaryDirectory(prefix='muse-doc-check-') as tmp:
    root = Path(tmp)
    key = root / 'consumer.key'
    token = secrets.token_urlsafe(48)
    key.write_text(token)
    key.chmod(0o600)
    os.environ.update(
        MUSE_TUNNEL_DB=str(root / 'ledger.db'),
        MUSE_TUNNEL_KEY_FILE=str(key),
        DINGTALK_DECRYPTED_PATH=str(root / 'dt-snapshot.db'),
    )
    import api, chaoxing_media as cxm, collect, doc_text, media, store
    from chaoxing_health import diagnostic
    from fastapi.testclient import TestClient
    from vendor import dingtalk as dt_vendor

    api.init()

    # 1. Verify DOCX (with tables), XLSX (sheets/cells), PPTX (numeric slide order), CSV/TXT & asset_texts cache.
    docx_bytes = make_docx(['实验课程安排通知'], [['周次', '教室', '内容'], ['第8周', '广B204', '操作系统实验']])
    xlsx_bytes = make_xlsx('获奖名单', [['学号', '姓名', '等级'], ['202401', '张三', '一等奖']])
    pptx_bytes = make_pptx({1: '第一章 绪论', 2: '第二章 进程', 10: '第十章 文件系统'})

    cache_dir = root / 'SpaceFiles'
    cache_dir.mkdir(mode=0o700)
    docx_file = cache_dir / 'cached_blob_without_ext'
    docx_file.write_bytes(docx_bytes)
    archived_docx = media.archive_file(str(docx_file), [cache_dir], filename_hint='实验安排.docx', extract=True)
    assert archived_docx['status'] == 'available'
    assert archived_docx['mime_type'] == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    assert archived_docx['filename'] == '实验安排.docx'
    assert '第8周\t广B204\t操作系统实验' in archived_docx['extracted_text']

    pptx_file = cache_dir / 'slides.pptx'
    pptx_file.write_bytes(pptx_bytes)
    archived_pptx = media.archive_file(str(pptx_file), [cache_dir], extract=True)
    text_pptx = archived_pptx['extracted_text']
    assert text_pptx.index('[Slide 2]') < text_pptx.index('[Slide 10]'), text_pptx

    # 2. Verify DingTalk Space document (contentType=501): uncached -> specific diagnostic -> cached -> v2 with extracted_text!
    now_ms = int(time.time() * 1000)
    aes_key = b'0123456789abcdef'
    dt_messages = [
        {
            'mid': 9001, 'cid': 'cid_cs', 'senderId': 10001, 'contentType': 501, 'createdAt': now_ms,
            'content': {
                'contentType': 501,
                'attachments': [{
                    'type': 0, 'filepath': '', 'url': '',
                    'extension': json.dumps({
                        'cid': 'cid_cs', 's_id': 'space_88', 'f_id': 'dentry_998877',
                        'f_name': '2025-2026奖学金获奖名单.xlsx', 'f_size': str(len(xlsx_bytes)), 'f_type': 'xlsx',
                    }, ensure_ascii=False),
                }],
            },
        },
        {
            'mid': 9002, 'cid': 'cid_cs', 'senderId': 10001, 'contentType': 2950, 'createdAt': now_ms + 1,
            'content': {
                'contentType': 2950,
                'attachments': [{'type': 2950, 'extension': json.dumps({'cardInstanceId': 'card-1'})}],
            },
        },
    ]
    acct_dir, enc_db, dt_key_file = make_encrypted_dingtalk_db(root, aes_key, dt_messages)
    os.environ['DINGTALK_DB_SOURCE'] = str(root / 'DingTalk')
    os.environ['DINGTALK_KEY_FILE'] = str(dt_key_file)

    with patch.object(collect.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='')), \
         patch.object(collect, 'client_transport', return_value={'available': True, 'reason': 'remote_tcp_connected', 'remote_established_tcp': 1, 'meaning': 'ok'}):
        obs, ins = collect.collect_dingtalk()
        assert (obs, ins) == (2, 2)
        with api.connection() as c:
            st = json.loads(c.execute("SELECT details FROM source_status WHERE source='dingtalk'").fetchone()[0])
            assert st['components']['media']['recent_missing_documents'] == 1
            assert st['diagnostics'][0]['code'] == 'dingtalk_space_file_not_cached'
            assert st['diagnostics'][0]['sample_files'] == ['2025-2026奖学金获奖名单.xlsx']
            p1 = json.loads(c.execute("SELECT payload FROM events WHERE source='dingtalk' AND source_id='cid_cs:9001'").fetchone()[0])
            assert p1['attachment_status'] == 'incomplete'
            assert p1['attachments'][0]['reason'] == 'dingtalk_space_file_not_cached'
            assert 'raw_content' not in p1['parsed']  # No duplicate raw_content bloat!
            p2 = json.loads(c.execute("SELECT payload FROM events WHERE source='dingtalk' AND source_id='cid_cs:9002'").fetchone()[0])
            assert p2['attachment_status'] == 'card_reference_only'

        # Drop the XLSX into ~/Downloads (exact filename + declared size match) and re-run collector!
        downloads_dir = root / 'Downloads'
        downloads_dir.mkdir()
        (downloads_dir / 'unrelated_secret.txt').write_text('never-archive-unrelated')
        (downloads_dir / '2025-2026奖学金获奖名单.xlsx').write_bytes(xlsx_bytes)
        obs2, ins2 = collect.collect_dingtalk()
        assert (obs2, ins2) == (2, 1)  # Only 9001 produced v2; 9002 skipped via fast-path!
        with api.connection() as c:
            p1_v2 = json.loads(c.execute(
                "SELECT payload FROM events WHERE source='dingtalk' AND source_id='cid_cs:9001' ORDER BY revision DESC LIMIT 1"
            ).fetchone()[0])
            assert p1_v2['attachment_status'] == 'available'
            att = p1_v2['attachments'][0]
            assert att['mime_type'] == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            assert '张三\t一等奖' in att['extracted_text']
            st2 = json.loads(c.execute("SELECT details FROM source_status WHERE source='dingtalk'").fetchone()[0])
            assert st2['components']['media']['recent_missing_documents'] == 0

        # Steady-state pass: even if Downloads copy is evicted, fast-path does not re-read or fail!
        (downloads_dir / '2025-2026奖学金获奖名单.xlsx').unlink()
        assert collect.collect_dingtalk() == (2, 0)

    # Search finds extracted spreadsheet text directly through /v1/tools/search!
    web = TestClient(api.app)
    headers = {'Authorization': 'Bearer ' + token}
    hits = web.get('/v1/tools/search', params={'source': 'dingtalk', 'q': '张三'}, headers=headers).json()['items']
    assert len(hits) == 1 and hits[0]['event_id'] == 'dingtalk:cid_cs:9001:v2'

    # 3. Verify DingTalk WAL offset replay handles multiple commits + uncommitted tail without corrupting pages.
    from Crypto.Cipher import AES
    cipher = AES.new(aes_key, AES.MODE_ECB)
    target_db = root / 'wal_target.db'
    target_db.write_bytes(b'\x00' * 8192)
    wal_file = root / 'test.db-wal'
    salt = b'SALT1234'
    wal_hdr = b'\x37\x7f\x06\x82' + b'\x00' * 12 + salt + b'\x00' * 8
    page1_v1 = cipher.encrypt(b'OLD_VERSION_PAGE' * 256)
    page1_v2 = cipher.encrypt(b'COMMITTED_V2_PAG' * 256)
    page2_uncommitted = cipher.encrypt(b'UNCOMMITTED_TAIL' * 256)
    frames = (
        struct.pack('>II', 1, 2) + salt + b'\x00' * 8 + page1_v1 +
        struct.pack('>II', 1, 2) + salt + b'\x00' * 8 + page1_v2 +
        struct.pack('>II', 2, 0) + salt + b'\x00' * 8 + page2_uncommitted
    )
    wal_file.write_bytes(wal_hdr + frames)
    applied = dt_vendor._apply_wal_frames(target_db, str(wal_file), cipher)
    assert applied == 1
    assert target_db.read_bytes()[:16] == b'COMMITTED_V2_PAG'
    assert target_db.read_bytes()[4096:4112] == b'\x00' * 16

    # 4. Verify Chaoxing CDN subdomains (*.cldisk.com), JSON downloadData indirection, test/web/notice cards,
    #    forbidDownload, Content-Length mismatch guard, and compact source_state!
    public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]
    with patch.object(cxm.socket, 'getaddrinfo', return_value=public_dns):
        assert cxm.trusted_url('http://s1.cldisk.com/download/abc') == 'https://s1.cldisk.com/download/abc'
        assert cxm.trusted_url('https://sharewh.xuexi365.com/download/abc') == 'https://sharewh.xuexi365.com/download/abc'

    quiz_card = {'attachmentType': 25, 'att_web': {'examOrWork': 'test', 'examOrWorkId': 88, 'courseId': 12, 'title': '随堂测1'}}
    web_card = {'attachmentType': 25, 'att_web': {'title': '公众号通知', 'content': '详情', 'url': 'https://mp.weixin.qq.com/s/123'}}
    emb_notice = {'attachmentType': 8, 'att_notice': {
        'idCode': 'n99', 'title': '转发教务通知', 'content': '见附件',
        'attachment': [{'attachmentType': 18, 'att_clouddisk': {'downPath': 'https://d0.ananas.chaoxing.com/download/abcd1234abcd1234', 'name': '调课表', 'suffix': 'xlsx'}}],
    }}
    folder_att = {'attachmentType': 18, 'att_clouddisk': {'isfile': False, 'name': '实验资料夹', 'fileId': 'f1'}}
    forbid_att = {'attachmentType': 18, 'att_clouddisk': {'isfile': True, 'forbidDownload': True, 'name': '保密讲义', 'suffix': 'pdf'}}

    detail_sample = {'attachment': [quiz_card, web_card, emb_notice, folder_att, forbid_att]}
    assert cxm.unsupported_notice_attachments(detail_sample) == 0
    cards = cxm.notice_cards(detail_sample)
    assert {c['kind'] for c in cards} == {'quiz_reference', 'web_link_reference', 'notice_reference', 'folder_reference'}
    refs = cxm.notice_references(detail_sample)
    assert refs == [('https://d0.ananas.chaoxing.com/download/abcd1234abcd1234', '调课表.xlsx')]
    assert cxm.forbidden_notice_attachments(detail_sample) == ['保密讲义.pdf']

    def mock_cx_handler(req: httpx.Request):
        if req.url.host == 'mooc1.chaoxing.com' and 'downloadData' in req.url.path:
            assert 'cookie' in req.headers
            return httpx.Response(200, json={'status': True, 'url': 'https://s2.cldisk.com/download/real-docx'})
        if req.url.host == 's2.cldisk.com' and req.url.path == '/download/real-docx':
            assert 'cookie' not in req.headers
            return httpx.Response(200, content=docx_bytes, headers={'content-type': 'application/octet-stream', 'content-length': str(len(docx_bytes))})
        if req.url.path == '/truncated':
            return httpx.Response(200, content=b'partial', headers={'content-length': '100'})
        if req.url.path == '/forbidden-json':
            return httpx.Response(200, json={'status': False, 'msg': '该资料不允许下载'})
        return httpx.Response(404)

    real_async_client = httpx.AsyncClient

    async def verify_cx_downloads():
        async with real_async_client(cookies={'UID': 'secret-uid'}, transport=httpx.MockTransport(mock_cx_handler)) as auth_client, \
                   real_async_client(transport=httpx.MockTransport(mock_cx_handler)) as bare_client:
            svc = SimpleNamespace(_client=auth_client)
            with patch.object(cxm.socket, 'getaddrinfo', return_value=public_dns):
                asset = await cxm.download(svc, 'https://mooc1.chaoxing.com/coursedata/downloadData?dataId=1', '实验计划.docx', True, bare_client=bare_client)
                assert asset['status'] == 'available'
                assert '操作系统实验' in asset['extracted_text']
                compact = cxm._compact_state_asset(asset)
                assert 'extracted_text' not in compact
                rehydrated = cxm._rehydrate_asset(compact)
                assert rehydrated['extracted_text'] == asset['extracted_text']

                try:
                    await cxm.download(svc, 'https://s2.cldisk.com/truncated', 'broken.pdf', False, bare_client=bare_client)
                except ValueError as exc:
                    assert str(exc) == 'download_size_mismatch'
                else:
                    raise AssertionError('Accepted truncated Content-Length body')

                try:
                    await cxm.download(svc, 'https://mooc1.chaoxing.com/forbidden-json', 'forbid.pdf', True, bare_client=bare_client)
                except ValueError as exc:
                    diag = diagnostic(exc, 'media', 'material_download')
                    assert diag['code'] == 'attachment_download_forbidden' and diag['action'] == 'manual_check_in_app'
                else:
                    raise AssertionError('Accepted forbidden JSON response')

    asyncio.run(verify_cx_downloads())

    # 5. Verify specific network/HTTP diagnostics.
    assert diagnostic(socket.gaierror('nodename nor servname'), 'media', 'notice_attachment')['code'] == 'dns_lookup_failed'
    r404 = httpx.Response(404, request=httpx.Request('GET', 'https://notice.chaoxing.com/missing'))
    assert diagnostic(httpx.HTTPStatusError('404', request=r404.request, response=r404), 'media', 'notice_attachment')['code'] == 'resource_not_found'

    print('PASS: DOCX/XLSX/PPTX extraction, DingTalk Space document discovery & fast-path, WAL offset replay, Chaoxing CDN/JSON/card/truncation guards, compact state, and specific diagnostics')
