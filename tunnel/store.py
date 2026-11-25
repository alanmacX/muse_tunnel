"""Durable unfiltered source observations; no importance model or labels."""
from datetime import datetime,timedelta
from api import LOCAL,canonical,connection,digest,manifest_at,now


def ingest(source, source_id, payload, sent_at=None):
    encoded=canonical(payload)
    h=digest(payload)
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        previous=c.execute('SELECT revision,hash FROM events WHERE source=? AND source_id=? ORDER BY revision DESC LIMIT 1',
                           (source,source_id)).fetchone()
        if previous and previous['hash']==h:
            return False
        revision=previous['revision']+1 if previous else 1
        observed=now()
        try:
            stamp=datetime.fromisoformat(str(sent_at).replace('Z','+00:00'))
            if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=LOCAL)
            source_day=stamp.astimezone(LOCAL).date().isoformat()
        except (ValueError,TypeError):
            source_day=datetime.now(LOCAL).date().isoformat()
        c.execute('''INSERT INTO events
          (event_id,source,source_id,revision,source_day,observed_day,payload,hash,ingested_at)
          VALUES (?,?,?,?,?,?,?,?,?)''',
          (f'{source}:{source_id}:v{revision}',source,source_id,revision,source_day,
           datetime.now(LOCAL).date().isoformat(),encoded,h,observed))
    return True


def refresh_daily():
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        today=datetime.now(LOCAL).date()
        days={today.isoformat(),(today-timedelta(days=1)).isoformat()}
        # Only days with a newer ledger entry need their manifest re-hashed.
        tips=dict(c.execute('''SELECT day,max(seq) FROM (
          SELECT source_day day,seq FROM events UNION ALL SELECT observed_day,seq FROM events
        ) GROUP BY day'''))
        days.update(tips)
        upper=max(tips.values(),default=0)
        latest={r['day']:r for r in c.execute('''SELECT p.day,p.revision,p.hash,p.upper_seq
          FROM packages p JOIN (SELECT day,max(revision) revision FROM packages GROUP BY day) t
          USING(day,revision)''')}
        for day in sorted(days):
            previous=latest.get(day)
            if previous and tips.get(day,0)<=previous['upper_seq']:continue
            h=digest(manifest_at(c,day,upper))
            if previous and previous['hash']==h:continue
            c.execute('INSERT INTO packages VALUES (?,?,?,?,?,?)',
                      (day,previous['revision']+1 if previous else 1,upper,'',h,now()))
    with connection() as c:
        c.execute('PRAGMA wal_checkpoint(PASSIVE)')


def status_signature(source, details):
    components=details.get('components')
    if source not in ('chaoxing','dingtalk'):return components
    # Counts, rolling timestamps and repeated attempts are visible in /sources
    # but should not generate new availability events by themselves.
    signals={k:{name:v.get(name) for name in ('enabled','available','reason')}
             for k,v in (components or {}).items()}
    issues=sorted({(i['stage'],i['endpoint'],i['code'],i.get('http_status',0))
                   for i in details.get('diagnostics',[])})
    return signals,issues


def update_status(source,state,available,reason,details):
    import json
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM source_status WHERE source=?',(source,)).fetchone()
        timestamp=now()
        success=timestamp if available else (old['last_success'] if old else None)
        failures=0 if available else ((old['failures'] if old else 0)+1)
        c.execute('INSERT OR REPLACE INTO source_status VALUES (?,?,?,?,?,?,?,?)',
                  (source,state,int(available),timestamp,success,failures,reason,canonical(details)))
        changed=not old or (old['state'],old['available'],old['reason'])!=(state,int(available),reason) or status_signature(source,json.loads(old['details']))!=status_signature(source,details)
        if old:
            old_details=json.loads(old['details'])
            changed=changed or any(old_details.get(k)!=details.get(k) for k in ('failure_stage','failure_endpoint'))
        if changed:
            payload={'source':source,'kind':'source_status','state':state,'available':bool(available),
                     'reason':reason,'details':details,'sent_at':timestamp}
            source_id='availability'
            revision=(c.execute('SELECT COALESCE(MAX(revision),0) FROM events WHERE source=? AND source_id=?',
                                (source,source_id)).fetchone()[0])+1
            day=datetime.now(LOCAL).date().isoformat()
            c.execute('INSERT INTO events (event_id,source,source_id,revision,source_day,observed_day,payload,hash,ingested_at) VALUES (?,?,?,?,?,?,?,?,?)',
                      (f'{source}:availability:v{revision}',source,source_id,revision,day,day,canonical(payload),digest(payload),timestamp))


def state_get(source,key):
    with connection() as c:
        r=c.execute('SELECT value FROM source_state WHERE source=? AND key=?',(source,key)).fetchone()
    return r[0] if r else None


def state_set(source,key,value):
    with connection() as c:
        c.execute('INSERT OR REPLACE INTO source_state VALUES (?,?,?)',(source,key,value))
