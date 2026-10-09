"""One-time SSH-only migration of encrypted legacy credentials, never print values."""
import base64,json,os,sqlite3
from pathlib import Path
from Crypto.Cipher import AES
os.umask(0o077)
with sqlite3.connect('backup/student-agent.db') as c:
 c.row_factory=sqlite3.Row
 row=c.execute('SELECT student_id,password_enc,save_credentials FROM zjut_config WHERE id=1').fetchone()
 if not row or not row['save_credentials'] or not row['password_enc']:raise RuntimeError('No saved ZJUT credential')
env={}
for line in Path('backup/student-agent.env').read_text().splitlines():
 if '=' in line and not line.lstrip().startswith('#'):
  k,v=line.split('=',1);env[k.strip()]=v.strip().strip('"').strip("'")
key=base64.b64decode(env['ZJUT_KEY']);blob=base64.b64decode(row['password_enc'])
AES.new(key,AES.MODE_GCM,nonce=blob[:16]).decrypt_and_verify(blob[32:],blob[16:32])
# Keep only the necessary encrypted record and a separately permissioned key.
p=Path('data/zjut-credentials.json');p.write_text(json.dumps({'student_id':row['student_id'],'password_enc':row['password_enc']}));p.chmod(0o600)
p=Path('data/zjut-encryption.key');p.write_bytes(key);p.chmod(0o600)
print('Encrypted ZJUT credentials migrated and validated')
