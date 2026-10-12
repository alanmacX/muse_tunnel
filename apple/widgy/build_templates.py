"""Build a visible public connection-test widget from the user's variable export."""
import copy,json
from pathlib import Path
P=Path(__file__).parent
base=json.loads((P/'user-sample.json').read_text())
test=copy.deepcopy(base)
test['3']='Muse Tunnel · Visible connection test v2'
text=copy.deepcopy(base['36'][0]['3'])
text.pop('s',None)
text['d0']=0
text['66'][1]['23']=['mode']
test['1']=[text]
test['36']=[]
assert len(test['1'])==1 and test['36']==[]
(P/'muse-connection-test.json').write_text(json.dumps(test,ensure_ascii=False,indent=2)+'\n')
# The original ten-row draft incorrectly used variables as layers; retire it.
(P/'muse-today-dashboard.draft.json').unlink(missing_ok=True)
print('Generated one visual layer in key 1, not variables key 36; no credentials.')
