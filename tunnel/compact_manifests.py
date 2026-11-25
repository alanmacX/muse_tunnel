"""Verify old manifests against the immutable ledger, replace copies, then reclaim pages."""
import argparse
from bisect import bisect_right
from contextlib import closing
import json
import os
import sqlite3

from api import DB, connection, digest, manifest_at


def compact(apply=False):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        days = c.execute('SELECT DISTINCT day FROM packages WHERE manifest<>\'\'').fetchall()
        verified = old_bytes = 0
        for (day,) in days:
            entries = manifest_at(c, day, 9223372036854775807)
            seqs = [e['seq'] for e in entries]
            for p in c.execute('SELECT revision,upper_seq,manifest,hash FROM packages WHERE day=? AND manifest<>\'\'',
                               (day,)):
                rebuilt = entries[:bisect_right(seqs, p['upper_seq'])]
                if json.loads(p['manifest']) != rebuilt or digest(rebuilt) != p['hash']:
                    raise RuntimeError(f'Manifest verification failed: {day} revision {p["revision"]}')
                old_bytes += len(p['manifest'].encode())
                verified += 1
            if apply:
                c.execute("UPDATE packages SET manifest='' WHERE day=? AND manifest<>''", (day,))
    return {'verified_revisions': verified, 'duplicate_bytes': old_bytes, 'applied': apply}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--vacuum', action='store_true')
    args = parser.parse_args()
    if args.vacuum and not args.apply:
        parser.error('--vacuum requires --apply')
    os.umask(0o077)
    result = compact(args.apply)
    if args.vacuum:
        with closing(sqlite3.connect(DB, timeout=30)) as c:
            c.execute('VACUUM')
            if c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                raise RuntimeError('Could not truncate WAL: active reader or writer')
            if c.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise RuntimeError('Compacted database failed its quick_check')
    print(json.dumps({**result, 'database_bytes': DB.stat().st_size}))
