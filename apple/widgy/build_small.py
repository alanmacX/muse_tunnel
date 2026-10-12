"""Generate small-widget templates using the user-confirmed small export."""
import copy,json
from pathlib import Path
P=Path(__file__).parent
BASE=json.loads((P/'user-sample.json').read_text())
OUT=P/'design'


def frame(x,y,w,h):
    def value(a):return {'b':0,'a':[{'a':a,'b':615,'d':615,'c':0}]}
    return {'b':value(x),'c':value(y),'d':value(w),'e':value(h)}


def widget(theme):
    size=BASE['6']  # User confirmed this actual exported document is Small.
    root=copy.deepcopy(BASE)
    root['3']='Muse · 今日剩余安排 v2 · '+('深色' if theme=='dark' else '浅色')
    root['5']='Muse Tunnel';root['6']=size
    root['36']=[]  # Variables, not visual layers. Keep empty; no invalid names.
    root['1']=[]
    foreground='uicol_white-100' if theme=='dark' else 'uicol_black-100'
    muted='uicol_gray-100';accent='uicol_systemOrange-100'
    def layer(name,props):
        props['d0']=len(root['1'])
        root['1'].append(props)
    # Shape/text style names are established in older public exports; their current
    # v29 behavior still needs native confirmation. Document type/size are
    # copied from the user's confirmed small export, without guessing enums.
    def text(name,x,y,w,h,size,field=None,literal='',color=None):
        props={'z':'1','1':'System Medium','f':color or foreground,**frame(x,y,w,h),
            'f0':{'b':0,'a':[{'a':size,'b':615,'d':615,'c':0}]}}
        props['66']=[{'5':'Custom Text','6':'Text','25':literal}]
        if field:
            endpoint=copy.deepcopy(BASE['36'][0]['3']['66'][1])
            endpoint['18']='https://hajiqu.xyz/v1/apple/dashboard';endpoint['23']=[field]
            props['66'].append(endpoint)
        layer(name,props)
    text('Muse',130,120,760,125,10,literal='• MUSE',color=accent)
    text('Date',1190,120,280,125,8,field='display_date',color=muted)
    for n,y in ((1,335),(2,535)):
        text('Course '+str(n)+' time',130,y,440,175,11,field='display_course_'+str(n)+'_time')
        text('Course '+str(n)+' location',640,y,830,175,13,field='display_course_'+str(n)+'_location')
    text('Separator',130,720,1340,90,6,literal='────────────────────────',color=muted)
    for n,y in ((1,875),(2,1090)):
        text('Important '+str(n),130,y,1340,170,11,field='display_important_'+str(n))
    text('Freshness',130,1380,900,120,8,field='display_widget_footer',color=muted)
    text('More courses',1100,1380,370,120,8,field='display_courses_more',color=muted)
    # Widgy's visual stack lists foreground layers first, background last.
    layer('Background',{'z':'2','g':'uicol_black-100' if theme=='dark' else 'uicol_white-100',**frame(0,0,1600,1600)})
    return root

if __name__=='__main__':
    for theme in ('dark','light'):
        data=widget(theme)
        assert data['6']==BASE['6'] and data['0']==BASE['0'] and data['a2']==BASE['a2']
        assert len(data['1'])==12 and data['36']==[]
        assert len({x['d0'] for x in data['1']})==len(data['1'])
        for l in data['1']:
            for endpoint in l.get('66',[]):
                if endpoint.get('5')=='JSON Endpoint':
                    assert endpoint['18']=='https://hajiqu.xyz/v1/apple/dashboard'
                    assert len(endpoint['23'])==1
                    assert set(endpoint['21'])=={'__widgy_auth_header_name','__widgy_auth_prefix'}
        (OUT/('muse-small-'+theme+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    # Retire only the generated size guesses, not any user export.
    for theme in ('dark','light'):
        for size in (0,1,2):
            (OUT/('muse-'+theme+'-size-'+str(size)+'.json')).unlink(missing_ok=True)
    print('Generated light/dark Small templates; original document size preserved; endpoint bindings checked; no credentials embedded.')
