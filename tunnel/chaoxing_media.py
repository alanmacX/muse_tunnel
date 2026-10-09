"""Read-only recent notice attachments and current-course material files."""
import asyncio,html,json,re,os,socket,ipaddress,tempfile,subprocess,zipfile
from datetime import datetime,timedelta
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit,urljoin,quote
import xml.etree.ElementTree as ET
import httpx
from api import DB,LOCAL,connection,digest,now
from store import ingest,state_get,state_set
from media import archive_file,MAX_BYTES
from vendor import chaoxing


def trusted_url(url,auth=False):
    p=urlsplit(url)
    if p.scheme=='http':url='https://'+url.split('://',1)[1];p=urlsplit(url)
    host=p.hostname or ''
    allowed=host.endswith('.chaoxing.com') or host=='chaoxing.com'
    if not auth:allowed=allowed or host in {'d0.cldisk.com','d1.cldisk.com','d2.cldisk.com','p.cldisk.com','sharewh3.xuexi365.com'}
    if p.scheme!='https' or not allowed or p.username or p.password or p.port not in (None,443):raise ValueError('untrusted_attachment_host')
    for info in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(info[4][0]).is_global:raise ValueError('non_public_attachment_address')
    return url


async def authenticated_get(client,url,params=None):
    for _ in range(6):
        url=trusted_url(url,auth=True)
        r=await client.get(url,params=params,follow_redirects=False);params=None
        if r.is_redirect:url=urljoin(str(r.url),r.headers['location']);continue
        r.raise_for_status();return r
    raise ValueError('too_many_source_redirects')


async def download(svc,url,name,authenticated=False):
    root=DB.parent/'chaoxing-files';root.mkdir(mode=0o700,parents=True,exist_ok=True)
    name=re.sub(r'[^\w. ()-]','_',name)[:180] or 'attachment.bin'
    folder=Path(tempfile.mkdtemp(dir=root));path=folder/name
    try:
        async with httpx.AsyncClient(timeout=40,follow_redirects=False,headers={'Referer':'https://pan-yz.chaoxing.com/'}) as bare:
            for _ in range(6):
                url=trusted_url(url,auth=authenticated)
                client=svc._client if authenticated else bare
                async with client.stream('GET',url,follow_redirects=False) as r:
                    if r.is_redirect:
                        target=urljoin(str(r.url),r.headers['location'])
                        # Session cookies must never be forwarded to the storage CDN.
                        target=trusted_url(target)
                        authenticated=authenticated and (urlsplit(target).hostname or '').endswith('.chaoxing.com')
                        url=target;continue
                    r.raise_for_status()
                    if 'text/html' in r.headers.get('content-type','') and 'attachment' not in r.headers.get('content-disposition',''):
                        raise ValueError('download_returned_html')
                    if int(r.headers.get('content-length','0'))>MAX_BYTES:raise ValueError('attachment_too_large')
                    size=0
                    with path.open('wb') as f:
                        path.chmod(0o600)
                        async for chunk in r.aiter_bytes():
                            size+=len(chunk)
                            if size>MAX_BYTES:raise ValueError('attachment_too_large')
                            f.write(chunk)
                    asset=archive_file(str(path.resolve()),[root])
                    if asset is None:raise ValueError('attachment_archive_failed')
                    text=extract_text(path)
                    if text:
                        asset['extracted_text']=text[:100000]
                        asset['text_truncated']=len(text)>100000
                    return asset
            raise ValueError('too_many_attachment_redirects')
    finally:
        path.unlink(missing_ok=True);folder.rmdir()


