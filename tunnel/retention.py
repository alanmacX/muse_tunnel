"""Consumer-selected, audited retention with a fourteen-day restore window."""
import gzip
import json
from datetime import datetime,timedelta,timezone
from pathlib import Path
from fastapi import APIRouter,HTTPException,Query
from pydantic import BaseModel,Field,ConfigDict
import api

router=APIRouter()
MIN_DAYS=90


def eligible(c,row):
    if json.loads(row['payload']) is None:return False
    if json.loads(row['payload']).get('kind')!='message':return False
    cutoff=datetime.now(timezone.utc)-timedelta(days=MIN_DAYS)
    if datetime.fromisoformat(row['ingested_at'])>cutoff:return False
    if not c.execute('SELECT 1 FROM receipts WHERE consumer=? AND seq=?',(api.CONSUMER,row['seq'])).fetchone():return False
    for day in {row['source_day'],row['observed_day']}:
        if not c.execute('''SELECT 1 FROM packages p JOIN audits a
          ON a.day=p.day AND a.revision=p.revision AND a.hash=p.hash AND a.consumer=?
          WHERE p.day=? AND p.revision=(SELECT MAX(revision) FROM packages WHERE day=?)''',
          (api.CONSUMER,day,day)).fetchone():return False
    return True


@router.get('/cleanup/candidates',operation_id='list_cleanup_candidates')
def candidates(after_seq:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=200)):
    with api.connection() as c:
        rows=c.execute('SELECT * FROM events WHERE seq>? AND ingested_at<=? ORDER BY seq LIMIT ?',
          (after_seq,(datetime.now(timezone.utc)-timedelta(days=MIN_DAYS)).isoformat(),limit+1)).fetchall()
        selected=rows[:limit]
        items=[api.event(r) for r in selected if eligible(c,r)]
    return {'items':items,'has_more':len(rows)>limit,'next_cursor':selected[-1]['seq'] if selected else after_seq,
            'minimum_age_days':MIN_DAYS,'restore_window_days':14,
            'meaning':'Age and audit checks only. Muse must exclude active reminders, important evidence and useful records.'}


class Selection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    event_id:str=Field(min_length=1,max_length=512)
    hash:str=Field(pattern=r'^[0-9a-f]{64}$')
    reason:str=Field(min_length=1,max_length=500)


class Cleanup(BaseModel):
    model_config=ConfigDict(extra='forbid')
    items:list[Selection]=Field(min_length=1,max_length=100)
    dry_run:bool=True


@router.post('/cleanup',operation_id='quarantine_unneeded_records')
def cleanup(body:Cleanup):
    root=api.DB.parent/'retention';root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with api.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        rows=[]
        for item in body.items:
            row=c.execute('SELECT * FROM events WHERE event_id=?',(item.event_id,)).fetchone()
            if row is None or row['hash']!=item.hash:raise HTTPException(409,'Unknown or mismatched record; no records removed')
            previous=c.execute('SELECT state FROM retention WHERE seq=?',(row['seq'],)).fetchone()
            if previous and previous['state'] in ('quarantined','purged'):
                continue
            if not eligible(c,row):raise HTTPException(409,'Record is recent, unread, unaudited or protected by type; no records removed')
            rows.append((row,item))
        if not body.dry_run:
            for row,item in rows:
                target=root/(str(row['seq'])+'.json.gz')
                # Archive before modifying the ledger; a crash leaves at worst an extra copy.
                with target.open('wb') as f:
                    target.chmod(0o600)
                    with gzip.GzipFile(fileobj=f,mode='wb') as compressed:compressed.write(row['payload'].encode())
                    f.flush()
                    import os
                    os.fsync(f.fileno())
                raw=json.loads(row['payload']).get('raw')
                expires=(datetime.now(timezone.utc)+timedelta(days=14)).isoformat()
                c.execute('INSERT OR REPLACE INTO retention VALUES (?,?,?,?,?,?)',
                  (row['seq'],'quarantined',api.digest(raw),item.reason,api.now(),expires))
                c.execute("UPDATE events SET payload='null' WHERE seq=?",(row['seq'],))
    return {'dry_run':body.dry_run,'selected':len(rows),'state':'preview' if body.dry_run else 'quarantined',
            'restore_window_days':14,'ledger_hashes_and_receipts_preserved':True}


class Restore(BaseModel):
    model_config=ConfigDict(extra='forbid')
    event_id:str=Field(min_length=1,max_length=512)


@router.post('/cleanup/restore',operation_id='restore_quarantined_record')
def restore(body:Restore):
    with api.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('''SELECT e.seq,r.* FROM events e JOIN retention r ON r.seq=e.seq
          WHERE e.event_id=?''',(body.event_id,)).fetchone()
        if not row or row['state']!='quarantined' or row['expires_at']<=api.now():raise HTTPException(410,'Restore window unavailable')
        path=api.DB.parent/'retention'/(str(row['seq'])+'.json.gz')
        with gzip.open(path,'rt') as f:payload=f.read()
        original=c.execute('SELECT hash FROM events WHERE seq=?',(row['seq'],)).fetchone()[0]
        if api.digest(json.loads(payload))!=original:raise HTTPException(409,'Archive hash mismatch')
        c.execute('UPDATE events SET payload=? WHERE seq=?',(payload,row['seq']))
        c.execute("UPDATE retention SET state='restored' WHERE seq=?",(row['seq'],))
    return {'restored':True,'event_id':body.event_id}


def expire():
    root=api.DB.parent/'retention'
    with api.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        for row in c.execute("SELECT seq FROM retention WHERE state='quarantined' AND expires_at<=?",(api.now(),)).fetchall():
            (root/(str(row['seq'])+'.json.gz')).unlink(missing_ok=True)
            c.execute("UPDATE retention SET state='purged' WHERE seq=?",(row['seq'],))
        for row in c.execute("SELECT seq FROM retention WHERE state='restored'"):
            (root/(str(row['seq'])+'.json.gz')).unlink(missing_ok=True)
        # A file is removable only after 90 days and when no active or restorable payload references it.
        keep=set()
        for path in root.glob('*.json.gz'):
            if path.stem.split('.')[0].isdigit():
                try:
                    with gzip.open(path,'rt') as f:keep.update(asset_ids(json.load(f)))
                except (OSError,ValueError):return  # Fail closed if archive cannot be read.
        cutoff=(datetime.now(timezone.utc)-timedelta(days=MIN_DAYS)).isoformat()
        for row in c.execute('SELECT asset_id FROM assets WHERE created_at<=?',(cutoff,)).fetchall():
            asset_id=row['asset_id']
            if asset_id in keep:continue
            if c.execute('SELECT 1 FROM events WHERE payload LIKE ? LIMIT 1',('%'+asset_id+'%',)).fetchone():continue
            (api.DB.parent/'media'/asset_id).unlink(missing_ok=True)
            c.execute('DELETE FROM assets WHERE asset_id=?',(asset_id,))


def asset_ids(value):
    if isinstance(value,dict):
        return ({value['asset_id']} if isinstance(value.get('asset_id'),str) else set()).union(*(asset_ids(v) for v in value.values()))
    if isinstance(value,list):return set().union(*(asset_ids(v) for v in value))
    return set()


if __name__=='__main__':
    api.init();expire();print('Expired retention quarantines and unreferenced old media')
