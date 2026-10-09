"""Replay the existing database collector, reporting observed history accurately.

This does not request cloud history or operate the desktop UI.
"""
import argparse
import json
import os
from datetime import datetime, timedelta

parser = argparse.ArgumentParser()
parser.add_argument('--days', type=int, default=7)
args = parser.parse_args()
if not 1 <= args.days <= 365:
    parser.error('days must be between 1 and 365')
# Do not share the temporary decrypted file with the scheduled collector.
os.environ.setdefault('DINGTALK_DB_SOURCE', '/home/ubuntu/.config/DingTalk')
os.environ['DINGTALK_DECRYPTED_PATH'] = 'data/history-replay-snapshot.db'
from api import init, connection, LOCAL, now
from collect import collect_dingtalk
from store import ingest, refresh_daily

init()
observed, inserted = collect_dingtalk()
if observed is None:
    raise SystemExit('Database collector failed; no complete-history claim')
end = datetime.now(LOCAL)
start = end - timedelta(days=args.days)
with connection() as c:
    rows = c.execute('''SELECT payload FROM events e WHERE source='dingtalk'
      AND json_extract(payload,'$.kind')='message'
      AND seq=(SELECT MAX(seq) FROM events other
               WHERE other.source=e.source AND other.source_id=e.source_id)''').fetchall()
messages = [json.loads(row[0]) for row in rows]
recent = [m for m in messages if m.get('sent_at') and start <= datetime.fromisoformat(m['sent_at']) <= end]
report = {'source': 'dingtalk', 'kind': 'history_coverage', 'state': 'database_replay_complete',
          'method': 'existing_client_database_and_wal_collector',
          'requested_window_start': start.isoformat(), 'requested_window_end': end.isoformat(),
          'cached_unique_messages': len(messages), 'cached_messages_in_window': len(recent),
          'new_event_versions': inserted, 'coverage': 'local_cache_only', 'history_complete': False,
          'limitations': ['Cloud history pagination is not implemented.',
                          'Referenced attachments and cards may still lack complete bodies.'],
          'sent_at': now()}
ingest('dingtalk', 'backfill-20261009-seven-days', report, report['sent_at'])
refresh_daily()
print(json.dumps(report, ensure_ascii=False))