def extract_text(path):
    suffix=path.suffix.lower()
    if suffix=='.doc':
        try:
            r=subprocess.run(['antiword',str(path)],capture_output=True,timeout=20)
            return r.stdout.decode('utf-8','replace') if r.returncode==0 else None
        except (OSError,subprocess.TimeoutExpired):return None
    if suffix in ('.docx','.pptx'):
        try:
            with zipfile.ZipFile(path) as z:
                selected=[n for n in z.namelist() if n=='word/document.xml' or re.fullmatch(r'ppt/slides/slide\d+\.xml',n)]
                if sum(z.getinfo(n).file_size for n in selected)>16000000:return None
                result=[]
                for name in sorted(selected):
                    tree=ET.fromstring(z.read(name))
                    result.append('\n'.join(e.text for e in tree.iter() if e.tag.endswith('}t') and e.text))
                return '\n\n'.join(result)
        except (OSError,ValueError,zipfile.BadZipFile,ET.ParseError):return None
    return None


class Materials(HTMLParser):
    def __init__(self,text):
        super().__init__(convert_charrefs=True);self.rows=[];self.row=None;self.depth=0;self.time=False
        self.feed(text)
        m=re.search(r'id="totalPages"\s+value="(\d+)"',text);self.pages=int(m[1]) if m else None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='ul':
            if self.row is not None:self.depth+=1
            elif 'dataBody_td' in a.get('class','').split():self.row={'name':a.get('dataname',''),'id':a.get('id',''),'type':a.get('type',''),'modified':''};self.depth=1
        if self.row is not None:
            if tag=='li':self.time='dataBody_time_stu' in a.get('class','')
            if 'onclick' in a and 'toOpen(' in a['onclick']:
                values=re.findall(r"'([^']*)'",a['onclick'])
                if len(values)>6:self.row['enc']=values[6]
    def handle_endtag(self,tag):
        if tag=='li':self.time=False
        if tag=='ul' and self.row is not None:
            self.depth-=1
            if self.depth==0:self.rows.append(self.row);self.row=None
    def handle_data(self,data):
        if self.row is not None and self.time:self.row['modified']+=data.strip()


def modified_time(value):
    value=re.sub(r'\s+',' ',value.strip())
    try:
        if re.match(r'^\d{4}-',value):return datetime.fromisoformat(value).replace(tzinfo=LOCAL)
        d=datetime.strptime(str(datetime.now(LOCAL).year)+'-'+value,'%Y-%m-%d %H:%M').replace(tzinfo=LOCAL)
        if d>datetime.now(LOCAL)+timedelta(days=1):d=d.replace(year=d.year-1)
        return d
    except ValueError:return None


async def course_materials(svc,course,cutoff):
    cid,clazz,cpi,name=course;folders=[('0','')];seen=set();files=[]
    while folders:
        folder,enc=folders.pop(0)
        if folder in seen:continue
        seen.add(folder)
        if len(seen)>200:raise ValueError('material_folder_limit')
        page=1;page_hashes=set()
        while True:
            params={'courseid':cid,'clazzid':clazz,'cpi':cpi,'ut':'s','page':page}
            if folder!='0':params.update(dataId=folder,enc=enc)
            r=await authenticated_get(svc._client,'https://mooc2-ans.chaoxing.com/mooc2-ans/coursedata/stu-datalist',params)
            parsed=Materials(r.text)
            if parsed.pages is None:raise ValueError('material_response_unverified')
            fingerprint=digest(parsed.rows)
            if page>1 and fingerprint in page_hashes:raise ValueError('material_pagination_not_advancing')
            page_hashes.add(fingerprint)
            for item in parsed.rows:
                if not item['id'].isdigit():continue
                if item['type'] in ('folder','0'):
                    folders.append((item['id'],item.get('enc','')));continue
                stamp=modified_time(item['modified'])
                if stamp and stamp<cutoff:continue
                identity='material:'+cid+':'+clazz+':'+item['id']
                cache=state_get('chaoxing',identity)
                if cache:
                    cached=json.loads(cache)
                    if cached.get('modified')==item['modified'] and (DB.parent/'media'/cached['asset']['asset_id']).exists():
                        asset=cached['asset']
                    else:cache=None
                if not cache:
                    params={'dataId':item['id'],'classId':clazz,'cpi':cpi,'courseId':cid,'ut':'s'}
                    url=str(httpx.URL('https://mooc1.chaoxing.com/coursedata/downloadData',params=params))
                    try:
                        asset=await download(svc,url,item['name'],True)
                        state_set('chaoxing',identity,json.dumps({'modified':item['modified'],'asset':asset},ensure_ascii=False))
                    except Exception as e:asset={'status':'not_downloaded','reason':str(e) if isinstance(e,ValueError) else type(e).__name__}
                payload={'source':'chaoxing','kind':'course_material','source_id':item['id'],'course_id':cid,
                    'conversation':name,'title':item['name'],'modified_at_display':item['modified'],
                    'date_basis':'source_display_with_year_inferred_if_missing','sent_at':stamp.isoformat() if stamp else None,
                    'attachments':[asset],'attachment_status':asset['status'],'synthetic':False}
                ingest('chaoxing',identity,payload,payload['sent_at']);files.append(payload)
            if page>=parsed.pages:break
            page+=1
            if page>100:raise ValueError('material_page_limit')
    return files


