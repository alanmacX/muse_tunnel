"""Isolated retention safety and recoverability checks; no production deletion."""
import os,secrets,tempfile,json
from pathlib import Path
from datetime import datetime,timedelta,timezone

with tempfile.TemporaryDirectory() as tmp:
    key=Path(tmp)/'key';token=secrets.token_urlsafe(48);key.write_text(token);key.chmod(0o600)
    os.environ.update(MUSE_TUNNEL_DB=tmp+'/ledger.db',MUSE_TUNNEL_KEY_FILE=str(key))
    import api,store,retention
    from fastapi.testclient import TestClient
    api.init();client=TestClient(api.app);headers={'Authorization':'Bearer '+token}
    old=(datetime.now(timezone.utc)-timedelta(days=100)).isoformat();day=datetime.fromisoformat(old).astimezone(api.LOCAL).date().isoformat()
    store.ingest('dingtalk','old',{'kind':'message','text':'expired unneeded message','raw':{'content':'original'}},old)
    store.ingest('dingtalk','new',{'kind':'message','text':'recent'},api.now())
    store.ingest('zjut','schedule',{'kind':'timetable','text':'current schedule'},old)
    with api.connection() as c:c.execute("UPDATE events SET ingested_at=?,observed_day=? WHERE source_id IN ('old','schedule')",(old,day))
    store.refresh_daily()
    def call(path,body=None,code=200):
        r=client.post('/v1'+path,json=body,headers=headers) if body is not None else client.get('/v1'+path,headers=headers)
        assert r.status_code==code,(path,r.status_code,r.text)
        return r.json()
    page=call('/feed?pending_only=false');items=page['items'];old_event=items[0]
    selection={'event_id':old_event['event_id'],'hash':old_event['hash'],'reason':'expired, no further use'}
    request={'items':[selection],'dry_run':False}
    assert client.get('/v1/cleanup/candidates').status_code==401
    call('/cleanup',request,409)  # unread
    call('/receipts',{'items':[{'event_id':e['event_id'],'hash':e['hash']} for e in items]})
    call('/cleanup',request,409)  # latest daily not audited
    for d in (day,datetime.now(api.LOCAL).date().isoformat()):
        package=call('/daily/'+d)
        call('/daily/'+d+'/audit',{'revision':package['revision'],'manifest_hash':package['manifest_hash']})
    assert len(call('/cleanup/candidates')['items'])==1
    invalid={'items':[selection,{'event_id':items[1]['event_id'],'hash':items[1]['hash'],'reason':'recent'}],'dry_run':False}
    call('/cleanup',invalid,409)
    assert call('/events/'+old_event['event_id'])['payload']==old_event['payload']
    call('/cleanup',{'items':[selection],'dry_run':True})
    assert call('/events/'+old_event['event_id'])['payload']==old_event['payload']
    call('/cleanup',request)
    archived=call('/events/'+old_event['event_id']);assert archived['payload'] is None and archived['retention']['state']=='quarantined'
    assert archived['hash']==old_event['hash']
    assert call('/feed')['items']==[]  # Never resurrects unread work.
    assert call('/daily/'+day)['audit_complete']
    call('/cleanup/restore',{'event_id':old_event['event_id']})
    assert call('/events/'+old_event['event_id'])['payload']==old_event['payload']
    call('/cleanup',request)
    with api.connection() as c:c.execute('UPDATE retention SET expires_at=?',(old,))
    retention.expire()
    call('/cleanup/restore',{'event_id':old_event['event_id']},410)
    assert call('/events/'+old_event['event_id'])['retention']['state']=='purged'
    assert not (Path(tmp)/'retention'/(str(old_event['seq'])+'.json.gz')).exists()
    print('PASS: authentication, age/type/read/audit guards, atomic rejection, preview, original hashes, restore and expiry')
