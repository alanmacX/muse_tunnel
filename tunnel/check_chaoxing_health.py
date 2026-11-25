"""Check the compact diagnostics contract without real credentials or network."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import tempfile
from unittest.mock import patch

import httpx

with tempfile.TemporaryDirectory() as tmp:
    key=Path(tmp)/'key';key.write_text(secrets.token_urlsafe(48));key.chmod(0o600)
    os.environ.update(MUSE_TUNNEL_DB=tmp+'/ledger.db',MUSE_TUNNEL_KEY_FILE=str(key))
    import api, collect, store
    from chaoxing_health import ReadIssue, read_request
    api.init()
    mode='expired';calls=[]

    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith('backclazzdata'):
            if mode=='expired':return httpx.Response(200,json={'msg':'请重新登录 private-marker'})
            if mode=='upstream':return httpx.Response(503,headers={'Retry-After':'30'},text='private-marker')
            return httpx.Response(200,json={'channelList':[]})
        if req.url.path.endswith('getNoticeList'):
            if mode=='healthy':return httpx.Response(200,json={'notices':{'list':[]}})
            return httpx.Response(200,json={'notices':{'list':[{'id':42}],'lastGetId':'signed-cursor'}})
        if mode=='retry' and calls.count('/retry')==1:return httpx.Response(503,headers={'Retry-After':'0'})
        return httpx.Response(200,json={})

    class Service:
        async def init(self,*,probe):
            assert not probe
            self._client=httpx.AsyncClient(transport=httpx.MockTransport(handler),cookies={'test':'private-marker'})

    async def media(*args):return {'available':True,'reason':'read_ok','failure_count':0,'window_start':api.now()}

    def state():
        with api.connection() as c:
            row=dict(c.execute("SELECT * FROM source_status WHERE source='chaoxing'").fetchone())
            row['details']=json.loads(row['details'])
            assert 'private-marker' not in json.dumps(row) and 'signed-cursor' not in json.dumps(row)
            return row,c.execute("SELECT count(*) FROM events WHERE source_id='availability'").fetchone()[0]

    async def run():
        global mode
        with patch.object(collect.chaoxing,'ChaoxingService',lambda _:Service()),patch.object(collect,'collect_media',media):
            await collect.collect_chaoxing()
            row,n=state()
            assert row['state']=='unavailable' and row['details']['requires_user_login']
            assert row['details']['diagnostics']==[{'stage':'courses','endpoint':'course_list','code':'login_required',
                       'http_status':200,'action':'scan_login_qr'}]
            await collect.collect_chaoxing()
            assert state()[1]==n
            mode='cursor'
            await collect.collect_chaoxing()
            row,_=state()
            issue=row['details']['diagnostics'][0]
            assert row['state']=='degraded' and issue['code']=='notice_pagination_not_advancing' and issue['page']==2
            assert store.state_get('chaoxing','inbox_anchor') is None
            mode='upstream';calls.clear()
            await collect.collect_chaoxing()
            row,_=state()
            assert calls.count('/mycourse/backclazzdata')==1  # Long Retry-After avoids an immediate retry.
            assert row['details']['diagnostics'][0]['http_status']==503
            assert not row['details']['requires_user_login']
            mode='healthy';await collect.collect_chaoxing()
            row,n=state()
            assert row['state']=='available' and row['details']['diagnostics']==[] and row['details']['last_complete_sync']
            await collect.collect_chaoxing()
            assert state()[1]==n  # Rolling media timestamps do not emit events.
        mode='retry'
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert (await read_request(client,'GET','https://notice.chaoxing.com/retry','notice_list')).status_code==200
            assert calls.count('/retry')==2

    asyncio.run(run())
    print('PASS: login/HTTP/pagination diagnosis, no secret leakage, bounded retry, resume and event deduplication')
