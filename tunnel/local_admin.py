"""SSH-only native client maintenance. No HTTP server or externally available key endpoint."""
import argparse
import json
import logging
import os
import re
import shutil
from pathlib import Path

os.environ.setdefault('DINGTALK_CFG', '/home/ubuntu/.config/DingTalk')
os.environ.setdefault('DINGTALK_KEY_CACHE', '/home/ubuntu/.config/DingTalk/.aes_key')
os.environ.setdefault('DISPLAY', ':99')
logging.disable(logging.CRITICAL)
from vendor import dingtalk_local as helper

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['screenshot', 'refresh-qr', 'refresh-key', 'import-file'])
    parser.add_argument('--message', help='DingTalk message key cid:mid for import-file')
    parser.add_argument('--path', help='Local file path to copy into data/imports for import-file')
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == 'screenshot':
        Path('data/dingtalk-login.png').write_bytes(helper._take_screenshot())
        print('Screenshot saved privately')
    elif args.action == 'refresh-qr':
        helper._click_qr_refresh()
        print('Refresh requested')
    elif args.action == 'import-file':
        if not args.message or ':' not in args.message or not args.path:
            parser.error('import-file requires --message <cid:mid> and --path <file>')
        src = Path(args.path)
        if src.is_symlink() or not src.is_file():
            raise SystemExit('Source file must be a regular non-symlink file')
        from api import DB, init
        from store import state_set
        init()
        imports = DB.parent / 'imports'
        imports.mkdir(parents=True, exist_ok=True, mode=0o700)
        safe_name = re.sub(r'[^\w. ()\u4e00-\u9fff-]', '_', src.name)[:180] or 'imported.bin'
        dest = (imports / safe_name).resolve()
        shutil.copyfile(src, dest)
        dest.chmod(0o600)
        state_set('dingtalk', 'file:' + args.message, json.dumps({'filepath': str(dest), 'filename': src.name}))
        print('File staged privately for next DingTalk collection pass')
    else:
        result = helper.discover_aes_key(force=True)
        print('Key refreshed' if result.get('ok') else 'Key refresh failed; collector remains unavailable')