def notice_references(data):
    refs=[]
    images=data.get('imgs') or data.get('img_data') or []
    if isinstance(images,str):
        try:images=json.loads(images)
        except ValueError:images=[images]
    if isinstance(images,dict):images=[images]
    for item in images:
        if isinstance(item,str):refs.append((item,'image.jpg'))
        elif isinstance(item,dict):
            url=item.get('imgUrl') or item.get('url') or item.get('imgurl')
            if url:refs.append((url,item.get('name') or 'image.jpg'))
    for url in re.findall(r'<img[^>]+src=["\x27]([^"\x27]+)',data.get('rtf_content') or '',re.I):refs.append((html.unescape(url),'image.jpg'))
    attachments=data.get('attachment') or []
    if isinstance(attachments,str):
        try:attachments=json.loads(attachments)
        except ValueError:attachments=[]
    for a in attachments:
        if not isinstance(a,dict):continue
        cloud=a.get('att_clouddisk') or {}
        url=cloud.get('downPath') or cloud.get('downloadUrl')
        if not url and cloud.get('fileId'):url='https://d0.cldisk.com/download/'+quote(str(cloud['fileId']),safe='')
        if url:refs.append((url,cloud.get('name') or cloud.get('fileName') or 'attachment.bin'))
    return list(dict.fromkeys(refs))


def unsupported_notice_attachments(data):
    items=data.get('attachment') or []
    if isinstance(items,str):
        try:items=json.loads(items)
        except ValueError:return 1
    if not isinstance(items,list):return 1
    return sum(not isinstance(a,dict) or not isinstance(a.get('att_clouddisk'),dict)
               or not any(a['att_clouddisk'].get(k) for k in ('downPath','downloadUrl','fileId')) for a in items)


