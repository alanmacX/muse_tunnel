"""Real source collectors. All observed content is archived before interpretation."""
import asyncio,base64,ipaddress,json,logging,os,re,sqlite3,subprocess,time
from datetime import datetime,timezone
from pathlib import Path
from api import init,digest,now,connection,DB
from store import ingest,refresh_daily,update_status,state_get,state_set
from vendor import chaoxing,dingtalk

# Legacy QR/client log messages can contain session data; do not emit them.
logging.getLogger('chaoxing').disabled=True
logging.getLogger('dingtalk').disabled=True
SESSION_DB=os.environ.get('MUSE_SESSION_DB','data/sessions.db')


from sanitize import clean
from media import attachments,archive_file
from chaoxing_media import collect_media
from chaoxing_health import ReadIssue, check_login, diagnostic, json_response, read_request

def timestamp(ms):
    try:return datetime.fromtimestamp(float(ms)/1000,tz=timezone.utc).isoformat()
    except (TypeError,ValueError,OverflowError):return None


def source_snapshot():
    key_path=Path(os.environ.get('DINGTALK_KEY_FILE','/home/ubuntu/.config/DingTalk/.aes_key'))
    os.environ['DINGTALK_AES_KEY']=key_path.read_text().strip()
    dingtalk._key_cache['key']=None
    source=Path(dingtalk._discover_db_path())
    if not source.is_file():raise FileNotFoundError('source_not_found')
    wal=Path(str(source)+'-wal')
    def signature():
        return [(p.stat().st_size,p.stat().st_mtime_ns) if p.exists() else None for p in (source,wal)]
    for attempt in range(3):
        before=signature()
        output=dingtalk.decrypt_db_to_tmp()
        if before==signature():return output
        time.sleep(.25)
    raise RuntimeError('source_snapshot_changing')


def client_transport():
    """Observable client TCP health, not proof of authentication or complete sync."""
    try:
        pids=set(Path('/sys/fs/cgroup/system.slice/dingtalk.service/cgroup.procs').read_text().split())
        result=subprocess.run(['ss','-H','-tnp','state','established'],capture_output=True,text=True,timeout=5,check=True)
        count=0
        for row in result.stdout.splitlines():
            fields=row.split()
            if len(fields)<4 or not pids.intersection(re.findall(r'pid=(\d+)',row)):continue
            try:peer=ipaddress.ip_address(fields[3].rsplit(':',1)[0].strip('[]'))
            except ValueError:continue
            if not peer.is_loopback:count+=1
        return {'available':count>0,'reason':'remote_tcp_connected' if count else 'no_remote_tcp_connection',
                'remote_established_tcp':count,'meaning':'transport_signal_only_not_authenticated_sync'}
    except Exception:return {'available':False,'reason':'transport_state_unknown','meaning':'not_authenticated_sync'}


