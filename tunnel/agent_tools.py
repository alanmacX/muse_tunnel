"""Bounded, read-only student information tools, reusable by an MCP adapter."""
from datetime import date
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException,Query
from api import authenticate,connection,deliver,event,source_availability

router=APIRouter(prefix='/v1/tools',dependencies=[Depends(authenticate)])
Source=Literal['dingtalk','chaoxing','zjut']


def upper_bound(c,upper_seq):
    return upper_seq if upper_seq is not None else c.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]


def latest_where():
    return '''e.seq<=? AND e.seq=(SELECT MAX(v.seq) FROM events v
        WHERE v.source=e.source AND v.source_id=e.source_id AND v.seq<=?)'''


@router.get('/search',operation_id='search_student_information',description='Search latest archived messages, notices, assignments, course files and academic snapshots. Results include source coverage. Freeze upper_seq on subsequent pages. Search is not a substitute for reading all pending events, and never acknowledges them.')
def search_student_information(source:Source|None=None,q:str=Query('',max_length=200),
        conversation:str|None=Query(None,max_length=200),kind:str|None=Query(None,max_length=80),
        from_day:date|None=None,to_day:date|None=None,after_seq:int=Query(0,ge=0),
        upper_seq:int|None=Query(None,ge=0),limit:int=Query(30,ge=1,le=100)):
    if from_day and to_day and from_day>to_day:raise HTTPException(422,'from_day exceeds to_day')
    with connection() as c:
        upper=upper_bound(c,upper_seq)
        where=latest_where()+' AND e.seq>? AND e.payload!=\'null\''
        params=[upper,upper,after_seq]
        for clause,value in [('e.source=?',source),("json_extract(e.payload,'$.kind')=?",kind)]:
            if value is not None:where+=' AND '+clause;params.append(value)
        if conversation is not None:
            where+=" AND (json_extract(e.payload,'$.conversation_id')=? OR json_extract(e.payload,'$.conversation')=? OR json_extract(e.payload,'$.course_id')=?)"
            params.extend([conversation]*3)
        if from_day:where+=' AND e.source_day>=?';params.append(from_day.isoformat())
        if to_day:where+=' AND e.source_day<=?';params.append(to_day.isoformat())
        if q:
            # Literal substring; parameterized SQL and escaping prevent wildcard expansion.
            needle='%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
            where+=" AND (COALESCE(json_extract(e.payload,'$.text'),'') || ' ' || COALESCE(json_extract(e.payload,'$.title'),'') || ' ' || COALESCE(json_extract(e.payload,'$.conversation'),'') || ' ' || COALESCE(json_extract(e.payload,'$.attachments'),'') || ' ' || COALESCE(json_extract(e.payload,'$.courses'),'') || ' ' || COALESCE(json_extract(e.payload,'$.exams'),'')) LIKE ? ESCAPE '\\'"
            params.append(needle)
        rows=c.execute('SELECT e.* FROM events e WHERE '+where+' ORDER BY e.seq LIMIT ?',params+[limit+1]).fetchall()
        selected=rows[:limit]
        return {'items':deliver(c,selected),'upper_seq':upper,'next_cursor':selected[-1]['seq'] if selected else after_seq,
                'has_more':len(rows)>limit,'sources':source_availability(c),'coverage':'archived_latest_versions_only',
                'date_filter':'source_day; undated records use ingestion day','acknowledges_events':False}


@router.get('/events/{event_id}',operation_id='read_student_event',description='Read the exact archived event version, including original evidence and attachment download paths. Does not fetch upstream history or acknowledge processing.')
def read_student_event(event_id:str):
    with connection() as c:
        row=c.execute('SELECT * FROM events WHERE event_id=?',(event_id,)).fetchone()
        if row is None:raise HTTPException(404,'Unknown event')
        return {'item':deliver(c,[row])[0],'sources':source_availability(c)}


@router.get('/conversations',operation_id='list_student_conversations',description='Discover archived DingTalk conversations or Chaoxing course/notice groups. Counts are latest archived records, not proof of complete upstream history. Paginate with fixed upper_seq.')
def list_student_conversations(source:Source,offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),upper_seq:int|None=Query(None,ge=0)):
    with connection() as c:
        upper=upper_bound(c,upper_seq)
        rows=c.execute('''SELECT COALESCE(json_extract(e.payload,'$.conversation_id'),json_extract(e.payload,'$.course_id'),json_extract(e.payload,'$.conversation')) AS conversation_key,
            MAX(json_extract(e.payload,'$.conversation')) AS name,COUNT(*) AS archived_records,MAX(e.seq) AS latest_seq
            FROM events e WHERE '''+latest_where()+''' AND e.source=? AND e.payload!='null'
            AND COALESCE(json_extract(e.payload,'$.conversation_id'),json_extract(e.payload,'$.course_id'),json_extract(e.payload,'$.conversation')) IS NOT NULL
            GROUP BY conversation_key ORDER BY conversation_key LIMIT ? OFFSET ?''',(upper,upper,source,limit+1,offset)).fetchall()
        selected=rows[:limit]
        return {'items':[dict(r) for r in selected],'upper_seq':upper,'has_more':len(rows)>limit,
                'next_offset':offset+len(selected),'sources':source_availability(c),'coverage':'archived_only'}
