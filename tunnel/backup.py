"""Private consistent SQLite backups. Does not export secrets through HTTP."""
import os,sqlite3,shutil
from datetime import datetime,timezone,timedelta
from pathlib import Path
os.umask(0o077)
root=Path('data/backups');root.mkdir(parents=True,exist_ok=True,mode=0o700)
stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
dest=root/stamp;dest.mkdir(mode=0o700)
for name in ('tunnel.db','sessions.db'):
    with sqlite3.connect('data/'+name) as source,sqlite3.connect(dest/name) as backup:
        source.backup(backup)
    (dest/name).chmod(0o600)
for name in ('consumer.key','zjut-credentials.json','zjut-encryption.key'):
    if Path('data/'+name).exists():
        shutil.copyfile('data/'+name,dest/name);(dest/name).chmod(0o600)
if Path('data/media').exists():
    # Media blobs are immutable; hardlinks retain them without duplicating disk usage.
    shutil.copytree('data/media',dest/'media',copy_function=os.link)
# Keep fourteen days of successful snapshots; initial migration archive is separate.
cutoff=datetime.now(timezone.utc)-timedelta(days=14)
for folder in root.iterdir():
    if folder.is_dir():
        try:age=datetime.strptime(folder.name,'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        except ValueError:continue
        if age<cutoff:shutil.rmtree(folder)
print('Private backup completed')
