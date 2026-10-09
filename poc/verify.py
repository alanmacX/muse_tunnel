"""Integration checks against a DISPOSABLE seeded instance; changes receipts."""
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

base = os.environ.get('MUSE_TEST_URL','http://127.0.0.1:18766')
key = Path(os.environ.get('MUSE_POC_KEY_FILE','data/consumer.key')).read_text().strip()


def call(path, body=None, auth=True, expected=200):
    headers = {'Authorization':'Bearer '+key} if auth else {}
    if body is not None:
        headers['Content-Type']='application/json'
    req = Request(base+path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urlopen(req,timeout=15) as r:
            code, data = r.status, json.load(r)
    except HTTPError as e:
        code, data = e.code, json.load(e)
    assert code == expected, (path,code,data)
    return data


def run():
    call('/v1/feed',auth=False,expected=401)
    call('/v1/feed?token='+key,auth=False,expected=401)
    call('/v1/feed?limit=201',expected=422)
    call('/v1/receipts',{'items':[{'event_id':'chaoxing:mock-1:v1','hash':'0'*64}]},expected=409)
    assert call('/v1/status')['acknowledged'] == 0
    first = call('/v1/feed?limit=1')['items'][0]
    assert call('/v1/feed?limit=1')['items'][0] == first  # No ACK: replay.
    call('/v1/receipts',{'items':[{'event_id':first['event_id'],'hash':first['hash']},
                                {'event_id':'missing','hash':'0'*64}]},expected=409)
    assert call('/v1/status')['acknowledged'] == 0  # Entire batch rolls back.
    body={'items':[{'event_id':first['event_id'],'hash':first['hash']}]}
    call('/v1/receipts',body)
    call('/v1/receipts',body)
    assert call('/v1/status')['acknowledged']==1
    day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    package=call('/v1/daily/'+day)
    call('/v1/daily/'+day+'/audit',{'revision':package['revision'],'manifest_hash':package['manifest_hash']},expected=409)
    cursor=0
    upper=None
    all_items=[]
    while True:
        path=f'/v1/feed?after_seq={cursor}&limit=97&pending_only=false'
        if upper is not None:
            path+=f'&upper_seq={upper}'
        page=call(path)
        upper=page['upper_seq']
        all_items+=page['items']
        cursor=page['next_cursor']
        if not page['has_more']:
            break
    assert len(all_items)==608
    assert len({e['event_id'] for e in all_items})==608
    assert [e['seq'] for e in all_items]==sorted(e['seq'] for e in all_items)
    assert all_items[-1]['payload']['sent_at'] < all_items[0]['payload']['sent_at']
    assert all_items[19]['payload']['text']==all_items[20]['payload']['text']
    manifest=[]
    offset=0
    while True:
        page=call(f'/v1/daily/{day}/items?revision={package["revision"]}&offset={offset}&limit=91')
        manifest+=page['manifest']
        offset=page['next_offset']
        if not page['has_more']:
            break
    encoded=json.dumps(manifest,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    assert hashlib.sha256(encoded).hexdigest()==package['manifest_hash']
    for start in range(0,len(all_items),100):
        call('/v1/receipts',{'items':[{'event_id':e['event_id'],'hash':e['hash']} for e in all_items[start:start+100]]})
    assert call('/v1/status')['pending']==0
    assert call('/v1/feed')['items']==[]
    assert call('/v1/daily/'+day)['missing_receipt_ids']==[]
    call('/v1/daily/'+day+'/audit',{'revision':package['revision'],'manifest_hash':package['manifest_hash']})
    assert call('/v1/daily/'+day)['audit_complete']
    print('PASS: auth, bounds, undelivered/hash rejection, batch rollback, replay, idempotent ACK, 608-event pagination, late event, duplicate text, manifest hash, daily audit.')


if __name__=='__main__':
    run()
