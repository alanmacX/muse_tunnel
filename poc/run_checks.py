"""Runs integration checks in a temporary database without consuming Muse's feed."""
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.request import urlopen


def run():
    with tempfile.TemporaryDirectory(prefix='muse-poc-test-') as tmp:
        env=dict(os.environ,MUSE_POC_DB=tmp+'/test.db',MUSE_POC_KEY_FILE=tmp+'/consumer.key')
        os.environ.update({k:env[k] for k in ('MUSE_POC_DB','MUSE_POC_KEY_FILE')})
        subprocess.run([sys.executable,'provision.py','seed'],env=env,check=True)
        import verify
        log=open(tmp+'/server.log','w')
        process=None
        def start():
            p=subprocess.Popen([sys.executable,'-m','uvicorn','muse_tunnel.app:create_app','--factory',
                                '--host','127.0.0.1','--port','18766','--no-access-log'],env=env,stdout=log,stderr=log)
            for _ in range(100):
                try:
                    urlopen('http://127.0.0.1:18766/health',timeout=1).close()
                    return p
                except Exception:
                    if p.poll() is not None:
                        raise RuntimeError('Server failed to start')
                    time.sleep(.1)
            p.terminate(); p.wait(timeout=10)
            raise RuntimeError('Server startup timed out')
        try:
            process=start()
            verify.run()
            day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
            old=verify.call('/v1/daily/'+day)
            # Abrupt termination: receipts must survive, including daily audit.
            process.kill(); process.wait(timeout=10)
            process=start()
            assert verify.call('/v1/status')['acknowledged']==608
            assert verify.call('/v1/daily/'+day)['audit_complete']
            subprocess.run([sys.executable,'provision.py','append-late'],env=env,check=True)
            new=verify.call('/v1/daily/'+day)
            assert new['revision']==old['revision']+1
            assert not new['audit_complete'] and len(new['missing_receipt_ids'])==1
            verify.call('/v1/daily/'+day+'/audit',{'revision':old['revision'],'manifest_hash':old['manifest_hash']},expected=409)
            late=verify.call('/v1/feed')['items']
            assert len(late)==1 and late[0]['seq']==609
            # No ACK on this version: it remains pending after a crash.
            process.kill(); process.wait(timeout=10)
            process=start()
            assert verify.call('/v1/feed')['items']==late
            # Credential replacement invalidates the old key without restart.
            from pathlib import Path
            import secrets
            replacement=Path(tmp+'/next.key')
            replacement.write_text(secrets.token_urlsafe(48)+'\n'); replacement.chmod(0o600)
            replacement.replace(env['MUSE_POC_KEY_FILE'])
            verify.call('/v1/status',expected=401)
            verify.key=Path(env['MUSE_POC_KEY_FILE']).read_text().strip()
            assert verify.call('/v1/status')['pending']==1
            print('PASS: crash persistence, unacknowledged replay after restart, daily revision, stale audit rejection, immediate credential revocation.')
        finally:
            if process and process.poll() is None:
                process.terminate(); process.wait(timeout=10)
            log.close()


if __name__=='__main__':
    run()
