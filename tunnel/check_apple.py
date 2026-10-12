"""Isolated Apple token scopes, compact views, clocks, CAS and emergency lifecycle."""
import copy,os,secrets,tempfile
from datetime import datetime as RealDatetime,timedelta
from pathlib import Path
with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);consumer=secrets.token_urlsafe(48);device=secrets.token_urlsafe(48)
    for name,value in [('key',consumer),('apple.key',device)]:
        p=root/name;p.write_text(value);p.chmod(0o600)
    os.environ.update(MUSE_TUNNEL_DB=str(root/'ledger.db'),MUSE_TUNNEL_KEY_FILE=str(root/'key'),MUSE_APPLE_KEY_FILE=str(root/'apple.key'))
    import api,apple_api as a,store
    from fastapi.testclient import TestClient
    api.init();client=TestClient(api.app)
    muse={'Authorization':'Bearer '+consumer};watch={'Authorization':'Bearer '+device}
    fixed=RealDatetime.now(api.LOCAL).replace(hour=8,minute=0,second=0,microsecond=0)
    class Clock(RealDatetime):
        @classmethod
        def now(cls,tz=None):return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    a.datetime=Clock
    a.now=lambda:fixed.isoformat()
    def get(path='/v1/apple/dashboard',auth=watch):return client.get(path,headers=auth)
    def post(body):return client.post('/v1/apple/dashboard',json=body,headers=muse)
    assert client.get('/v1/apple/dashboard').status_code==401
    assert get().json()['status']=='awaiting_muse'
    for path in ('/v1/feed','/v1/status','/v1/connector-policy','/v1/apple/state','/v1/files/unknown','/v1/tools/search'):
        assert get(path).status_code==401,path
    assert client.post('/v1/apple/heartbeat',json={'base_revision':1},headers=watch).status_code==401
    store.ingest('chaoxing','test-evidence',{'kind':'notice','title':'isolated source'},fixed.isoformat())
    with api.connection() as c:
        r=c.execute('SELECT event_id,hash FROM events').fetchone();evidence=[dict(r)]
    def stamp(hours):return (fixed+timedelta(hours=hours)).isoformat()
    body={'base_revision':0,'date':fixed.date().isoformat(),'courses_today':[
        {'id':'course-'+str(n),'title':'Example '+str(n),'location':'Room '+str(n),'start_at':stamp(n),'end_at':stamp(n+.5),'evidence':evidence} for n in (1,2,3)],
        'important_events':[{'id':'task','title':'Important task','action':'Check details','reason':'Requires action today','due_at':stamp(4),'evidence':evidence}],
        'live_activities':[{'id':'emergency','title':'Immediate action','action':'Check verified notice','reason':'Immediate personal impact','severity':'emergency','start_at':stamp(-.1),'end_at':stamp(1),'evidence':evidence}]}
    assert client.post('/v1/apple/dashboard',json=body,headers=watch).status_code==401
    created=post(body);assert created.status_code==200,created.text
    assert created.json()['revision']==1 and created.json()['changed']
    assert post(body).json()['changed'] is False
    result=get().json();assert len(result['courses'])==2 and result['remaining_courses_count']==1 and len(result['important_events'])==1
    assert len(get('/v1/apple/live-activities').json()['live_activities'])==1
    assert 'evidence' not in result['courses'][0] and 'content' not in result
    assert get('/v1/apple/watch').json()['courses']==result['courses']
    invalid=copy.deepcopy(body);invalid['live_activities'][0]['severity']='time_sensitive'
    assert post(invalid).status_code==422
    invalid=copy.deepcopy(body);invalid['courses_today'][0]['evidence'][0]['hash']='0'*64
    assert post(invalid).status_code==409
    changed=copy.deepcopy(body);changed['courses_today'][0]['location']='Other room'
    assert post(changed).status_code==409
    changed['base_revision']=1;assert post(changed).json()['revision']==2
    assert client.post('/v1/apple/heartbeat',json={'base_revision':1},headers=muse).status_code==409
    assert client.post('/v1/apple/heartbeat',json={'base_revision':2},headers=muse).status_code==200
    quiet=client.post('/v1/apple/heartbeat?quiet=true',json={'base_revision':2},headers=muse)
    assert quiet.status_code==204 and quiet.content==b''
    assert client.post('/v1/apple/heartbeat?quiet=true',json={'base_revision':1},headers=muse).status_code==409
    fixed+=timedelta(minutes=16)
    stale=get().json();assert stale['stale'] and stale['courses'] and stale['live_activities']==[]
    assert client.post('/v1/apple/heartbeat',json={'base_revision':2},headers=muse).status_code==200
    assert not get().json()['stale']
    # Merely passing the start time filters courses without Muse needing another write.
    fixed+=timedelta(minutes=44)
    current=get().json();assert current['courses'][0]['id']=='course-2' and current['remaining_courses_count']==0
    assert get('/v1/apple/live-activities').json()['live_activities']==[]
    assert client.post('/v1/apple/heartbeat',json={'base_revision':2,'maintenance_status':'partial'},headers=muse).status_code==200
    assert get().json()['status']=='partial'
    fixed+=timedelta(days=1)
    nextday=get().json();assert nextday['courses']==[] and nextday['important_events']==[] and nextday['status']=='awaiting_today_plan'
    assert client.post('/v1/apple/heartbeat',json={'base_revision':2},headers=muse).status_code==409
    assert post(changed).status_code==422
    key=root/'apple.key';key.write_text(secrets.token_urlsafe(48))
    assert get().status_code==401
    assert get(auth=muse).status_code==200
    assert client.get('/v1/status',headers=muse).json()['acknowledged']==0
    print('PASS: Apple read-only scope/revocation, evidence/hash validation, compact counts, idempotent publish/CAS, heartbeat, started-class filtering, stale preservation, emergency-only expiry, midnight rollover and no implicit receipts')
