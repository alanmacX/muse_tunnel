"""Muse-maintained compact Apple views; independent read-only device credential."""
import json,os,re,secrets
from datetime import date,datetime,timedelta
from pathlib import Path
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException,Query,Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import AliasChoices,BaseModel,ConfigDict,Field,model_validator
from api import CONSUMER,KEY_FILE,LOCAL,authenticate,canonical,connection,digest,now,security,source_availability

APPLE_KEY_FILE=Path(os.environ.get('MUSE_APPLE_KEY_FILE','data/apple.key'))
STALE_SECONDS=1500
router=APIRouter(prefix='/v1/apple')


def init_apple():
    with connection() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS apple_dashboard (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),revision INTEGER NOT NULL,
          content TEXT NOT NULL,content_hash TEXT NOT NULL,maintained_at TEXT NOT NULL,
          maintenance_status TEXT NOT NULL)''')


def authenticate_apple(credentials:HTTPAuthorizationCredentials|None=Depends(security)):
    supplied=(credentials.credentials if credentials else '').encode()
    if secrets.compare_digest(supplied,KEY_FILE.read_text().strip().encode()):return CONSUMER
    if APPLE_KEY_FILE.exists() and not APPLE_KEY_FILE.stat().st_mode & 0o077:
        token=APPLE_KEY_FILE.read_text().strip()
        if len(token)>=40 and secrets.compare_digest(supplied,token.encode()):return 'apple-read-only'
    raise HTTPException(401,'Invalid Apple view credential',headers={'WWW-Authenticate':'Bearer'})


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid')


class Evidence(Strict):
    event_id:str=Field(min_length=1,max_length=512)
    hash:str=Field(pattern=r'^[0-9a-f]{64}$')


class Referenced(Strict):
    id:str=Field(min_length=1,max_length=160)
    title:str=Field(min_length=1,max_length=40)
    evidence:list[Evidence]=Field(min_length=1,max_length=8)


class Course(Referenced):
    start_at:datetime
    end_at:datetime
    location:str=Field(min_length=1,max_length=40)
    @model_validator(mode='after')
    def valid_times(self):
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:raise ValueError('Timezone required')
        if self.end_at<=self.start_at:raise ValueError('Course end must follow start')
        return self


class Important(Referenced):
    due_at:datetime|None=None
    action:str=Field(min_length=1,max_length=60)
    reason:str=Field(min_length=1,max_length=160)
    status:Literal['pending','completed','cancelled']='pending'
    @model_validator(mode='after')
    def valid_times(self):
        if self.due_at is not None and self.due_at.tzinfo is None:raise ValueError('Timezone required')
        return self


class Emergency(Referenced):
    severity:Literal['emergency']
    action:str=Field(min_length=1,max_length=80)
    reason:str=Field(min_length=1,max_length=200)
    start_at:datetime
    end_at:datetime
    status:Literal['active','resolved','cancelled']='active'
    @model_validator(mode='after')
    def valid_times(self):
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:raise ValueError('Timezone required')
        if self.end_at<=self.start_at or self.end_at-self.start_at>timedelta(hours=8):
            raise ValueError('Emergency interval must be positive and at most 8 hours; reassess before extending')
        return self


class Publish(Strict):
    base_revision:int=Field(ge=0)
    date:date
    courses_today:list[Course]=Field(max_length=24,validation_alias=AliasChoices('courses_today','courses'))
    # Muse ranks these by actual importance; backend never invents priority labels.
    important_events:list[Important]=Field(default_factory=list,max_length=2)
    live_activities:list[Emergency]=Field(default_factory=list,max_length=1)
    maintenance_status:Literal['ok','partial']='ok'
    @model_validator(mode='after')
    def unique_items(self):
        for items in (self.courses_today,self.important_events,self.live_activities):
            ids=[i.id for i in items]
            if len(ids)!=len(set(ids)):raise ValueError('Duplicate item ID')
        for course in self.courses_today:
            if course.start_at.astimezone(LOCAL).date()!=self.date:raise ValueError('Course starts outside plan date')
        for item in self.important_events:
            if item.due_at is not None and item.due_at.astimezone(LOCAL).date()!=self.date:
                raise ValueError('Dated important event must be due on plan date')
        return self


class Heartbeat(Strict):
    base_revision:int=Field(ge=1)
    maintenance_status:Literal['ok','partial']='ok'


def stored(c):
    row=c.execute('SELECT * FROM apple_dashboard WHERE singleton=1').fetchone()
    return dict(row) if row else None


def verify_evidence(c,body):
    for item in [*body.courses_today,*body.important_events,*body.live_activities]:
        for ref in item.evidence:
            row=c.execute('SELECT hash,payload FROM events WHERE event_id=?',(ref.event_id,)).fetchone()
            if row is None or row['hash']!=ref.hash or row['payload']=='null':
                raise HTTPException(409,'Unknown, mismatched or unavailable evidence event')


@router.post('/dashboard',dependencies=[Depends(authenticate)],operation_id='publish_apple_dashboard',
    description='Muse maintains today Apple plan after reading all pending events. Full replacement with optimistic base_revision, stable item IDs and exact source event hashes. Up to two important events, only emergency Live Activities. No device push is performed by this tool.')
def publish_apple_dashboard(body:Publish):
    if body.date!=datetime.now(LOCAL).date():raise HTTPException(422,'Publish only the current Asia/Shanghai date')
    content=body.model_dump(mode='json',exclude={'base_revision','maintenance_status'})
    fingerprint=digest(content)
    with connection() as c:
        c.execute('BEGIN IMMEDIATE');old=stored(c)
        verify_evidence(c,body)
        unchanged=old is not None and old['content_hash']==fingerprint
        if not unchanged and body.base_revision!=(old['revision'] if old else 0):
            raise HTTPException(409,'Apple plan changed; read /v1/apple/state and reconcile')
        revision=old['revision'] if unchanged else (old['revision']+1 if old else 1)
        checked=now()
        c.execute('INSERT OR REPLACE INTO apple_dashboard VALUES (1,?,?,?,?,?)',
            (revision,canonical(content),fingerprint,checked,body.maintenance_status))
        c.execute('INSERT OR REPLACE INTO source_state VALUES (?,?,?)',('apple','courses_explicit_revision',str(revision)))
    return {'revision':revision,'changed':not unchanged,'maintained_at':checked,'device_push_sent':False}


@router.post('/heartbeat',dependencies=[Depends(authenticate)],operation_id='maintain_apple_dashboard',
    description='After a successful ten-minute check, renew the unchanged current plan. Revision must match. Set partial when evidence or processing is incomplete. Never claim successful maintenance merely because the endpoint was called.')
def maintain_apple_dashboard(body:Heartbeat,quiet:bool=Query(False)):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE');old=stored(c)
        if old is None or body.base_revision!=old['revision']:raise HTTPException(409,'Apple plan missing or changed')
        if json.loads(old['content'])['date']!=datetime.now(LOCAL).date().isoformat():
            raise HTTPException(409,'New day requires publishing today plan')
        checked=now()
        c.execute('UPDATE apple_dashboard SET maintained_at=?,maintenance_status=? WHERE singleton=1',
            (checked,body.maintenance_status))
    if quiet:return Response(status_code=204)
    return {'revision':old['revision'],'changed':False,'maintained_at':checked,'device_push_sent':False}


@router.get('/state',dependencies=[Depends(authenticate)],operation_id='read_apple_maintenance_state',
    description='Muse-only complete current Apple plan and revision for safe reconciliation. Reading this never acknowledges source events.')
def read_apple_maintenance_state():
    with connection() as c:
        old=stored(c)
    if old is None:return {'revision':0,'plan':None,'maintained_at':None}
    return {'revision':old['revision'],'plan':json.loads(old['content']),
        'maintained_at':old['maintained_at'],'maintenance_status':old['maintenance_status']}


def view():
    clock=datetime.now(LOCAL)
    with connection() as c:
        old=stored(c);sources=source_availability(c)
        confirmed=c.execute("SELECT value FROM source_state WHERE source='apple' AND key='courses_explicit_revision'").fetchone()
    today=clock.date().isoformat()
    plan=json.loads(old['content']) if old else {}
    age=(clock-datetime.fromisoformat(old['maintained_at'])).total_seconds() if old else None
    stale=age is None or age>STALE_SECONDS
    same_day=plan.get('date')==today
    courses_unconfirmed=bool(old and same_day and not plan.get('courses_today') and (not confirmed or confirmed[0]!=str(old['revision'])))
    status='awaiting_muse' if not old else 'awaiting_today_plan' if not same_day else 'stale' if stale else 'partial' if old['maintenance_status']=='partial' else 'ready'
    courses=[]
    for item in plan.get('courses_today',[]) if same_day else []:
        start=datetime.fromisoformat(item['start_at']).astimezone(LOCAL)
        end=datetime.fromisoformat(item['end_at']).astimezone(LOCAL)
        if end>clock:
            courses.append({'id':item['id'],'title':item['title'],'start_at':item['start_at'],
                'end_at':item['end_at'],'time':start.strftime('%H:%M'),'location':item['location'],
                'status':'in_progress' if start<=clock else 'upcoming'})
    # Sort by the actual instant, not ISO strings with potentially different offsets.
    courses.sort(key=lambda x:datetime.fromisoformat(x['start_at']))
    important=[]
    for item in plan.get('important_events',[]) if same_day else []:
        if item['status']!='pending':continue
        important.append({k:item[k] for k in ('id','title','due_at','action')})
    emergencies=[]
    for item in plan.get('live_activities',[]):
        if (item['severity']=='emergency' and item['status']=='active'
            and datetime.fromisoformat(item['start_at'])<=clock<datetime.fromisoformat(item['end_at'])
            and not stale and old['maintenance_status']=='ok'):
            emergencies.append({k:item[k] for k in ('id','title','severity','action','start_at','end_at')})
    issues=[{'source':name,'state':s['state'],'reason':s['reason']} for name,s in sources.items()
            if not s['available'] or s['state'] in ('degraded','stale')]
    cx=sources.get('chaoxing',{}).get('details',{}).get('components',{})
    media_only=(len(issues)==1 and issues[0]['source']=='chaoxing' and cx.get('media',{}).get('available') is False
        and all(v.get('available') for k,v in cx.items() if k!='media' and v.get('enabled',True)))
    health_hint='附件缺失' if media_only else '来源异常' if issues else ''
    freshness=('等待 Muse 维护' if status=='awaiting_muse' else '等待今日安排' if status=='awaiting_today_plan'
        else '信息已过期' if status=='stale' else '部分信息待核实' if status=='partial' else '已核对')
    if issues:freshness+=' · 来源异常'
    if old:freshness+=' '+datetime.fromisoformat(old['maintained_at']).astimezone(LOCAL).strftime('%H:%M')
    return {'date':today,'timezone':'Asia/Shanghai','revision':old['revision'] if old else 0,
        'maintained_at':old['maintained_at'] if old else None,
        'valid_until':(datetime.fromisoformat(old['maintained_at'])+timedelta(seconds=STALE_SECONDS)).isoformat() if old else None,
        'status':status,'stale':stale,'courses':courses[:2],
        'courses_need_publication':courses_unconfirmed,'remaining_courses_count':max(0,len(courses)-2),'important_events':important[:2],
        'live_activities':emergencies,'source_issues':issues,'health_hint':health_hint,'display_freshness':freshness,
        'view_updated_at':clock.isoformat(),'live_activity_transport':'data_only_not_APNs'}


def compact_location(value):
    # Omit only the known default campus prefix; never trim a similarly named building.
    value=re.sub(r'^(?:屏峰校区|屏峰(?=[\s·・/｜|,，:：—-]))\s*[·・/｜|,，:：—-]*\s*','',value).strip()
    return value or '地点待核实'


@router.get('/dashboard',dependencies=[Depends(authenticate_apple)],operation_id='get_apple_dashboard',
    description='Compact read-only smallest-widget view: at most two not-yet-ended courses today (time/location), at most two Muse-selected important events. Ended classes are filtered at request time. Explicit freshness. Apple token cannot access messages or write plans.')
def get_apple_dashboard():
    result=view()
    result['display_title']='今日剩余课程'
    for item in result['courses']:item['location']=compact_location(item['location'])
    for n in (1,2):
        courses=result['courses'];important=result['important_events']
        result['display_course_'+str(n)]=(('进行中 ' if courses[n-1]['status']=='in_progress' else courses[n-1]['time']+' ')+courses[n-1]['location']) if len(courses)>=n else (('课程安排待 Muse 补齐' if result['courses_need_publication'] else '今日课程已结束或无课') if n==1 and result['status']=='ready' else '')
        result['display_important_'+str(n)]=important[n-1]['title'] if len(important)>=n else ''
    result['display_date']=datetime.now(LOCAL).strftime('%m.%d')
    verified_empty=(result['status']=='ready' and not result['courses_need_publication'])
    result['display_course_1_title']=result['courses'][0]['title'] if result['courses'] else '上完了' if verified_empty else '待补齐' if result['courses_need_publication'] else '待同步'
    result['display_course_2_title']=result['courses'][1]['title'] if len(result['courses'])>1 else ''
    for n in (1,2):
        courses=result['courses']
        if len(courses)>=n:
            item=courses[n-1]
            result['display_course_'+str(n)+'_time']='进行中' if item['status']=='in_progress' else item['time']
            result['display_course_'+str(n)+'_location']=item['location']
        else:
            result['display_course_'+str(n)+'_time']=('待补齐' if result['courses_need_publication'] else '无剩余课' if result['status']=='ready' else '待同步') if n==1 else ''
            result['display_course_'+str(n)+'_location']=''
    short='过期' if result['stale'] else '待补齐' if result['courses_need_publication'] else result['health_hint'] if result['health_hint'] else '部分未确认' if result['status']=='partial' else '待同步' if result['status']!='ready' else '更新'
    if result['maintained_at']:short+=' '+datetime.fromisoformat(result['maintained_at']).astimezone(LOCAL).strftime('%H:%M')
    result['display_widget_footer']=short
    result['display_courses_more']='另有 '+str(result['remaining_courses_count'])+' 节' if result['remaining_courses_count'] else ''
    return result


@router.get('/watch',dependencies=[Depends(authenticate_apple)],operation_id='get_apple_watch_dashboard',
    description='Same bounded time/location and important-event view for Watch; no raw messages, evidence payloads or credentials.')
def get_apple_watch_dashboard():
    return get_apple_dashboard()


@router.get('/live-activities',dependencies=[Depends(authenticate_apple)],operation_id='get_apple_emergency_activities',
    description='Only active, unexpired emergency items from a fresh Muse plan. Time-sensitive ordinary events never appear. Consumers must end previously displayed IDs when absent or past end_at/valid_until; this endpoint does not send ActivityKit pushes.')
def get_apple_emergency_activities():
    result=view()
    return {k:result[k] for k in ('revision','status','stale','maintained_at','valid_until','live_activities','live_activity_transport')}
