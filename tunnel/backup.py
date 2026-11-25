"""Private consistent SQLite backups. Does not export secrets through HTTP."""
import os,sqlite3,shutil,sys
from contextlib import closing
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from storage_guard import require_backup_space
os.umask(0o077)
root=Path('data/backups');root.mkdir(parents=True,exist_ok=True,mode=0o700)
def prune_backups():
    snapshots = []
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir():
            continue
        try:
            instant = datetime.strptime(folder.name, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        snapshots.append((instant, folder))
    kept = {}
    for instant, folder in sorted(snapshots, reverse=True):
        day = instant.astimezone(ZoneInfo('Asia/Shanghai')).date()
        if day not in kept and len(kept) < 2:
            kept[day] = folder
    keep = set(kept.values())
    remove = [folder for _, folder in snapshots if folder not in keep]
    if remove:
        for folder in keep:
            for name in ('tunnel.db', 'sessions.db'):
                with sqlite3.connect((folder / name).resolve().as_uri() + '?mode=ro', uri=True) as db:
                    if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                        raise RuntimeError(f'Backup check failed: {folder.name}/{name}')
        for folder in remove:
            shutil.rmtree(folder)
    print(f'Muse backups: kept {len(keep)} backup days, removed {len(remove)} old snapshots')

if sys.argv[1:] == ['--prune-only']:
    prune_backups()
    sys.exit(0)
if sys.argv[1:]:
    raise SystemExit('Unknown argument')

stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
databases = ('tunnel.db', 'sessions.db')
private_files = ('consumer.key','zjut-credentials.json','zjut-encryption.key','qq-source.json')

def size(path):
    try: return path.stat().st_size
    except FileNotFoundError: return 0

copy_bytes = sum(size(Path('data')/name) + size(Path('data')/(name+'-wal')) for name in databases)
copy_bytes += sum(size(Path('data')/name) for name in private_files)
copy_bytes += sum(size(p) for p in Path('data/retention').rglob('*') if p.is_file())
free, required = require_backup_space(root, copy_bytes)
print(f'Backup space check passed: free={free} bytes, required={required} bytes', flush=True)
dest=root/('.incoming-'+stamp);dest.mkdir(mode=0o700)
try:
    for name in databases:
        source_uri=(Path('data')/name).resolve().as_uri()+'?mode=ro'
        with closing(sqlite3.connect(source_uri,uri=True)) as source, closing(sqlite3.connect(dest/name)) as backup:
            source.backup(backup)
            if backup.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise RuntimeError(f'Backup check failed: {name}')
        (dest/name).chmod(0o600)
    for name in private_files:
        if Path('data/'+name).exists():
            shutil.copyfile('data/'+name,dest/name);(dest/name).chmod(0o600)
    if Path('data/media').exists():
        # Media blobs are immutable; hardlinks retain them without duplicating disk usage.
        shutil.copytree('data/media',dest/'media',copy_function=os.link)
    if Path('data/retention').exists():
        shutil.copytree('data/retention',dest/'retention',copy_function=shutil.copy2)
    dest.rename(root/stamp)
finally:
    if dest.exists():shutil.rmtree(dest)
prune_backups()
print('Private backup completed')
