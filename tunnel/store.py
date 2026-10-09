"""Durable unfiltered source observations; no importance model or labels."""
from datetime import datetime,timedelta
from api import LOCAL,canonical,connection,digest,now


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
    import json
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        today=datetime.now(LOCAL).date()
        days={today.isoformat(),(today-timedelta(days=1)).isoformat()}
        days.update(r[0] for r in c.execute('SELECT source_day FROM events UNION SELECT observed_day FROM events'))
        upper=c.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        for day in sorted(days):
            manifest=[dict(r) for r in c.execute('''SELECT seq,event_id,hash,source,revision FROM events
              WHERE seq<=? AND (source_day=? OR observed_day=?) ORDER BY seq''',(upper,day,day))]
            h=digest(manifest)
            previous=c.execute('SELECT revision,hash FROM packages WHERE day=? ORDER BY revision DESC LIMIT 1',(day,)).fetchone()
            if previous and previous['hash']==h:continue
            c.execute('INSERT INTO packages VALUES (?,?,?,?,?,?)',
                      (day,previous['revision']+1 if previous else 1,upper,canonical(manifest),h,now()))


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
        changed=not old or (old['state'],old['available'],old['reason'])!=(state,int(available),reason) or json.loads(old['details']).get('components')!=details.get('components')
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
