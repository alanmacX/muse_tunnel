"""Build the user's lock-screen document with two live course text layers."""
import copy,json,os,subprocess
from pathlib import Path
from build_small import BASE,frame

P=Path(__file__).parent
root=json.loads((P/'lock-sample.json').read_text())
root['3']='今日课程 · 锁屏 v1'
root['36']=[]
root['1']=[]
for index,(field,y,height,size) in enumerate((
    ('display_course_1_title',220,520,18),
    ('display_course_1_location',780,400,13),
)):
    endpoint=copy.deepcopy(BASE['36'][0]['3']['66'][1])
    endpoint['18']='https://hajiqu.xyz/v1/apple/watch'
    endpoint['23']=[field]
    root['1'].append({'z':'1','d0':index,'1':'System Medium','f':'uicol_label-100',
        **frame(80,y,1440,height),
        'f0':{'b':0,'a':[{'a':size,'b':615,'d':615,'c':0}]},
        '66':[{'5':'Custom Text','6':'Text','25':''},endpoint]})

# Preserve the exact lock-screen family/type provided by the user.
assert (root['0'],root['6'],root['a2'])==(29,6,4)
public=P/'design'/'muse-lock-course.json'
public.write_text(json.dumps(root,ensure_ascii=False,indent=2)+'\n')
key=(P.parents[1]/'tunnel'/'data'/'apple.key').read_text().strip()
if len(key)<40:raise RuntimeError('Apple read-only credential missing')
for layer in root['1']:
    layer['66'][1]['21']={'Authorization':'Bearer '+key}
private=P.parents[1]/'tunnel'/'data'/'widgy'/'muse-lock-course-v1.json'
private.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
private.parent.chmod(0o700)
fd=os.open(private,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as f:f.write(json.dumps(root,ensure_ascii=False,indent=2)+'\n')
private.chmod(0o600)
reply=subprocess.run(['curl','--config','-','--silent','--show-error','--fail',
    '--proto','=https','--max-time','30','https://hajiqu.xyz/v1/apple/watch'],
    input='header = "Authorization: Bearer '+key+'"\n',text=True,capture_output=True,check=True)
body=json.loads(reply.stdout)
assert all(isinstance(body.get(layer['66'][1]['23'][0]),str) for layer in root['1'])
print('Lock-screen document generated: two text layers, original family preserved, authenticated Watch endpoint verified. Native display not yet verified.')
