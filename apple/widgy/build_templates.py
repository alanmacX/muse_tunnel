"""Generate Widgy v29 drafts from the user's actual exported text/JSON layer.

No credentials. Native import/layout has to be checked on the user's Widgy.
No assumptions about Watch or Live Activity document-type identifiers.
"""
import copy,json,uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parent
BASE=json.loads(r'''{"l2":"","m2":"","3":"widget","q2":"","36":[{"3":{"d":{"b":0,"a":[{"d":615,"a":800,"c":0,"b":615}]},"66":[{"25":"","5":"Custom Text","6":"Text"},{"19":"GET","5":"JSON Endpoint","6":"Endpoint","18":"https:\/\/hajiqu.xyz\/health","23":["ok"],"21":{"__widgy_auth_header_name":"Authorization","__widgy_auth_prefix":"Bearer"}}],"c":{"a":[{"a":400,"b":615,"d":615,"c":0}],"b":0},"b":{"a":[{"b":615,"c":0,"a":800,"d":615}],"b":0},"z":"1","s":"Variable: Title","e":{"b":0,"a":[{"d":615,"c":0,"b":615,"a":200}]}},"1":"Title","2":0,"0":"C13DCB47-7C15-4174-AA71-792A682B1D0E"}],"a2":0,"n2":"","5":"iPhone","o2":"","0":29,"21":false,"6":2,"20":false,"p2":"","9":260,"zzz":""}''')


def save(name,value):
    (ROOT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def build():
    save('user-sample.json',BASE)
    test=copy.deepcopy(BASE)
    test['3']='Muse Tunnel · 连接测试'
    test['36'][0]['1']='连接状态（应显示 live）'
    test['36'][0]['3']['66'][1]['23']=['mode']
    save('muse-connection-test.json',test)

    # Keep the exported document family/size identifiers; only compose text layers.
    # Position values use the conventional Widgy 0..1600 design coordinate range.
    # The exact frame interpretation must still be verified in the native editor.
    dashboard=copy.deepcopy(BASE)
    dashboard['3']='Muse · 今日课程与截止（草稿）'
    dashboard['5']='Muse Tunnel'
    dashboard['36']=[]
    fields=[('标题','display_title',70,140),('课程 1','display_course_1',230,125),
        ('课程 2','display_course_2',375,125),('课程 3','display_course_3',520,125),
        ('课程其余数量','display_courses_more',660,85),
        ('截止标题','display_deadlines_heading',785,110),
        ('截止 1','display_deadline_1',925,120),('截止 2','display_deadline_2',1065,120),
        ('截止其余数量','display_deadlines_more',1200,85),
        ('更新时间和来源状态','display_freshness',1335,130)]
    for order,(label,field,y,height) in enumerate(fields):
        layer=copy.deepcopy(BASE['36'][0])
        layer['0']=str(uuid.uuid5(uuid.NAMESPACE_URL,'muse-tunnel:widgy:dashboard:'+field)).upper()
        layer['1']=label;layer['2']=order
        props=layer['3']
        props.pop('s',None)  # Do not copy the sample's Title variable binding to every new layer.
        for attr,value in [('b',80),('c',y),('d',1440),('e',height)]:
            props[attr]={'b':0,'a':[{'a':value,'b':615,'d':615,'c':0}]}
        endpoint=props['66'][1]
        endpoint['18']='https://hajiqu.xyz/v1/apple/dashboard'
        endpoint['23']=[field]
        dashboard['36'].append(layer)
    save('muse-today-dashboard.draft.json',dashboard)
    example={
        'synthetic':True,'schema_version':1,'date':'2026-10-12','timezone':'Asia/Shanghai',
        'display_title':'示例 · 今日课程（非真实数据）',
        'display_course_1':'09:00–10:35  示例课程 A · 示例教室',
        'display_course_2':'14:00–15:35  示例课程 B · 临时调课',
        'display_course_3':'','display_courses_more':'',
        'display_deadlines_heading':'今日截止 · 示例',
        'display_deadline_1':'18:00  示例实验报告 · 未完成',
        'display_deadline_2':'','display_deadlines_more':'',
        'display_freshness':'示例数据 · 仅供字段绑定和布局验证',
        'courses_today':[],'deadlines_today':[],'live_activities':[]}
    save('apple-dashboard.example.json',example)
    assert len({l['0'] for l in dashboard['36']})==len(fields)
    for layer in dashboard['36']:
        ep=layer['3']['66'][1]
        assert ep['23'][0] in example
        assert set(ep['21'])=={'__widgy_auth_header_name','__widgy_auth_prefix'}
        assert ep['19']=='GET'
    assert test['36'][0]['3']['66'][1]['23']==['mode']
    print('Generated 4 JSON files; schema paths and unique layer IDs checked. Native import/layout not yet verified.')

if __name__=='__main__':build()
