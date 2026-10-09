"""Single-consumer synthetic PoC. No upstream credentials or collectors."""
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
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

DB = Path(os.environ.get('MUSE_POC_DB', 'data/poc.db'))
KEY_FILE = Path(os.environ.get('MUSE_POC_KEY_FILE', 'data/consumer.key'))
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


app = FastAPI(title='Muse Tunnel Synthetic PoC', docs_url=None, redoc_url=None, openapi_url=None)
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
            if total > 65536:
                return JSONResponse({'detail': 'Body too large'}, 413)
            chunks.append(chunk)
        request._body = b''.join(chunks)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.get('/health')
def health():
    return {'ok': True, 'mode': 'synthetic'}


@api.get('/openapi.json')
def schema():
    return app.openapi()


def event(row):
    return {'seq': row['seq'], 'event_id': row['event_id'], 'hash': row['hash'],
            'ingested_at': row['ingested_at'], 'payload': json.loads(row['payload'])}


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
                'restart_pending_scan_at': 0}


@api.get('/events/{event_id}')
def detail(event_id: str):
    with connection() as c:
        row = c.execute('SELECT * FROM events WHERE event_id=?', (event_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Unknown event')
        return deliver(c, [row])[0]


class Receipt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_id: str = Field(min_length=1, max_length=100)
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
        counts = {s: sum(e['source']==s for e in entries) for s in ('chaoxing', 'dingtalk')}
        return {'date': p['day'], 'timezone': 'Asia/Shanghai', 'revision': p['revision'],
                'manifest_hash': p['hash'], 'upper_seq': p['upper_seq'], 'count': len(entries),
                'source_counts': counts, 'missing_receipt_ids': missing, 'audit_complete': bool(audit),
                'coverage': 'synthetic_only', 'created_at': p['created_at'],
                'summary': f"模拟数据共 {len(entries)} 个事件版本；此摘要不代替完整清单。",
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
    return {'mode': 'synthetic', 'consumer': CONSUMER, 'total_events': total,
            'acknowledged': acked, 'pending': total-acked, 'daily_packages': latest,
            'upstream_coverage': 'not_connected', 'muse_runtime_verified': False}


app.include_router(api)


def create_app():
    init()
    return app
