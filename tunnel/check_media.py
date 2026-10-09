"""Isolated binary delivery, authentication and cache-boundary checks."""
import hashlib
import os
import secrets
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    key = root / 'key'
    token = secrets.token_urlsafe(48)
    key.write_text(token)
    key.chmod(0o600)
    os.environ.update(MUSE_TUNNEL_DB=str(root / 'ledger.db'), MUSE_TUNNEL_KEY_FILE=str(key))
    import api, media, store
    from fastapi.testclient import TestClient
    api.init()
    cache = root / 'ImageFiles'
    cache.mkdir()
    image = cache / 'image.png'
    body = b'\x89PNG\r\n\x1a\n' + b'example image bytes'
    image.write_bytes(body)
    ref = {'attachments': [{'filepath': str(image), 'type': 2}]}
    files = media.attachments(ref, [cache])
    asset = files[0]
    assert asset['status'] == 'available'
    assert media.archive_file(str(key), [cache]) is None
    link = cache / 'secret.png'
    link.symlink_to(key)
    assert media.archive_file(str(link), [cache]) is None
    assert media.archive_file('../key', [cache]) is None
    assert media.attachments({'attachments': [{'url': 'http://127.0.0.1/secret'}]}, [cache])[0]['status'] == 'not_downloaded'
    assert media.attachments({'attachments': [{'extension': '{"cardInstanceId":"123"}'}]}, [cache])[0]['status'] == 'card_reference_only'
    assert store.ingest('dingtalk', 'file-test', {'attachments': files}, api.now())
    assert not store.ingest('dingtalk', 'file-test', {'attachments': media.attachments(ref, [cache])}, api.now())
    image.unlink()  # Archive must survive source-cache eviction.
    client = TestClient(api.app)
    for path in (asset['metadata_path'], asset['download_path']):
        assert client.get(path).status_code == 401
    auth = {'Authorization': 'Bearer ' + token}
    reply = client.get(asset['download_path'], headers=auth)
    assert reply.status_code == 200 and reply.content == body
    assert hashlib.sha256(reply.content).hexdigest() == asset['sha256']
    assert reply.headers['content-type'] == 'image/png'
    assert reply.headers['content-disposition'].startswith('attachment;')
    assert reply.headers['cache-control'] == 'no-store'
    assert client.get('/v1/files/' + '0' * 64 + '/content', headers=auth).status_code == 404
    assert client.get('/v1/files/not-a-hash/content', headers=auth).status_code == 404
    key.write_text(secrets.token_urlsafe(48))
    assert client.get(asset['download_path'], headers=auth).status_code == 401
    print('PASS: authenticated binary delivery, hash, cache eviction, idempotency, secret/path isolation, missing references and key revocation')