def collect_dingtalk():
    source='dingtalk'
    try:
        alive=subprocess.run(['systemctl','is-active','--quiet','dingtalk'],capture_output=True).returncode==0
        if not alive:raise RuntimeError('client_not_running')
        output=source_snapshot()
        with sqlite3.connect(f'file:{output}?mode=ro',uri=True) as c:
            c.row_factory=sqlite3.Row
            tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tbmsg_%'")]
            if not tables:raise RuntimeError('no_message_tables')
            normalized={(str(m['cid']),str(m['mid'])):m for m in dingtalk.query_new_messages(c,0)}
            count=0;inserted=0;latest=0
            for table in tables:
                if not re.fullmatch(r'tbmsg_[0-9]+',table):raise RuntimeError('unexpected_table_name')
                columns={r[1] for r in c.execute(f'PRAGMA table_info("{table}")')}
                if not {'mid','cid','content','contentType','createdAt'}.issubset(columns):
                    raise RuntimeError('message_schema_changed')
                for row in c.execute(f'SELECT * FROM "{table}"'):
                    raw=clean(dict(row));mid=raw.get('mid');cid=str(raw.get('cid',''))
                    if mid is None or not cid:raise RuntimeError('message_identity_missing')
                    with connection() as ledger:
                        retired=ledger.execute('''SELECT r.raw_hash FROM retention r JOIN events e ON e.seq=r.seq
                          WHERE e.source=? AND e.source_id=? AND r.state IN ('quarantined','purged')
                          AND e.seq=(SELECT MAX(seq) FROM events WHERE source=? AND source_id=?)''',
                          (source,cid+':'+str(mid),source,cid+':'+str(mid))).fetchone()
                    if retired and retired['raw_hash']==digest(raw):
                        count+=1;latest=max(latest,int(raw.get('createdAt') or 0))
                        continue
                    parsed=clean(normalized.get((cid,str(mid)),{}))
                    sent=timestamp(raw.get('createdAt'))
                    cache_root=Path(os.environ.get('DINGTALK_DB_SOURCE','/home/ubuntu/.config/DingTalk'))
                    media_roots=[folder for name in ('ImageFiles','AudioFiles','VideoFiles','QuickTransFiles','resource_cache')
                                 for folder in cache_root.glob('*/'+name)]
                    media_roots.append(DB.parent/'imports')
                    files=attachments(raw.get('content'), media_roots)
                    imported=state_get(source,'file:'+cid+':'+str(mid))
                    if imported:
                        imported=json.loads(imported)
                        asset=archive_file(imported['filepath'],media_roots)
                        if asset:
                            if files:
                                files[0].update(asset)
                            else:
                                files.append(asset)
                    payload={'source':source,'kind':'message','source_id':str(mid),'conversation_id':cid,
                             'conversation':parsed.get('conversation_title') or cid,
                             'sent_at':sent,'text':parsed.get('text') or '',
                             'content_type':raw.get('contentType'),'raw':raw,'parsed':parsed,
                             'attachments':files,
                             'attachment_status':('available' if files and all(f['status']=='available' for f in files)
                                                  else 'incomplete' if files else 'none_detected'),
                             'synthetic':False}
                    inserted+=ingest(source,cid+':'+str(mid),payload,sent)
                    count+=1;latest=max(latest,int(raw.get('createdAt') or 0))
        transport=client_transport()
        login_lost=state_get(source,'login_required_since')
        if login_lost and timestamp(latest) and timestamp(latest)>login_lost:
            state_set(source,'login_required_since','')
            login_lost=None
        update_status(source,'degraded' if login_lost else 'available_local_cache' if transport['available'] else 'degraded',True,
                      'login_required' if login_lost else 'client_cache_readable' if transport['available'] else transport['reason'],
                      {'observed_messages':count,'new_versions':inserted,'latest_source_message_at':timestamp(latest),
                       'client_running':alive,'upstream_connection':'unknown','coverage':'local_cache_only',
                       'components':{'cache':{'available':True,'reason':'read_ok'},'transport':transport,
                                     'authenticated_sync':{'available':False if login_lost else None,
                                                           'reason':'login_required' if login_lost else 'not_verified'}},
                       'attachments':'authenticated_files_when_cached_otherwise_explicitly_missing','client_version':'8.1.0.6021101'})
        return count,inserted
    except Exception as e:
        reason=str(e) if isinstance(e,(FileNotFoundError,RuntimeError)) and str(e) in {
            'source_not_found','source_snapshot_changing','client_not_running','no_message_tables',
            'unexpected_table_name','message_schema_changed','message_identity_missing'} else type(e).__name__
        update_status(source,'unavailable',False,reason,{'coverage':'local_cache_only','upstream_connection':'unknown'})
        return None,None
    finally:
        Path(dingtalk.DECRYPTED_DB_PATH).unlink(missing_ok=True)


