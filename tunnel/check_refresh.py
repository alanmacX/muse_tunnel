"""Isolated demand refresh checks; no school credentials or live refresh used."""
import os,secrets,tempfile,asyncio,time
from pathlib import Path
from unittest.mock import patch,AsyncMock

with tempfile.TemporaryDirectory() as tmp:
    key=Path(tmp)/'key';key.write_text(secrets.token_urlsafe(48));key.chmod(0o600)
    os.environ.update(MUSE_TUNNEL_DB=tmp+'/ledger.db',MUSE_TUNNEL_KEY_FILE=str(key))
    import api,zjut_collect as z
    from store import state_set,ingest
    from fastapi.testclient import TestClient
    api.init();client=TestClient(api.app);auth={'Authorization':'Bearer '+key.read_text()}
    ingest('dingtalk','notice',{'kind':'message','text':'Holiday schedule published'},api.now())
    evidence='dingtalk:notice:v1'
    body={'idempotency_key':'notice-check','reason':'Check schedule after new notice','evidence_event_id':evidence}
    assert client.post('/v1/sources/zjut/refresh',json=body).status_code==401
    def post(body,code=200):
        r=client.post('/v1/sources/zjut/refresh',json=body,headers=auth)
        assert r.status_code==code,r.text
        return r.json()
    first=post(body);assert post(body)['request_id']==first['request_id']
    post({**body,'reason':'different purpose'},409)
    post({**body,'idempotency_key':'bad-evidence','evidence_event_id':'unknown'},409)
    second=post({**body,'idempotency_key':'second-notice'})
    state_set('zjut','last_attempt_seconds',str(time.time()))
    state_set('zjut','retry_interval_seconds','86400')
    assert asyncio.run(z.collect())=='cached'
    assert client.get(first['status_path'],headers=auth).json()['state']=='queued'
    state_set('zjut','last_attempt_seconds',str(time.time()-301))
    with patch.object(z,'credentials',return_value=('test','test')),patch.object(z.zjut,'_login',new=AsyncMock()) as login, \
         patch.object(z.zjut,'_fetch_semester',new=AsyncMock(return_value={'year':'2026','term':'1'})), \
         patch.object(z.zjut,'_post_query',new=AsyncMock(return_value={'kbList':[]})), \
         patch.object(z,'fetch_exams',new=AsyncMock(return_value={'items':[],'totalCount':0})):
        assert asyncio.run(z.collect())=={'timetable':0,'exams':0}
        assert login.await_count==1  # Both requests share one source refresh.
        assert asyncio.run(z.collect())=='cached'
        assert login.await_count==1
    for request in (first,second):
        result=client.get(request['status_path'],headers=auth).json()
        assert result['state']=='completed' and result['result']['succeeded']
    with api.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM events WHERE json_extract(payload,'$.kind')='refresh_result'").fetchone()[0]==2
    print('PASS: authenticated requests, evidence validation, idempotency, cooldown, request coalescing, real completion and daily fallback')