async def collect_media(svc,data,force=False):
    cutoff=datetime.now(LOCAL)-timedelta(days=7)
    with connection() as c:
        rows=c.execute("SELECT payload FROM events e WHERE source='chaoxing' AND source_id LIKE 'inbox:%' AND seq=(SELECT MAX(seq) FROM events WHERE source=e.source AND source_id=e.source_id)").fetchall()
    notices=[json.loads(r[0]) for r in rows if json.loads(r[0]) is not None]
    recent=[p for p in notices if p.get('sent_at') and datetime.fromisoformat(p['sent_at'])>=cutoff]
    signature=digest([(p['source_id'],p['title'],p['text'],p['raw'].get('rtf_content')) for p in recent])
    last=state_get('chaoxing','media_last_attempt') or state_get('chaoxing','media_last_success')
    previous=json.loads(state_get('chaoxing','media_status') or '{}')
    delay=1800 if previous.get('available') else 300
    if not force and last and (datetime.now(LOCAL).timestamp()-float(last)<delay) and state_get('chaoxing','media_notice_signature')==signature:
        return json.loads(state_get('chaoxing','media_status') or '{}')
    failures=[];files=[];images=0
    for notice in recent:
        try:
            r=await authenticated_get(svc._client,'https://notice.chaoxing.com/pc/notice/'+quote(notice['source_id'],safe='')+'/getNoticeDetail',{'sendTag':notice['raw'].get('sendTag',0)})
            value=r.json()
            if not value.get('status') or not isinstance(value.get('msg'),dict):raise ValueError('notice_detail_unverified')
            detail=value['msg'];assets=[]
            for url,name in notice_references(detail):
                try:
                    key='notice-file:'+digest([notice['source_id'],url,name])
                    cached=json.loads(state_get('chaoxing',key) or 'null')
                    if cached and (DB.parent/'media'/cached['asset_id']).exists():asset=cached
                    else:
                        asset=await download(svc,url,name)
                        state_set('chaoxing',key,json.dumps(asset,ensure_ascii=False))
                    assets.append(asset)
                except Exception as e:assets.append({'status':'not_downloaded','filename':name,'reason':str(e) if isinstance(e,ValueError) else type(e).__name__})
            if unsupported_notice_attachments(detail):
                assets.append({'status':'not_downloaded','reason':'unsupported_attachment_reference'})
            if any(a['status']!='available' for a in assets):
                failures.append({'notice_id':notice['source_id'],'reason':'incomplete_attachments'})
            images+=sum(a.get('mime_type','').startswith('image/') for a in assets)
            payload={'source':'chaoxing','kind':'notice_detail','source_id':notice['source_id'],
                'title':notice['title'],'text':detail.get('content') or notice['text'],
                'rtf_content':detail.get('rtf_content'),'sent_at':notice['sent_at'],
                'attachments':assets,'attachment_status':'available' if assets and all(a['status']=='available' for a in assets) else 'incomplete' if assets else 'none_detected',
                'synthetic':False}
            ingest('chaoxing','notice-detail:'+notice['source_id'],payload,payload['sent_at'])
        except Exception as e:failures.append({'notice_id':notice['source_id'],'reason':str(e) if isinstance(e,ValueError) else type(e).__name__})
    courses=[]
    for channel in data.get('channelList',[]):
        if str(channel.get('cataid'))!='100000002':continue
        content=channel.get('content') or {};row=((content.get('course') or {}).get('data') or [{}])[0]
        if str(content.get('isFiled','0'))=='1' or not chaoxing.ChaoxingService._course_is_current(content,row,chaoxing.ChaoxingService._current_semester_cutoff(datetime.now(LOCAL))):continue
        cid=str(row.get('id') or '');clazz=str(channel.get('key') or '')
        if cid and clazz:courses.append((cid,clazz,str(channel.get('cpi') or ''),content.get('name') or cid))
    for course in courses:
        try:files.extend(await course_materials(svc,course,cutoff))
        except Exception as e:failures.append({'course_id':course[0],'reason':str(e) if isinstance(e,ValueError) else type(e).__name__})
    if data.get('hasMore'):failures.append({'reason':'course_list_pagination_required'})
    for f in files:
        if f['attachment_status']!='available':failures.append({'material_id':f['source_id'],'reason':'attachment_not_downloaded'})
    result={'notices_checked':len(recent),'courses_checked':len(courses),'recent_materials':len(files),
            'downloaded_materials':sum(f['attachment_status']=='available' for f in files),'notice_images':images,'failures':failures,
            'available':not failures and all(f['attachment_status']=='available' for f in files),'reason':'read_ok' if not failures else 'partial_media_collection',
            'window_start':cutoff.isoformat()}
    state_set('chaoxing','media_status',json.dumps(result,ensure_ascii=False))
    state_set('chaoxing','media_last_attempt',str(datetime.now(LOCAL).timestamp()))
    state_set('chaoxing','media_notice_signature',signature)
    if result['available']:
        state_set('chaoxing','media_last_success',str(datetime.now(LOCAL).timestamp()));state_set('chaoxing','media_notice_signature',signature)
    return result