async def roaming(svc,params,conv):
    suffix='@conference.easemob.com' if conv['is_group'] else '@easemob.com'
    url='https://a1-vip6.easecdn.com/cx-dev/cxstudy/users/'+params['tuid']+'/messageroaming'
    newest=[];cursor=-1;seen_pages=set();page_limit=200
    checkpoint=state_get('chaoxing','conversation:'+conv['id'])
    anchor_found=False;complete=False
    for page in range(100):
        r=await svc._client.post(url,json={'queue':conv['id']+suffix,'start':cursor,'end':-1,'count':page_limit},
                                headers={'Authorization':'Bearer '+params['token'],'Content-Type':'application/json'})
        r.raise_for_status();data=r.json()
        if not isinstance(data.get('data'),dict) or 'msgs' not in data['data']:
            raise RuntimeError('roaming_response_changed')
        messages=data['data']['msgs'] or []
        if not isinstance(messages,list):raise RuntimeError('roaming_response_changed')
        if not messages:complete=True;break
        ids=[]
        for raw in messages:
            encoded=raw.get('msg','');meta={}
            try:meta=chaoxing._decode_meta(base64.b64decode(encoded))
            except Exception:pass
            mid=str(meta.get('id') or raw.get('msgId') or raw.get('id') or '')
            if not mid:
                mid='fallback-'+digest(clean(raw))
            ids.append(mid)
            sent=timestamp(raw.get('msgTime')) or timestamp(meta.get('timestamp'))
            user=raw.get('fromUser') or {}
            parsed=chaoxing._decode_roaming_message(encoded,conv,
                      raw_sender_uid=str(raw.get('from') or user.get('uid') or ''),
                      raw_sender_name=user.get('nickname') or user.get('name'),
                      raw_msg_time_ms=float(raw['msgTime']) if raw.get('msgTime') else None) if encoded else None
            payload={'source':'chaoxing','kind':'message','conversation_id':conv['id'],'conversation':conv['name'],
                     'source_id':mid,'sent_at':sent,'text':(parsed or {}).get('text',''),
                     'raw':clean(raw),'parsed':clean(parsed),'attachment_status':'references_only','synthetic':False}
            ingest('chaoxing',conv['id']+':'+mid,payload,sent)
            newest.append(mid)
            if checkpoint==mid:anchor_found=True
        page_hash=digest(ids)
        if page_hash in seen_pages:
            raise RuntimeError('history_cursor_not_advancing')
        seen_pages.add(page_hash)
        if anchor_found:complete=True;break
        # Probe actual oldest source ID as next page boundary; do not infer exhaustion from a page length.
        cursor=ids[-1]
    if not complete:raise RuntimeError('history_page_limit_reached')
    if newest:state_set('chaoxing','conversation:'+conv['id'],newest[0])
    return len(newest)


async def inbox(svc):
    seen=set();cursors=set();count=0;cursor='';newest=None;anchor_found=False
    today=datetime.now(timezone.utc).date().isoformat()
    checkpoint=state_get('chaoxing','inbox_anchor') if state_get('chaoxing','inbox_full_day')==today else None
    def finish():
        if newest:state_set('chaoxing','inbox_anchor',newest)
        if not checkpoint:state_set('chaoxing','inbox_full_day',today)
        return count
    for page in range(1,501):
        endpoint='notice_list'
        r=await read_request(svc._client,'POST','https://notice.chaoxing.com/pc/notice/getNoticeList',endpoint,page=page,
                                data={'lastValue':cursor,'type':'0','sort':'0'},
                                headers={'Referer':'https://notice.chaoxing.com/pc/notice/myNotice'})
        data=json_response(r,endpoint,page=page,code='notice_response_changed')
        # The website returns exactly {status: true} after the final nonempty page.
        if cursor and data == {'status': True}:return finish()
        container=data.get('notices')
        if not isinstance(container,dict) or not isinstance(container.get('list'),list):
            raise ReadIssue('notice_response_changed',endpoint,r.status_code,page)
        rows=container['list']
        if not rows:return finish()
        marker=digest(clean(rows))
        if marker in seen:raise ReadIssue('notice_pagination_not_advancing',endpoint,r.status_code,page)
        seen.add(marker)
        for raw in rows:
            identity=(raw.get('idCode') or raw.get('id')) if isinstance(raw,dict) else None
            if identity is None:raise ReadIssue('notice_identity_missing',endpoint,r.status_code,page)
            if newest is None:newest=str(identity)
            if checkpoint==str(identity):anchor_found=True
            sent=timestamp(raw.get('insertTime') or raw.get('createTime') or raw.get('sendTime'))
            payload={'source':'chaoxing','kind':'notice','conversation':'学习通收件箱','source_id':str(identity),
                     'sent_at':sent,'title':raw.get('title',''),'text':raw.get('content',''),
                     'raw':clean(raw),'attachment_status':'references_only','synthetic':False}
            ingest('chaoxing','inbox:'+str(identity),payload,sent);count+=1
        if anchor_found or container.get('lastPage') in (True,1,'1','true'):return finish()
        next_cursor=str(container.get('lastGetId') or '')
        if not next_cursor or next_cursor==cursor or next_cursor in cursors:
            raise ReadIssue('notice_pagination_not_advancing',endpoint,r.status_code,page)
        cursors.add(next_cursor)
        cursor=next_cursor
    raise ReadIssue('notice_page_limit_reached','notice_list',page=500)


