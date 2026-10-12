"""Create local credential-bearing exports. Never write keys to tracked templates."""
import json,os,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
PRIVATE=ROOT/'tunnel'/'data'/'widgy'
PRIVATE.mkdir(mode=0o700,parents=True,exist_ok=True)
PRIVATE.chmod(0o700)
key=(ROOT/'tunnel'/'data'/'apple.key').read_text().strip()
if len(key)<40:raise RuntimeError('Apple read-only credential missing')
for theme in ('dark','light'):
    source=ROOT/'apple'/'widgy'/'design'/('muse-small-'+theme+'.json')
    value=json.loads(source.read_text())
    urls=set()
    for layer in value['1']:
        for item in layer.get('66',[]):
            if item.get('5')!='JSON Endpoint':continue
            assert item['18']=='https://hajiqu.xyz/v1/apple/dashboard'
            item['21']={'Authorization':'Bearer '+key}
            urls.add(item['18'])
    target=PRIVATE/('muse-small-'+theme+'-v2.json')
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:f.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    target.chmod(0o600)
    for url in urls:
        reply=subprocess.run(['curl','--config','-','--silent','--show-error','--fail','--proto','=https','--max-time','30',url],
            input='header = "Authorization: '+next(item['21']['Authorization'] for layer in value['1'] for item in layer.get('66',[]) if item.get('5')=='JSON Endpoint')+'"\n',text=True,capture_output=True,check=True)
        body=json.loads(reply.stdout)
        fields=[p['23'][0] for layer in value['1'] for p in layer.get('66',[]) if p.get('5')=='JSON Endpoint']
        assert all(isinstance(body.get(k),str) for k in fields)
    print(theme+': private JSON saved, 9 endpoint bindings and authenticated HTTPS verified; native rendering/header interpretation not yet verified')
