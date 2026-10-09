"""Offline retry checks; no school credentials or production ledger used."""
import asyncio,os,secrets,tempfile,time
from pathlib import Path
from unittest.mock import patch,AsyncMock

with tempfile.TemporaryDirectory() as tmp:
    key=Path(tmp)/'key';key.write_text(secrets.token_urlsafe(48));key.chmod(0o600)
    os.environ.update(MUSE_TUNNEL_DB=tmp+'/ledger.db',MUSE_TUNNEL_KEY_FILE=str(key))
    import api,zjut_collect as z,httpx
    from store import state_get,state_set
    api.init()
    async def check():
        async def timeout(*args):raise httpx.ReadTimeout('simulated timeout')
        with patch.object(z,'credentials',return_value=('test','test')),patch.object(z.zjut,'_login',side_effect=timeout) as login:
            assert await z.collect(force=True)=='failed'
            assert state_get('zjut','retry_interval_seconds')=='120'
            assert await z.collect()=='cached' and login.call_count==1
            state_set('zjut','last_attempt_seconds',str(time.time()-121))
            assert await z.collect()=='failed' and login.call_count==2
            assert state_get('zjut','retry_interval_seconds')=='240'
        with patch.object(z,'credentials',return_value=('test','test')),patch.object(z.zjut,'_login',new=AsyncMock()), \
             patch.object(z.zjut,'_fetch_semester',new=AsyncMock(return_value={'year':'2026','term':'1'})), \
             patch.object(z.zjut,'_post_query',new=AsyncMock(return_value={'kbList':[]})), \
             patch.object(z,'fetch_exams',new=AsyncMock(return_value={'items':[],'totalCount':0})):
            assert await z.collect(force=True)=={'timetable':0,'exams':0}
            assert state_get('zjut','retry_interval_seconds')=='86400'
            assert state_get('zjut','network_failure_count')=='0'
        with api.connection() as c:
            status=c.execute("SELECT state FROM source_status WHERE source='zjut'").fetchone()[0]
            assert status=='available'
        print('PASS: timeout retries after 2 minutes, throttling, exponential backoff, successful recovery restores daily polling')
    asyncio.run(check())