async def coursework(svc,data):
    semaphore=asyncio.Semaphore(4)
    courses=[]
    for channel in data['channelList']:
        if str(channel.get('cataid'))!='100000002':continue
        content=channel.get('content') or {}
        rows=((content.get('course') or {}).get('data')) or []
        if not rows:continue
        course=rows[0]
        cid=str(course.get('id') or '')
        class_id=str(channel.get('key') or '')
        if not cid or not class_id:continue
        name=content.get('name') or cid
        payload={'source':'chaoxing','kind':'course','source_id':cid+':'+class_id,
                 'conversation':name,'raw':clean(channel),'synthetic':False}
        ingest('chaoxing','course:'+cid+':'+class_id,payload)
        courses.append((cid,class_id,str(channel.get('cpi') or ''),name))
    async def fetch(course):
        async with semaphore:
            cid,class_id,cpi,name=course
            try:
                r=await read_request(svc._client,'GET','https://mooc1-api.chaoxing.com/work/task-list','assignment_list',
                    params={'courseId':cid,'classId':class_id,'cpi':cpi},
                    headers={'User-Agent':chaoxing.CHAOXING_MOBILE_UA,'Referer':'https://mooc1.chaoxing.com'})
                if 'json' in r.headers.get('content-type',''):
                    json_response(r,'assignment_list',code='assignment_response_unverified')
                if not any(x in r.text for x in ('goTask','暂无作业','class="empty"')):
                    raise ReadIssue('assignment_response_unverified','assignment_list',r.status_code)
                items=chaoxing._parse_assignments_html(r.text,cid,name)
                for item in items:
                    identity='assignment:'+cid+':'+str(item['id'])
                    # A relative countdown is not a new notification every minute.
                    with connection() as c:
                        prior=c.execute('SELECT payload FROM events WHERE source=? AND source_id=? ORDER BY revision DESC LIMIT 1',('chaoxing',identity)).fetchone()
                    due=item.get('dueDate')
                    if prior:
                        old=json.loads(prior[0]).get('due_at')
                        try:
                            if old and due and abs((datetime.fromisoformat(old)-datetime.fromisoformat(due)).total_seconds())<300:due=old
                        except ValueError:pass
                    payload={'source':'chaoxing','kind':'assignment','source_id':str(item['id']),
                             'conversation':name,'course_id':cid,'title':item['title'],
                             'status':item['status'],'due_at':due,'synthetic':False}
                    ingest('chaoxing',identity,payload)
                return None,len(items)
            except Exception as e:return diagnostic(e,'coursework','assignment_list'),0
    results=await asyncio.gather(*(fetch(c) for c in courses))
    groups={}
    for issue,_ in results:
        if issue:
            key=(issue['code'],issue.get('http_status'))
            entry=groups.setdefault(key,{**issue,'affected_courses':0})
            entry['affected_courses']+=1
    return len(courses),sum(n for _,n in results),list(groups.values())


def chaoxing_course_response(response):
    check_login(response,'course_list')
    response.raise_for_status()
    data=json_response(response,'course_list',code='course_api_invalid_response')
    if isinstance(data.get('channelList'),list):return data
    raise ReadIssue('course_api_invalid_response','course_list',response.status_code)


