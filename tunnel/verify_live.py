"""Read-only production transport and hash verification. Sends no receipts."""
import json
from pathlib import Path
import re,subprocess
from api import digest
key=Path('data/consumer.key').read_text().strip()
assert re.fullmatch(r'[A-Za-z0-9_-]+',key)
def get(path,auth=True,expected=200):
    config='header = "Authorization: Bearer '+key+'"\n' if auth else ''
    response=subprocess.run(['curl','--config','-','--silent','--show-error','--proto','=https',
        '--connect-timeout','10','--max-time','30','--write-out','\n%{http_code}',
        'https://hajiqu.xyz'+path],input=config,text=True,capture_output=True,check=True)
    body,code=response.stdout.rsplit('\n',1)
    assert int(code)==expected,(path,code)
    return json.loads(body) if expected==200 else None
assert get('/health',False)['mode']=='live'
get('/v1/status',False,401)
get('/v1/openapi.json',False,401)
assert get('/v1/openapi.json')['info']['title']=='Muse Data Tunnel'
get('/docs',False,404)
cursor=0;upper=None;count=0;pages=0;ids=set()
while True:
    page=get(f'/v1/feed?after_seq={cursor}&limit=100'+(f'&upper_seq={upper}' if upper is not None else ''))
    if upper is None:upper=page['upper_seq']
    for event in page['items']:
        assert event['hash']==digest(event['payload'])
        assert event['event_id'] not in ids
        ids.add(event['event_id']);count+=1
        assert 'priority' not in event['payload']
        assert event['payload'].get('synthetic',False) is False
    pages+=1;cursor=page['next_cursor']
    if not page['has_more']:break
status=get('/v1/status')
assert status['dataset_id']=='live-v1'
print(json.dumps({'HTTPS':'verified','unauthenticated_status':401,'verified_events':count,'pages':pages,'upper_seq':upper,'pending':status['pending'],'sources':{k:{'state':v['state'],'reason':v['reason'],'components':v['details'].get('components'),'observed_messages':v['details'].get('observed_messages')} for k,v in status['sources'].items()}},ensure_ascii=False))
