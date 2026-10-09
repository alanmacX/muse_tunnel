"""Offline regression checks: pagination, real empty response, and partial errors."""
import asyncio,httpx
from zjut_collect import fetch_exams
from vendor import zjut
async def check():
    pages=[]
    async def handler(req):
        if req.method=='GET':return httpx.Response(200,text='index')
        from urllib.parse import parse_qs
        p=int(parse_qs(req.content.decode())['queryModel.currentPage'][0]);pages.append(p)
        start=(p-1)*100
        return httpx.Response(200,json={'items':[{'id':i,'kcmc':'Course','kssj':'2026-12-01 (09:00-11:00)'} for i in range(start,min(start+100,205))],'totalCount':205})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        data=await fetch_exams(client,'2026','1');assert len(data['items'])==205 and pages==[1,2,3]
        assert len(zjut.parse_exams(data))==205
    async def empty(req):return httpx.Response(200,json={'items':[],'totalCount':0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(empty)) as client:
        assert (await fetch_exams(client,'2026','1'))['items']==[]
    async def repeat(req):return httpx.Response(200,json={'items':[{'id':1}],'totalCount':2})
    async with httpx.AsyncClient(transport=httpx.MockTransport(repeat)) as client:
        try:await fetch_exams(client,'2026','1')
        except RuntimeError as e:assert str(e)=='exam_pagination_not_advancing'
        else:raise AssertionError('Repeated exam page accepted')
    async def changing(req):
        from urllib.parse import parse_qs
        if req.method=='GET':return httpx.Response(200,text='index')
        p=int(parse_qs(req.content.decode())['queryModel.currentPage'][0])
        return httpx.Response(200,json={'items':[{'id':p}],'totalCount':2 if p==1 else 3})
    async with httpx.AsyncClient(transport=httpx.MockTransport(changing)) as client:
        try:await fetch_exams(client,'2026','1')
        except RuntimeError as e:assert str(e)=='exam_changed_during_pagination'
        else:raise AssertionError('Changed snapshot accepted')
    practice=zjut.parse_practice_schedule({'sjkList':[{'kcmc':'Practice','qsjsz':'1-4周'}]})[0]
    assert 'weekdays' not in practice and 'start_section' not in practice
    print('PASS: 205 exams / 3 pages, confirmed empty, repeated page rejection, changed total rejection, no invented practice times.')
asyncio.run(check())