async def collect_chaoxing():
    svc=chaoxing.ChaoxingService(SESSION_DB);source='chaoxing'
    components={'im':{'available':False,'enabled':False,'reason':'excluded_by_user'}}
    issues=[]
    details={'coverage':'inbox_coursework_recent_notice_and_course_files_im_excluded',
             'components':components,'diagnostics':[],'requires_user_login':False,
             'conversations':0,'observed_messages':0,'course_list_has_more':None,
             'history_retention':'upstream_defined',
             'attachments':'authenticated_recent_notice_and_course_files_with_explicit_failures'}
    stats={'notices':None,'courses':None,'assignments':None}
    stage,endpoint='session','session_store'

    def failure(exc, stage, endpoint):
        issue=diagnostic(exc,stage,endpoint)
        issues.append(issue)
        components[stage]={'available':False,'reason':issue['code']}

    def publish():
        complete=all(components.get(key,{}).get('available',False)
                     for key in ('courses','notices','coursework','media'))
        partial=any(components.get(key,{}).get('available',False)
                    for key in ('courses','notices','coursework','media'))
        # Put authentication first; expose at most one short list of failures.
        issues.sort(key=lambda i:i['code']!='login_required')
        login=any(i['code']=='login_required' for i in issues)
        if complete:state_set(source,'last_complete_sync',now())
        details.update(stats,diagnostics=issues[:4],diagnostic_count=len(issues),
                       requires_user_login=login,last_complete_sync=state_get(source,'last_complete_sync'))
        if issues:
            details.update(failure_stage=issues[0]['stage'],failure_endpoint=issues[0]['endpoint'])
        reason=('login_required' if login else 'authenticated_read_ok' if complete else
                'partial_collection' if partial else issues[0]['code'] if issues else 'all_components_failed')
        update_status(source,'available' if complete else 'degraded' if partial else 'unavailable',
                      partial,reason,details)

    try:
        # Use the real courses endpoint as the auth probe (legacy passport check redirects to 404).
        await svc.init(probe=False)
        if not svc._client:raise ReadIssue('login_required','session_store')
        stage,endpoint='courses','course_list'
        courses_data=None
        try:
            r=await read_request(svc._client,'GET','https://mooc1-api.chaoxing.com/mycourse/backclazzdata',endpoint,
                params={'view':'json','mcode':'','rss':1},headers={'User-Agent':chaoxing.CHAOXING_MOBILE_UA,
                'X-Requested-With':'com.chaoxing.mobile','Accept-Language':'zh_CN','Referer':'https://i.chaoxing.com'})
            courses_data=chaoxing_course_response(r)
            details['course_list_has_more']=bool(courses_data.get('hasMore'))
            components['courses']={'available':True,'reason':'read_ok'}
            if courses_data.get('hasMore'):
                failure(ReadIssue('course_list_pagination_required',endpoint,r.status_code),stage,endpoint)
        except ReadIssue as e:
            if e.code=='login_required':raise
            failure(e,stage,endpoint)
        stage,endpoint='notices','notice_list'
        try:
            stats['notices']=await inbox(svc)
            components['notices']={'available':True,'reason':'read_ok'}
        except Exception as e:
            if isinstance(e,ReadIssue) and e.code=='login_required':raise
            failure(e,stage,endpoint)
        if courses_data is not None:
            stage,endpoint='coursework','assignment_list'
            try:
                last=state_get(source,'coursework_last_success')
                cached=state_get(source,'coursework_status')
                if not courses_data.get('hasMore') and last and cached and time.time()-float(last)<900:
                    stats['courses'],stats['assignments'],component=json.loads(cached)
                    components[stage]=component.copy()
                else:
                    stats['courses'],stats['assignments'],errors=await coursework(svc,courses_data)
                    issues.extend(errors)
                    components[stage]={'available':not errors,'reason':'read_ok' if not errors else 'partial_assignment_collection'}
                    if not errors and not courses_data.get('hasMore'):
                        state_set(source,'coursework_last_success',str(time.time()))
                        state_set(source,'coursework_status',json.dumps([stats['courses'],stats['assignments'],components[stage]]))
            except Exception as e:failure(e,stage,endpoint)
            stage,endpoint='media','material_collection'
            try:
                media=await collect_media(svc,courses_data)
                failures=media.pop('failures',[])
                components[stage]={**media,'failure_count':len(failures)}
                grouped={}
                for item in failures:
                    issue=item.get('diagnostic') or diagnostic(ValueError(item.get('reason')),'media',endpoint)
                    key=(issue['endpoint'],issue['code'],issue.get('http_status'))
                    entry=grouped.setdefault(key,{**issue,'affected_items':0})
                    entry['affected_items']+=1
                issues.extend(grouped.values())
            except Exception as e:failure(e,stage,endpoint)
        else:
            for key in ('coursework','media'):
                components[key]={'available':False,'reason':'blocked_by_course_list'}
        return 0,stats['notices'],stats['assignments']
    except Exception as e:
        failure(e,stage,endpoint)
        return None,None,None
    finally:
        try:publish()
        finally:
            if svc._client:await svc._client.aclose()


async def main():
    from storage_guard import report_collection
    os.umask(0o077)
    DB.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    disk = report_collection(DB.parent)
    init()
    update_status('storage', disk['level'], disk['level']=='ok',
                  'disk_space_ok' if disk['level']=='ok' else 'disk_space_'+disk['level'], disk)
    # Independent sources: a failure in one never prevents the other collector from running.
    from zjut_collect import collect as collect_zjut
    from qq_collect import collect as collect_qq
    ding,cha,academic,qq=await asyncio.gather(asyncio.to_thread(collect_dingtalk),collect_chaoxing(),collect_zjut(),collect_qq())
    refresh_daily()
    print(json.dumps({'dingtalk_counts':ding,'chaoxing_counts':cha,'zjut_counts':academic,'qq_counts':qq,'checked_at':now()}))
if __name__=='__main__':asyncio.run(main())
