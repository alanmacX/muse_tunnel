"""Administrator-only QR login; never exposed to Muse or the public API."""
import asyncio,base64,logging,os
from pathlib import Path
from vendor.chaoxing import ChaoxingService
logging.disable(logging.CRITICAL)
os.umask(0o077)
async def main():
    svc=ChaoxingService('data/sessions.db')
    try:
        qr=await svc.create_qr_session()
        Path('data/chaoxing-login.png').write_bytes(base64.b64decode(qr['image_data_url'].split(',',1)[1]))
        print('QR_READY',flush=True)
        for _ in range(150):
            status=(await svc.poll_qr(qr['uuid'],qr['enc'])).get('status')
            if status=='confirmed':
                print('LOGIN_CONFIRMED',flush=True);return
            if status in ('expired','failed'):
                print('QR_'+status.upper(),flush=True);return
            await asyncio.sleep(2)
        print('QR_TIMEOUT',flush=True)
    finally:
        for client in (svc._client,svc._qr_client):
            if client:await client.aclose()
if __name__=='__main__':asyncio.run(main())
