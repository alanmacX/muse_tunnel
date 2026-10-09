"""Complete term snapshots for ZJUT timetable and paginated exam arrangements."""
import base64,json,logging,time
from pathlib import Path
from Crypto.Cipher import AES
import httpx
from api import now
from sanitize import clean
from store import ingest,update_status,state_get,state_set
from vendor import zjut
logging.getLogger('httpx').setLevel(logging.WARNING)
SOURCE='zjut'

def credentials():
    p=Path('data/zjut-credentials.json');k=Path('data/zjut-encryption.key')
    if not p.exists() or not k.exists():raise RuntimeError('credentials_not_configured')
    if p.stat().st_mode&0o077 or k.stat().st_mode&0o077:raise RuntimeError('credential_permissions_unsafe')
    config=json.loads(p.read_text());blob=base64.b64decode(config['password_enc'])
    password=AES.new(k.read_bytes(),AES.MODE_GCM,nonce=blob[:16]).decrypt_and_verify(blob[32:],blob[16:32]).decode()
    return config['student_id'],password

async def fetch_exams(client,year,term):
    await client.get(zjut.BASE+zjut.EXAMS_PAGE)
    allrows=[];seen=set();total=None
    for page in range(1,501):
        r=await client.post(zjut.BASE+zjut.EXAMS,data={'xnm':year,'xqm':zjut.TERM_MAP.get(term,term),
             'queryModel.showCount':'100','queryModel.currentPage':str(page),'queryModel.sortName':'','queryModel.sortOrder':'asc'},
             headers={'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'})
        r.raise_for_status();data=r.json()
        if not isinstance(data,dict) or not isinstance(data.get('items'),list):raise RuntimeError('exam_response_changed')
        rows=data['items']
        try:reported=int(data['totalCount'])
        except (KeyError,TypeError,ValueError):raise RuntimeError('exam_total_missing')
        if total is None:total=reported
        if reported!=total:raise RuntimeError('exam_changed_during_pagination')
        marker=json.dumps(rows,sort_keys=True,ensure_ascii=False)
        if rows and marker in seen:raise RuntimeError('exam_pagination_not_advancing')
        seen.add(marker);allrows.extend(rows)
        if len(allrows)==total:return {'items':allrows,'totalCount':total}
        if not rows or len(allrows)>total:raise RuntimeError('exam_count_mismatch')
    raise RuntimeError('exam_page_limit_reached')

async def collect(force=False):
    previous=state_get(SOURCE,'last_attempt_seconds')
    if not force and previous and time.time()-float(previous)<1800:return 'cached'
    state_set(SOURCE,'last_attempt_seconds',str(time.time()))
    components={};counts={}
    try:
        sid,password=credentials()
        async with httpx.AsyncClient(follow_redirects=True,timeout=25,headers={'User-Agent':zjut.UA,'Accept-Language':'zh-CN'}) as client:
            await zjut._login(client,sid,password)
            semester=await zjut._fetch_semester(client)
            if not semester:raise RuntimeError('semester_detection_failed')
            year,term=semester['year'],semester['term'];identity=year+':'+term
            for kind in ('timetable','exams'):
                try:
                    raw=await zjut._post_query(client,zjut.SCHEDULE_PAGE,zjut.SCHEDULE,year,term) if kind=='timetable' else await fetch_exams(client,year,term)
                    if kind=='timetable' and (not isinstance(raw,dict) or not isinstance(raw.get('kbList'),list)):raise RuntimeError('timetable_response_changed')
                    parsed=zjut.parse_schedule(raw) if kind=='timetable' else zjut.parse_exams(raw)
                    # A complete snapshot is the authoritative replacement set; removals remain visible as revisions.
                    payload={'source':SOURCE,'kind':kind,'source_id':identity+':'+kind,'semester':semester,
                             'rows':clean(parsed),'raw':clean(raw),'snapshot_semantics':'complete_replacement','synthetic':False}
                    ingest(SOURCE,identity+':'+kind,payload)
                    counts[kind]=len(parsed);components[kind]={'available':True,'reason':'read_ok'}
                except Exception as e:
                    components[kind]={'available':False,'reason':str(e) if isinstance(e,RuntimeError) else type(e).__name__}
            good=sum(x['available'] for x in components.values())
            update_status(SOURCE,'available' if good==2 else 'degraded' if good else 'unavailable',bool(good),
                          'authenticated_read_ok' if good==2 else 'partial_collection',
                          {'components':components,'counts':counts,'semester':semester,'poll_interval_seconds':1800,
                           'stale_after_seconds':2400,'coverage':'current_term_timetable_and_exams','transport':'legacy_http_origin_https_cas'})
            return counts
    except Exception as e:
        reason=str(e) if isinstance(e,RuntimeError) else type(e).__name__
        update_status(SOURCE,'unavailable',False,reason,{'components':components,'poll_interval_seconds':1800,
                         'stale_after_seconds':2400,'coverage':'current_term_timetable_and_exams'})
        return 'failed'
