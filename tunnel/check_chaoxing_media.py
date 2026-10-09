"""Isolated source redirect, cookie isolation, parsing and binary checks."""
import asyncio,hashlib,json,os,socket,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx

with tempfile.TemporaryDirectory() as tmp:
    os.environ['MUSE_TUNNEL_DB']=str(Path(tmp)/'ledger.db')
    import api,chaoxing_media as m
    api.init()
    public=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('1.1.1.1',443))]
    with patch.object(m.socket,'getaddrinfo',return_value=public):
        assert m.trusted_url('http://d0.cldisk.com/x')=='https://d0.cldisk.com/x'
        for url in ('https://evil.example/x','https://chaoxing.com.evil.example/x','https://u:p@d0.cldisk.com/x','http://127.0.0.1/x','https://d0.cldisk.com:444/x'):
            try:m.trusted_url(url)
            except ValueError:pass
            else:raise AssertionError(url)
        try:m.trusted_url('https://d0.cldisk.com/x',auth=True)
        except ValueError:pass
        else:raise AssertionError('source credentials could reach CDN')
    with patch.object(m.socket,'getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]):
        try:m.trusted_url('https://d0.cldisk.com/x')
        except ValueError:pass
        else:raise AssertionError('private DNS address accepted')
    parsed=m.Materials('<input id="totalPages" value="1"><ul class="dataBody_td" dataName="授课计划.doc" type="doc" id="42"><li class="dataBody_time_stu">10-08 14:23</li></ul>')
    assert parsed.pages==1 and parsed.rows[0]['name']=='授课计划.doc'
    assert m.modified_time('10-08 14:23').month==10
    assert m.unsupported_notice_attachments({'attachment':'[{"att_other":{}}]'})==1
    assert m.unsupported_notice_attachments({'attachment':'bad json'})==1
    assert m.unsupported_notice_attachments({'attachment':[{'att_clouddisk':{'fileId':'123'}}]})==0
    assert m.notice_references({'rtf_content':'<img src="https://p.cldisk.com/a.png">'})==[('https://p.cldisk.com/a.png','image.jpg')]
    real_client=httpx.AsyncClient
    image=b'\x89PNG\r\n\x1a\n'+b'isolated test bytes'
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.host=='mooc1.chaoxing.com':
            return httpx.Response(302,headers={'location':'https://d0.cldisk.com/image.png'})
        assert 'cookie' not in request.headers and 'authorization' not in request.headers
        return httpx.Response(200,content=image,headers={'content-type':'image/png'})
    async def check():
        async with real_client(cookies={'session':'private-test-cookie'},transport=httpx.MockTransport(handler)) as source:
            def bare(**kwargs):return real_client(transport=httpx.MockTransport(handler),**kwargs)
            with patch.object(m.socket,'getaddrinfo',return_value=public),patch.object(m.httpx,'AsyncClient',side_effect=bare):
                asset=await m.download(SimpleNamespace(_client=source),'https://mooc1.chaoxing.com/download','image.png',True)
                assert asset['status']=='available' and asset['sha256']==hashlib.sha256(image).hexdigest()
                assert (Path(tmp)/'media'/asset['asset_id']).read_bytes()==image
                assert not list((Path(tmp)/'chaoxing-files').iterdir())
                assert 'private-test-cookie' in requests[0].headers['cookie']
                with patch.object(m,'MAX_BYTES',4):
                    try:await m.download(SimpleNamespace(_client=source),'https://d0.cldisk.com/image.png','image.png')
                    except ValueError as e:assert str(e)=='attachment_too_large'
                    else:raise AssertionError('stream limit was ignored')
    asyncio.run(check())
    print('PASS: trusted hosts/public DNS, redirect cookie isolation, original image bytes/hash, stream size limit, cleanup, unsupported references, course parsing')
