"""Exercise the live protocol in an isolated ledger; never ACK production events."""
import json,os,secrets,subprocess,sys,tempfile,time
from pathlib import Path
from datetime import datetime,timedelta,timezone
from urllib.request import Request,urlopen
from urllib.error import HTTPError

with tempfile.TemporaryDirectory(prefix='muse-live-check-') as tmp:
    key=secrets.token_urlsafe(48);keyfile=Path(tmp)/'key';keyfile.write_text(key);keyfile.chmod(0o600)
    os.environ.update(MUSE_TUNNEL_DB=tmp+'/test.db',MUSE_TUNNEL_KEY_FILE=str(keyfile))
    import api,store
    api.init()
    day=datetime.now(api.LOCAL).date().isoformat()
    for n in range(608):store.ingest('dingtalk',f'cid/slash:{n}',{'text':str(n)},api.now())
    store.refresh_daily()
    store.update_status('chaoxing','degraded',True,'partial_collection',{'components':{'im':{'available':False}}})
    store.update_status('dingtalk','available_local_cache',True,'client_cache_readable',{'upstream_connection':'unknown'})
    with api.connection() as c:c.execute('UPDATE source_status SET last_attempt=? WHERE source=?',((datetime.now(timezone.utc)-timedelta(seconds=301)).isoformat(),'dingtalk'))
    def call(path,body=None,expected=200,auth=True):
        req=Request('http://127.0.0.1:18768'+path,data=json.dumps(body).encode() if body is not None else None,
                    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'} if auth else {})
        try:
            with urlopen(req,timeout=10) as r:code=r.status;data=json.load(r)
        except HTTPError as e:code=e.code;data=json.load(e)
        assert code==expected,(path,code,data)
        return data
    log=open(tmp+'/server.log','w')
    def start():
        process=subprocess.Popen([sys.executable,'-m','uvicorn','api:create_app','--factory','--host','127.0.0.1','--port','18768','--no-access-log'],stdout=log,stderr=log)
        for _ in range(100):
            try:call('/health');return process
            except Exception:
                if process.poll() is not None:raise RuntimeError('test server failed')
                time.sleep(.1)
        raise RuntimeError('startup timed out')
    process=start()
    try:
        call('/v1/status',expected=401,auth=False)
        status=call('/v1/sources')['sources'];assert status['dingtalk']['state']=='stale';assert status['chaoxing']['details']['components']['im']['available'] is False
        items=[];cursor=0;upper=None
        while True:
            page=call(f'/v1/feed?after_seq={cursor}&limit=100'+(f'&upper_seq={upper}' if upper else ''))
            upper=page['upper_seq'];items+=page['items'];cursor=page['next_cursor']
            if not page['has_more']:break
        assert len(items)==610 and len({e['event_id'] for e in items})==610
        assert call('/v1/events/'+items[0]['event_id'])['event_id']==items[0]['event_id']
        invalid=[{'event_id':items[0]['event_id'],'hash':items[0]['hash']},{'event_id':items[1]['event_id'],'hash':'0'*64}]
        call('/v1/receipts',{'items':invalid},409);assert call('/v1/status')['acknowledged']==0
        for offset in range(0,len(items),100):call('/v1/receipts',{'items':[{'event_id':e['event_id'],'hash':e['hash']} for e in items[offset:offset+100]]})
        assert call('/v1/status')['pending']==0
        package=call('/v1/daily/'+day)
        call('/v1/daily/'+day+'/audit',{'revision':package['revision'],'manifest_hash':package['manifest_hash']})
        process.kill();process.wait();process=start();assert call('/v1/status')['acknowledged']==610
        assert call('/v1/daily/'+day)['audit_complete']
        assert not store.ingest('dingtalk','cid/slash:0',{'text':'0'},api.now())
        store.ingest('dingtalk','cid/slash:0',{'text':'revised'},api.now())
        store.ingest('chaoxing','late',{'text':'late'},(datetime.now(api.LOCAL)-timedelta(days=1)).isoformat())
        store.refresh_daily()
        call('/v1/daily/'+day+'/audit',{'revision':package['revision'],'manifest_hash':package['manifest_hash']},409)
        replay=call('/v1/feed')['items'];assert len(replay)==2
        process.kill();process.wait();process=start();assert call('/v1/feed')['items']==replay
        newkey=Path(tmp)/'next';newkey.write_text(secrets.token_urlsafe(48));newkey.chmod(0o600);newkey.replace(keyfile)
        call('/v1/status',expected=401)
        print('PASS: 610 events / 7 pages, slash IDs, atomic ACK, source degradation/staleness, crash persistence, late arrival, revision replay, stale audit rejection, key revocation.')
    finally:
        if process.poll() is None:process.terminate();process.wait()
        log.close()
