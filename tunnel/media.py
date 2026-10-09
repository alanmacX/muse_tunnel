"""Archive only explicit attachment files already present in the trusted client cache.

Never fetch a URL supplied by a message, or serve arbitrary filesystem paths.
"""
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from api import DB, connection, now

MAX_BYTES = 50 * 1024 * 1024


def media_root():
    return DB.parent / 'media'


def archive_file(candidate, roots):
    path = Path(candidate)
    if not path.is_absolute() or path.is_symlink():
        return None
    resolved = path.resolve()
    if not any(resolved.is_relative_to(root.resolve()) for root in roots):
        return None
    try:
        with resolved.open('rb') as source:
            data = source.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return {'status': 'too_large', 'max_bytes': MAX_BYTES}
    except (OSError, ValueError):
        return None
    asset_id = hashlib.sha256(data).hexdigest()
    root = media_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / asset_id
    # Use signatures rather than trusting a source-provided MIME or extension.
    mime = 'application/octet-stream'
    for signature, kind in ((b'%PDF-', 'application/pdf'), (b'\x89PNG\r\n\x1a\n', 'image/png'),
                            (b'\xff\xd8\xff', 'image/jpeg'), (b'GIF87a', 'image/gif'),
                            (b'GIF89a', 'image/gif')):
        if data.startswith(signature):
            mime = kind
            break
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        mime = 'image/webp'
    filename = re.sub(r'[^\w. ()-]', '_', resolved.name)[:180] or asset_id
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        if not target.exists():
            temporary = root / ('.' + secrets.token_hex(16))
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, 'wb') as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        # Renew the lease before GC can run; protects a newly observed shared blob
        # during the short interval before its message is ingested.
        c.execute('''INSERT INTO assets VALUES (?,?,?,?,?) ON CONFLICT(asset_id)
          DO UPDATE SET created_at=excluded.created_at''',
                  (asset_id, len(data), mime, filename, now()))
    return {'status': 'available', 'asset_id': asset_id, 'sha256': asset_id,
            'size_bytes': len(data), 'mime_type': mime, 'filename': filename,
            'metadata_path': '/v1/files/' + asset_id,
            'download_path': '/v1/files/' + asset_id + '/content'}


def attachments(value, roots):
    """Retain references and distinguish card bodies from actual cached files."""
    found = []
    def append(ref):
        result = None
        path = ref.get('filepath')
        if isinstance(path, str) and path:
            result = archive_file(path, roots)
        preview = ref.get('blurredPath') or ref.get('thumbpath')
        if result is None and isinstance(preview, str) and preview:
            result = archive_file(preview, roots)
            if result and result['status'] == 'available':
                result.update(status='preview_available', original_available=False)
        found.append({'reference': ref, **(result or {
            'status': 'not_downloaded' if path or ref.get('url') or ref.get('mediaId') or ref.get('filename')
                      else 'card_reference_only'})})
    def visit(item, depth=0):
        if depth > 12:
            return
        if isinstance(item, str) and item[:1] in ('{', '['):
            try:
                visit(json.loads(item), depth + 1)
            except (ValueError, TypeError):
                pass
        elif isinstance(item, list):
            for part in item:
                visit(part, depth + 1)
        elif isinstance(item, dict):
            if 'contentType' in item and ('mediaId' in item or 'filepath' in item):
                append(item)
            for key, part in item.items():
                if key == 'attachments' and isinstance(part, list):
                    for ref in part:
                        if not isinstance(ref, dict):
                            continue
                        append(ref)
                else:
                    visit(part, depth + 1)
    visit(value)
    return found
