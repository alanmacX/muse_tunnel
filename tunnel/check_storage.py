"""Verify legacy compaction and backup thresholds in an isolated ledger."""
from datetime import datetime
from pathlib import Path
import os
import secrets
import tempfile
from unittest.mock import patch
from collections import namedtuple

with tempfile.TemporaryDirectory() as tmp:
    key=Path(tmp)/'key'; key.write_text(secrets.token_urlsafe(48)); key.chmod(0o600)
    os.environ.update(MUSE_TUNNEL_DB=tmp+'/ledger.db', MUSE_TUNNEL_KEY_FILE=tmp+'/key')
    import api, store, storage_guard as guard
    from compact_manifests import compact

    api.init()
    day = datetime.now(api.LOCAL).date().isoformat()
    store.ingest('qq', 'first', {'text': 'first'}, api.now())
    store.refresh_daily()
    with api.connection() as c:
        old = c.execute('SELECT * FROM packages WHERE day=?', (day,)).fetchone()
        manifest = api.manifest_at(c, day, old['upper_seq'])
        c.execute('UPDATE packages SET manifest=? WHERE day=?', (api.canonical(manifest), day))
        c.execute('INSERT INTO audits VALUES (?,?,?,?,?)', (api.CONSUMER, day, 1, old['hash'], api.now()))
        c.execute("UPDATE events SET payload='null' WHERE seq=1")
    store.ingest('qq', 'late', {'text': 'late'}, api.now())
    store.refresh_daily()
    assert compact()['verified_revisions'] == 1
    with api.connection() as c:
        c.execute("UPDATE packages SET manifest='[]' WHERE day=? AND revision=1", (day,))
    try: compact(apply=True)
    except RuntimeError: pass
    else: raise AssertionError('Accepted a different historical manifest')
    with api.connection() as c:
        assert c.execute('SELECT manifest FROM packages WHERE day=? AND revision=1', (day,)).fetchone()[0] == '[]'
        c.execute('UPDATE packages SET manifest=? WHERE day=? AND revision=1', (api.canonical(manifest), day))
    assert compact(apply=True)['verified_revisions'] == 1
    with api.connection() as c:
        old_after = c.execute('SELECT * FROM packages WHERE day=? AND revision=1', (day,)).fetchone()
        assert old_after['manifest'] == '' and old_after['hash'] == old['hash']
        assert api.package_manifest(c, old_after) == manifest
        assert api.manifest_at(c, day, old['upper_seq'], 1, 1) == []
        assert c.execute('SELECT hash FROM audits WHERE day=? AND revision=1', (day,)).fetchone()[0] == old['hash']

    Disk = namedtuple('Disk', 'total used free')
    for free, level in [(8, 'ok'), (5, 'warning'), (1, 'critical')]:
        with patch.object(guard.shutil, 'disk_usage', return_value=Disk(40*guard.GIB, (40-free)*guard.GIB, free*guard.GIB)):
            assert guard.disk_status(Path(tmp))['level'] == level
            if free == 1:
                try: guard.require_backup_space(Path(tmp), guard.GIB)
                except RuntimeError: pass
                else: raise AssertionError('Backup passed with insufficient space')
            else:
                assert guard.require_backup_space(Path(tmp), guard.GIB)[0] == free*guard.GIB
    print('PASS: old hash, audited revision, purged payload, migration rollback, stable paging and low-disk backup guard')
