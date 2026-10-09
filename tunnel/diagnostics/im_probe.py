"""Read-only differential IM probe. Never prints cookie/token values or account HTML."""
import asyncio,hashlib,json,logging,re,sys
from pathlib import Path
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from vendor.chaoxing import ChaoxingService,CHAOXING_MOBILE_UA
logging.disable(logging.CRITICAL)
DESKTOP='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36'
def describe(r):
 text=r.text
 title=re.search(r'<title[^>]*>(.*?)</title>',text,re.S)
 return {'status':r.status_code,'host':r.url.host,'path':r.url.path,'redirects':[{'status':h.status_code,'host':h.url.host,'path':h.url.path} for h in r.history],
 'bytes':len(r.content),'sha256':hashlib.sha256(r.content).hexdigest(),'title':title.group(1).strip() if title else None,
 'maintenance':'系统维护中' in text,'access_error':'您所浏览的页面暂时不能访问' in text,
 'im_fields':{k:bool(re.search(r"id=[\"']"+k+r"[\"']",text)) for k in ('myTuid','myPuid','myToken')},
 'scripts':re.findall(r"<script[^>]*src=[\"']([^\"']+)",text)[:12]}
async def main():
 svc=ChaoxingService('data/sessions.db');await svc.init()
 urls=['https://im.chaoxing.com/webim/me','https://im.chaoxing.com/','https://im.chaoxing.com/webim/','https://im.chaoxing.com/webim/me?webim=1']
 async with httpx.AsyncClient(follow_redirects=True,timeout=20) as anonymous:
  for label,client,ua in [('anonymous-desktop',anonymous,DESKTOP),('logged-in-desktop',svc._client,DESKTOP),('logged-in-mobile',svc._client,CHAOXING_MOBILE_UA)]:
   for url in urls:
    try:r=await client.get(url,headers={'User-Agent':ua,'Referer':'https://i.chaoxing.com'});print(json.dumps({'case':label,'url':url,**describe(r)},ensure_ascii=False),flush=True)
    except Exception as e:print(json.dumps({'case':label,'url':url,'error':type(e).__name__}),flush=True)
  for url in ['https://i.chaoxing.com/base','https://i.chaoxing.com']:
   r=await svc._client.get(url,headers={'User-Agent':DESKTOP})
   refs=set(re.findall(r"(?:https?:)?//im\.chaoxing\.com[^\"'\s<>]*",r.text))
   print(json.dumps({'case':'official-portal','url':url,'status':r.status_code,'im_links':[x.split('?')[0] for x in refs]},ensure_ascii=False),flush=True)
 await svc._client.aclose()
asyncio.run(main())
