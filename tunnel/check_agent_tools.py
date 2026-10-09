"""Isolated bounded search, version snapshots, auth and receipt separation."""
import os,secrets,tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);key=root/'key';token=secrets.token_urlsafe(48);key.write_text(token);key.chmod(0o600)
    os.environ.update(MUSE_TUNNEL_DB=str(root/'ledger.db'),MUSE_TUNNEL_KEY_FILE=str(key))
    import api,store
    from fastapi.testclient import TestClient
    api.init();client=TestClient(api.app);auth={'Authorization':'Bearer '+token}
    for path in ('/v1/tools/search','/v1/tools/conversations?source=dingtalk','/v1/tools/events/unknown'):
        assert client.get(path).status_code==401
    def add(identity,text,source='dingtalk',conversation='课程群'):
        store.ingest(source,identity,{'source':source,'kind':'message','conversation_id':'group-1','conversation':conversation,'text':text,'attachments':[]},'2026-10-08T10:00:00+08:00')
    add('one','调课原通知')
    first=client.get('/v1/tools/search?q=调课&limit=1',headers=auth).json()
    original=first['items'][0];upper=first['upper_seq']
    add('one','调课修订通知');add('two','调课第二条');add('three','普通公告');add('other','调课学习通','chaoxing')
    frozen=client.get('/v1/tools/search',params={'q':'调课','upper_seq':upper},headers=auth).json()
    assert frozen['items']==first['items']
    page=client.get('/v1/tools/search',params={'source':'dingtalk','q':'调课','limit':1},headers=auth).json()
    assert page['has_more'] and page['items'][0]['payload']['text']=='调课修订通知'
    second=client.get('/v1/tools/search',params={'source':'dingtalk','q':'调课','limit':1,'upper_seq':page['upper_seq'],'after_seq':page['next_cursor']},headers=auth).json()
    assert len(second['items'])==1 and second['items'][0]['payload']['text']=='调课第二条' and not second['has_more']
    assert client.get('/v1/tools/search?q=%25',headers=auth).json()['items']==[]
    assert client.get('/v1/tools/search?q=%27%20OR%201%3D1--',headers=auth).json()['items']==[]
    assert client.get('/v1/tools/search?from_day=2026-10-09',headers=auth).json()['items']==[]
    assert client.get('/v1/tools/search?from_day=2026-10-09&to_day=2026-10-08',headers=auth).status_code==422
    assert client.get('/v1/tools/search?limit=101',headers=auth).status_code==422
    assert client.get('/v1/tools/search?source=unknown',headers=auth).status_code==422
    groups=client.get('/v1/tools/conversations?source=dingtalk',headers=auth).json()
    assert groups['items'][0]['archived_records']==3
    exact=client.get('/v1/tools/events/'+original['event_id'],headers=auth).json()
    assert exact['item']['payload']['text']=='调课原通知'
    assert client.get('/v1/status',headers=auth).json()['acknowledged']==0
    schema=client.get('/v1/openapi.json',headers=auth).json()
    assert schema['paths']['/v1/tools/search']['get']['operationId']=='search_student_information'
    print('PASS: authentication, frozen latest-version search, pagination, source/date filters, literal keywords, exact evidence, conversation counts and no implicit receipts')
