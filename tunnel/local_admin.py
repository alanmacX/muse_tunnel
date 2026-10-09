"""SSH-only native client maintenance. No HTTP server or externally available key endpoint."""
import argparse,logging,os
from pathlib import Path
os.environ.setdefault('DINGTALK_CFG','/home/ubuntu/.config/DingTalk')
os.environ.setdefault('DINGTALK_KEY_CACHE','/home/ubuntu/.config/DingTalk/.aes_key')
os.environ.setdefault('DISPLAY',':99')
logging.disable(logging.CRITICAL)
from vendor import dingtalk_local as helper
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['screenshot','refresh-qr','refresh-key'])
    args=parser.parse_args();os.umask(0o077)
    if args.action=='screenshot':
        Path('data/dingtalk-login.png').write_bytes(helper._take_screenshot());print('Screenshot saved privately')
    elif args.action=='refresh-qr':
        helper._click_qr_refresh();print('Refresh requested')
    else:
        result=helper.discover_aes_key(force=True)
        print('Key refreshed' if result.get('ok') else 'Key refresh failed; collector remains unavailable')
