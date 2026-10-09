"""Authenticated single-consumer durable data tunnel."""
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

DB = Path(os.environ.get('MUSE_TUNNEL_DB', 'data/tunnel.db'))
KEY_FILE = Path(os.environ.get('MUSE_TUNNEL_KEY_FILE', 'data/consumer.key'))
CONSUMER = 'alan-muse'
LOCAL = ZoneInfo('Asia/Shanghai')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection():
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA synchronous=FULL')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init():
    DB.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with connection() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
          source TEXT NOT NULL, source_id TEXT NOT NULL, revision INTEGER NOT NULL,
          source_day TEXT NOT NULL, observed_day TEXT NOT NULL,
          payload TEXT NOT NULL, hash TEXT NOT NULL, ingested_at TEXT NOT NULL,
          UNIQUE(source, source_id, revision));
        CREATE TABLE IF NOT EXISTS source_status (
          source TEXT PRIMARY KEY, state TEXT NOT NULL, available INTEGER NOT NULL,
          last_attempt TEXT NOT NULL, last_success TEXT, failures INTEGER NOT NULL,
          reason TEXT NOT NULL, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_state (source TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(source,key));
        CREATE TABLE IF NOT EXISTS assets (
          asset_id TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL, mime_type TEXT NOT NULL,
          filename TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS retention (
          seq INTEGER PRIMARY KEY REFERENCES events(seq), state TEXT NOT NULL,
          raw_hash TEXT, reason TEXT NOT NULL, quarantined_at TEXT NOT NULL,
          expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS refresh_requests (
          request_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
          reason TEXT NOT NULL, evidence_event_id TEXT, created_at TEXT NOT NULL,
          state TEXT NOT NULL, started_at TEXT, completed_at TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS delivered (
          consumer TEXT NOT NULL, seq INTEGER NOT NULL REFERENCES events(seq),
          PRIMARY KEY(consumer, seq));
        CREATE TABLE IF NOT EXISTS receipts (
          consumer TEXT NOT NULL, seq INTEGER NOT NULL REFERENCES events(seq),
          hash TEXT NOT NULL, acknowledged_at TEXT NOT NULL, PRIMARY KEY(consumer, seq));
        CREATE TABLE IF NOT EXISTS packages (
          day TEXT NOT NULL, revision INTEGER NOT NULL, upper_seq INTEGER NOT NULL,
          manifest TEXT NOT NULL, hash TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(day, revision));
        CREATE TABLE IF NOT EXISTS audits (
          consumer TEXT NOT NULL, day TEXT NOT NULL, revision INTEGER NOT NULL,
          hash TEXT NOT NULL, audited_at TEXT NOT NULL,
          PRIMARY KEY(consumer, day, revision));
        ''')
    if not KEY_FILE.exists():
        raise RuntimeError('Consumer key file is required; run provision.py first')
    if KEY_FILE.stat().st_mode & 0o077:
        raise RuntimeError('Consumer key file must have mode 0600')
    if len(KEY_FILE.read_text().strip()) < 40:
        raise RuntimeError('Consumer key is too short')


security = HTTPBearer(auto_error=False)


def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
    token = credentials.credentials if credentials else ''
    # Read each time so atomic replacement revokes the previous key immediately.
    if not secrets.compare_digest(token.encode(), KEY_FILE.read_text().strip().encode()):
        raise HTTPException(401, 'Invalid consumer credential', headers={'WWW-Authenticate': 'Bearer'})
    return CONSUMER


app = FastAPI(title='Muse Data Tunnel', docs_url=None, redoc_url=None, openapi_url=None)
api = APIRouter(prefix='/v1', dependencies=[Depends(authenticate)])


@app.middleware('http')
async def bounded_request(request: Request, call_next):
    if request.method not in ('GET', 'POST'):
        return JSONResponse({'detail': 'Method not allowed'}, 405)
    if request.method == 'POST':
        total = 0
        chunks = []
        async for chunk in request.stream():
            total += len(chunk)
            if total > 262144:
                return JSONResponse({'detail': 'Body too large'}, 413)
            chunks.append(chunk)
        request._body = b''.join(chunks)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.get('/health')
def health():
    return {'ok': True, 'mode': 'live'}


@api.get('/openapi.json')
def schema():
    return app.openapi()


@api.get('/connector-policy',operation_id='get_connector_policy',
         description='Read operator-authored student steward instructions; apply only when the user asks to configure or update the connector.')
def connector_policy():
    return {'instructions':(Path(__file__).parent/'MUSE_CONNECTOR_PROMPT.md').read_text(),
            'requires_user_instruction_to_apply':True}


class RefreshRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    idempotency_key:str=Field(min_length=1,max_length=160)
    reason:str=Field(min_length=1,max_length=500)
    evidence_event_id:str|None=Field(default=None,max_length=512)


@api.post('/sources/zjut/refresh',operation_id='request_academic_refresh')
def request_academic_refresh(body:RefreshRequest):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM refresh_requests WHERE idempotency_key=?',(body.idempotency_key,)).fetchone()
        if old:
            if (old['reason'],old['evidence_event_id'])!=(body.reason,body.evidence_event_id):
                raise HTTPException(409,'Idempotency key already belongs to a different request')
            return refresh_item(old)
        if body.evidence_event_id and not c.execute('SELECT 1 FROM events WHERE event_id=?',(body.evidence_event_id,)).fetchone():
            raise HTTPException(409,'Unknown evidence event')
        if c.execute("SELECT COUNT(*) FROM refresh_requests WHERE state IN ('queued','running')").fetchone()[0]>=100:
            raise HTTPException(429,'Too many pending refresh requests')
        identity=secrets.token_hex(16)
        c.execute('INSERT INTO refresh_requests VALUES (?,?,?,?,?,?,?,?,?)',
                  (identity,body.idempotency_key,body.reason,body.evidence_event_id,now(),'queued',None,None,None))
        return refresh_item(c.execute('SELECT * FROM refresh_requests WHERE request_id=?',(identity,)).fetchone())


def refresh_item(row):
    result=dict(row)
    result['result']=json.loads(result['result']) if result['result'] else None
    result['status_path']='/v1/refresh-requests/'+row['request_id']
    result['meaning']='queued is not proof of source refresh; inspect completed result'
    return result


@api.get('/refresh-requests/{request_id}',operation_id='get_refresh_result')
def get_refresh_result(request_id:str):
    with connection() as c:
        row=c.execute('SELECT * FROM refresh_requests WHERE request_id=?',(request_id,)).fetchone()
    if row is None:raise HTTPException(404,'Unknown refresh request')
    return refresh_item(row)


def event(row):
    payload=json.loads(row['payload'])
    result={'seq': row['seq'], 'event_id': row['event_id'], 'hash': row['hash'],
            'ingested_at': row['ingested_at'], 'payload': payload}
    if payload is None:
        with connection() as c:
            retained=c.execute('SELECT state,quarantined_at,expires_at FROM retention WHERE seq=?',(row['seq'],)).fetchone()
        result['retention']=dict(retained) if retained else {'state':'unavailable'}
        result['hash_meaning']='original payload hash; payload is unavailable under retention policy'
    return result


def deliver(c, rows):
    c.executemany('INSERT OR IGNORE INTO delivered VALUES (?,?)',
                  [(CONSUMER, r['seq']) for r in rows])
    return [event(r) for r in rows]


@api.get('/feed')
def feed(after_seq: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
         upper_seq: int | None = Query(None, ge=0), pending_only: bool = True):
    with connection() as c:
        upper = upper_seq if upper_seq is not None else c.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        rows = c.execute('''SELECT e.* FROM events e WHERE seq>? AND seq<=?
          AND (?=0 OR NOT EXISTS(SELECT 1 FROM receipts r WHERE r.consumer=? AND r.seq=e.seq))
          ORDER BY seq LIMIT ?''', (after_seq, upper, int(pending_only), CONSUMER, limit+1)).fetchall()
        selected = rows[:limit]
        return {'items': deliver(c, selected), 'upper_seq': upper, 'has_more': len(rows)>limit,
                'next_cursor': selected[-1]['seq'] if selected else after_seq,
                'restart_pending_scan_at': 0, 'sources': source_availability(c)}


def find_asset(asset_id):
    if len(asset_id) != 64 or any(ch not in '0123456789abcdef' for ch in asset_id):
        raise HTTPException(404, 'Unknown file')
    with connection() as c:
        row = c.execute('SELECT * FROM assets WHERE asset_id=?', (asset_id,)).fetchone()
    path = DB.parent / 'media' / asset_id
    if row is None or not path.is_file() or path.is_symlink():
        raise HTTPException(404, 'Unknown file')
    return dict(row), path


@api.get('/files/{asset_id}', operation_id='get_file_metadata',
         description='Metadata for an archived attachment. Requires the consumer Bearer credential.')
def file_metadata(asset_id: str):
    row, _ = find_asset(asset_id)
    return {**row, 'sha256': asset_id, 'download_path': '/v1/files/' + asset_id + '/content'}


@api.get('/files/{asset_id}/content', response_class=FileResponse, operation_id='download_file',
         description='Download an archived attachment as binary for multimodal reading; verify SHA-256 and size using metadata.',
         responses={200: {'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}})
def file_content(asset_id: str):
    row, path = find_asset(asset_id)
    return FileResponse(path, media_type=row['mime_type'], filename=row['filename'],
                        content_disposition_type='attachment')


@api.get('/events/{event_id:path}')
def detail(event_id: str):
    with connection() as c:
        row = c.execute('SELECT * FROM events WHERE event_id=?', (event_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Unknown event')
        return deliver(c, [row])[0]


class Receipt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_id: str = Field(min_length=1, max_length=512)
    hash: str = Field(pattern=r'^[0-9a-f]{64}$')


class Receipts(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[Receipt] = Field(min_length=1, max_length=200)


@api.post('/receipts')
def receipts(body: Receipts):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        rows = []
        for item in body.items:
            row = c.execute('''SELECT e.* FROM events e JOIN delivered d ON d.seq=e.seq
              WHERE d.consumer=? AND e.event_id=?''', (CONSUMER, item.event_id)).fetchone()
            if row is None or row['hash'] != item.hash:
                raise HTTPException(409, 'Unknown, undelivered or mismatched event; no receipts saved')
            rows.append(row)
        c.executemany('INSERT OR IGNORE INTO receipts VALUES (?,?,?,?)',
                      [(CONSUMER, r['seq'], r['hash'], now()) for r in rows])
    return {'accepted': len(rows), 'idempotent': True, 'meaning': 'consumer-reported processing; not proof of comprehension'}


def package(c, day, revision):
    sql = 'SELECT * FROM packages WHERE day=?'
    params = [day.isoformat()]
    if revision is not None:
        sql += ' AND revision=?'
        params.append(revision)
    row = c.execute(sql+' ORDER BY revision DESC LIMIT 1', params).fetchone()
    if row is None:
        raise HTTPException(404, 'Daily package not generated yet')
    return row


@api.get('/daily/{day}')
def daily(day: date, revision: int | None = Query(None, ge=1)):
    with connection() as c:
        p = package(c, day, revision)
        entries = json.loads(p['manifest'])
        confirmed = {r[0] for r in c.execute('SELECT seq FROM receipts WHERE consumer=?', (CONSUMER,))}
        missing = [e['event_id'] for e in entries if e['seq'] not in confirmed]
        audit = c.execute('SELECT 1 FROM audits WHERE consumer=? AND day=? AND revision=?',
                          (CONSUMER, p['day'], p['revision'])).fetchone()
        counts = {s: sum(e['source']==s for e in entries) for s in ('chaoxing', 'dingtalk', 'zjut')}
        return {'date': p['day'], 'timezone': 'Asia/Shanghai', 'revision': p['revision'],
                'manifest_hash': p['hash'], 'upper_seq': p['upper_seq'], 'count': len(entries),
                'source_counts': counts, 'missing_receipt_ids': missing, 'audit_complete': bool(audit),
                'coverage': 'see_source_availability', 'created_at': p['created_at'],
                'summary': f"完整账本共 {len(entries)} 个事件版本；此摘要不代替完整清单。",
                'sources': source_availability(c),
                'items_endpoint': f"/v1/daily/{p['day']}/items?revision={p['revision']}"}


@api.get('/daily/{day}/items')
def daily_items(day: date, revision: int = Query(..., ge=1),
                offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
    with connection() as c:
        p = package(c, day, revision)
        manifest = json.loads(p['manifest'])
        part = manifest[offset:offset+limit]
        rows = [c.execute('SELECT * FROM events WHERE seq=?', (e['seq'],)).fetchone() for e in part]
        return {'manifest': part, 'items': deliver(c, rows), 'revision': revision,
                'manifest_hash': p['hash'], 'next_offset': offset+len(part),
                'has_more': offset+len(part)<len(manifest)}


class Audit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    manifest_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


@api.post('/daily/{day}/audit')
def audit(day: date, body: Audit):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        p = package(c, day, None)
        if p['revision'] != body.revision or p['hash'] != body.manifest_hash:
            raise HTTPException(409, 'Daily package changed; fetch latest revision')
        confirmed = {r[0] for r in c.execute('SELECT seq FROM receipts WHERE consumer=?', (CONSUMER,))}
        if any(e['seq'] not in confirmed for e in json.loads(p['manifest'])):
            raise HTTPException(409, 'Unacknowledged events remain')
        c.execute('INSERT OR IGNORE INTO audits VALUES (?,?,?,?,?)',
                  (CONSUMER, p['day'], p['revision'], p['hash'], now()))
    return {'audit_complete': True, 'revision': body.revision}


@api.get('/status')
def status():
    with connection() as c:
        total = c.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        acked = c.execute('SELECT COUNT(*) FROM receipts WHERE consumer=?', (CONSUMER,)).fetchone()[0]
        latest = [dict(r) for r in c.execute('''SELECT day,MAX(revision) revision FROM packages GROUP BY day''')]
        sources = source_availability(c)
    return {'mode': 'live', 'dataset_id': 'live-v1', 'consumer': CONSUMER, 'total_events': total,
            'acknowledged': acked, 'pending': total-acked, 'daily_packages': latest,
            'upstream_coverage': 'bounded_by_source_interfaces', 'sources': sources}


app.include_router(api)

# Retention has bounded operations only, never arbitrary filesystem deletion.
from retention import router as retention_router
app.include_router(retention_router, prefix='/v1', dependencies=[Depends(authenticate)])


def create_app():
    init()
    return app


def source_availability(c):
    result = {}
    current = datetime.now(timezone.utc)
    for source in ('chaoxing', 'dingtalk', 'zjut'):
        row = c.execute('SELECT * FROM source_status WHERE source=?', (source,)).fetchone()
        if not row:
            result[source] = {'state':'unknown','available':False,'reason':'not_yet_checked'}
            continue
        item = dict(row)
        item['available'] = bool(item['available'])
        item['details'] = json.loads(item['details'])
        age = (current-datetime.fromisoformat(item['last_attempt'])).total_seconds()
        item['check_age_seconds'] = round(age)
        stale_after = item['details'].get('stale_after_seconds',300)
        if age > stale_after:
            item.update(state='stale',available=False,reason='collector_heartbeat_missing')
        result[source] = item
    return result


def sources_endpoint():
    with connection() as c:
        return {'timezone':'Asia/Shanghai','sources':source_availability(c)}

# Router was registered above; register the sources endpoint explicitly.
app.add_api_route('/v1/sources', sources_endpoint, methods=['GET'], dependencies=[Depends(authenticate)], operation_id='get_source_availability')

# Task-oriented read tools share the same ledger and authentication as the feed.
from agent_tools import router as agent_tools_router
app.include_router(agent_tools_router)
