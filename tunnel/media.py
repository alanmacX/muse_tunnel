"""Archive only explicit attachment files already present in the trusted client cache.

Never fetch a URL supplied by a message, or serve arbitrary filesystem paths.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from api import DB, connection, now
from doc_text import cached_extract, detect_mime, is_document

MAX_BYTES = 50 * 1024 * 1024
CHUNK_BYTES = 65536
MAX_INDEXED_FILES = 25000


def media_root():
    return DB.parent / 'media'


def _clean_filename(raw_name: str, fallback_suffix: str, fallback_id: str) -> str:
    base = Path(str(raw_name or '').strip()).name
    if base and fallback_suffix and not Path(base).suffix:
        ext = fallback_suffix if fallback_suffix.startswith('.') else '.' + fallback_suffix
        base = base + ext
    cleaned = re.sub(r'[^\w. ()\u4e00-\u9fff-]', '_', base)[:180].strip(' ._')
    return cleaned or fallback_id


def archive_file(candidate, roots, *, filename_hint=None, extract=False):
    path = Path(candidate)
    if not path.is_absolute() or path.is_symlink():
        return None
    try:
        resolved = path.resolve()
        if not any(resolved.is_relative_to(root.resolve()) for root in roots):
            return None
        st = resolved.stat()
        if not resolved.is_file():
            return None
        if st.st_size > MAX_BYTES:
            return {'status': 'too_large', 'size_bytes': st.st_size, 'max_bytes': MAX_BYTES,
                    'reason': 'attachment_too_large'}
        hasher = hashlib.sha256()
        header = b''
        total = 0
        with resolved.open('rb') as source:
            while chunk := source.read(CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_BYTES:
                    return {'status': 'too_large', 'size_bytes': total, 'max_bytes': MAX_BYTES,
                            'reason': 'attachment_too_large'}
                if len(header) < 4096:
                    header += chunk[:4096 - len(header)]
                hasher.update(chunk)
    except (OSError, ValueError):
        return None

    asset_id = hasher.hexdigest()
    root = media_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / asset_id
    filename = _clean_filename(filename_hint or resolved.name, resolved.suffix, asset_id)
    mime = detect_mime(header, resolved, filename)

    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        if not target.exists():
            temporary = root / ('.' + secrets.token_hex(16))
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                copy_hash = hashlib.sha256()
                copy_size = 0
                with resolved.open('rb') as source, os.fdopen(fd, 'wb') as out:
                    while chunk := source.read(CHUNK_BYTES):
                        copy_size += len(chunk)
                        if copy_size > MAX_BYTES:
                            return {'status': 'too_large', 'size_bytes': copy_size,
                                    'max_bytes': MAX_BYTES, 'reason': 'attachment_too_large'}
                        copy_hash.update(chunk)
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                if copy_hash.hexdigest() != asset_id or copy_size != total:
                    return None
                os.replace(temporary, target)
            except OSError:
                return None
            finally:
                temporary.unlink(missing_ok=True)
        # Renew the lease before GC can run; upgrade generic MIME when newly detected.
        c.execute('''INSERT INTO assets VALUES (?,?,?,?,?) ON CONFLICT(asset_id)
          DO UPDATE SET created_at=excluded.created_at,
          mime_type=CASE WHEN assets.mime_type='application/octet-stream' THEN excluded.mime_type ELSE assets.mime_type END''',
                  (asset_id, total, mime, filename, now()))

    result = {'status': 'available', 'asset_id': asset_id, 'sha256': asset_id,
              'size_bytes': total, 'mime_type': mime, 'filename': filename,
              'metadata_path': '/v1/files/' + asset_id,
              'download_path': '/v1/files/' + asset_id + '/content'}
    if extract and is_document(mime, filename):
        result.update(cached_extract(target, asset_id, mime, filename))
    return result


def build_cache_index(cache_roots, exact_match_dirs=(), db_conn=None):
    """Build a single-pass lookup over DingTalk cache folders and download tables."""
    by_token: dict[str, list[Path]] = {}
    by_name_size: dict[tuple[str, int], list[Path]] = {}
    by_name: dict[str, list[Path]] = {}
    allowed_roots = [Path(r) for r in (*cache_roots, *exact_match_dirs)]
    scanned = 0

    def walk(base: Path, allow_name_only: bool, depth: int = 0):
        nonlocal scanned
        if scanned >= MAX_INDEXED_FILES or depth > 4 or base.is_symlink() or not base.is_dir():
            return
        try:
            entries = list(os.scandir(base))
        except OSError:
            return
        for entry in entries:
            if scanned >= MAX_INDEXED_FILES or entry.is_symlink():
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    walk(Path(entry.path), allow_name_only, depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    scanned += 1
                    p = Path(entry.path)
                    size = entry.stat(follow_symlinks=False).st_size
                    low = p.name.lower()
                    by_name_size.setdefault((low, size), []).append(p)
                    if allow_name_only:
                        by_name.setdefault(low, []).append(p)
                        stem = p.stem.lower()
                        if len(stem) >= 6:
                            by_token.setdefault(stem, []).append(p)
                        parent = p.parent.name.lower()
                        if len(parent) >= 6:
                            by_token.setdefault(parent, []).append(p)
            except OSError:
                continue

    for root in cache_roots:
        walk(Path(root), allow_name_only=True)
    for root in exact_match_dirs:
        walk(Path(root), allow_name_only=False)

    if db_conn is not None:
        try:
            tables = [r[0] for r in db_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE '%space%' OR name LIKE '%dentry%' OR name LIKE '%file%' OR name LIKE '%download%')"
            )]
            for tbl in tables[:20]:
                if not re.fullmatch(r'[A-Za-z0-9_]+', tbl):
                    continue
                cols = {r[1] for r in db_conn.execute(f'PRAGMA table_info("{tbl}")')}
                path_cols = [c for c in ('localPath', 'filepath', 'filePath', 'downloadPath', 'cachePath', 'path') if c in cols]
                id_cols = [c for c in ('fileId', 'dentryId', 'f_id', 'id', 'mediaId') if c in cols]
                if not path_cols or not id_cols:
                    continue
                select_cols = ', '.join(f'"{c}"' for c in (*id_cols, *path_cols))
                for row in db_conn.execute(f'SELECT {select_cols} FROM "{tbl}" LIMIT 5000'):
                    row_dict = dict(row)
                    for pc in path_cols:
                        val = row_dict.get(pc)
                        if isinstance(val, str) and val.startswith('/'):
                            cand = Path(val)
                            if not cand.is_symlink() and cand.is_file():
                                for ic in id_cols:
                                    tok = str(row_dict.get(ic) or '').strip().lower()
                                    if tok:
                                        by_token.setdefault(tok, []).append(cand)
        except Exception:
            pass

    return {'roots': allowed_roots, 'by_token': by_token, 'by_name_size': by_name_size, 'by_name': by_name}


def _parse_json_dict(val) -> dict:
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val[:1] == '{':
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _int_or_zero(val) -> int:
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 0


def _pick_cached_candidate(meta: dict, cache_index: dict | None) -> Path | None:
    if not cache_index:
        return None
    for key in ('file_id', 'media_id'):
        tok = str(meta.get(key) or '').strip().lstrip('@').lower()
        if len(tok) >= 6 and tok in cache_index['by_token']:
            return max(cache_index['by_token'][tok], key=lambda p: p.stat().st_mtime_ns)
    name = str(meta.get('filename') or '').strip().lower()
    size = meta.get('declared_size_bytes') or 0
    if name and size > 0:
        matches = cache_index['by_name_size'].get((name, size), [])
        if matches:
            return max(matches, key=lambda p: p.stat().st_mtime_ns)
    if name and meta.get('space_id') and name in cache_index['by_name']:
        matches = cache_index['by_name'][name]
        if len(matches) == 1:
            return matches[0]
    return None


def _extract_attachment_meta(ref: dict) -> dict:
    ext = _parse_json_dict(ref.get('extension'))
    carried = _parse_json_dict(ref.get('carriedData'))
    auth_media = _parse_json_dict(ref.get('authMedia'))

    space_id = str(ext.get('s_id') or ext.get('spaceId') or ref.get('spaceId') or carried.get('spaceId') or '').strip()
    file_id = str(ext.get('f_id') or ext.get('fileId') or ext.get('dentryId')
                  or ref.get('fileId') or ref.get('dentryId') or carried.get('fileId') or '').strip()
    media_id = str(ref.get('mediaId') or auth_media.get('authMediaId') or auth_media.get('mediaId')
                   or ext.get('mediaId') or ext.get('authMediaId') or '').strip()
    file_type = str(ext.get('f_type') or ext.get('fileType') or ref.get('fileType') or '').strip().lstrip('.').lower()
    raw_name = str(ext.get('f_name') or ext.get('fileName') or ref.get('filename') or ref.get('fileName')
                   or ref.get('name') or carried.get('fileName') or '').strip()
    if raw_name and file_type and not Path(raw_name).suffix and re.fullmatch(r'[a-z0-9]{1,8}', file_type):
        raw_name = f'{raw_name}.{file_type}'
    declared_size = (_int_or_zero(ext.get('f_size')) or _int_or_zero(ext.get('fileSize'))
                     or _int_or_zero(ref.get('size')) or _int_or_zero(ref.get('picSize')))
    expire_raw = _int_or_zero(ext.get('sp_expireTime') or ext.get('expireTime'))
    expire_ms = expire_raw * 1000 if 0 < expire_raw < 10_000_000_000 else expire_raw

    paths = []
    for src in (ref, ext, carried, auth_media):
        for k in ('filepath', 'localPath', 'filePath', 'downloadPath', 'path'):
            val = src.get(k)
            if isinstance(val, str) and val.strip():
                paths.append(val.strip())

    url = str(ref.get('url') or auth_media.get('url') or ext.get('downloadUrl') or '').strip()
    has_file_signal = bool(paths or url or media_id or raw_name or file_id or space_id)
    doc_like = is_document('', raw_name or (f'x.{file_type}' if file_type else '')) or bool(space_id or file_id)
    return {
        'paths': paths,
        'url': url,
        'filename': raw_name or None,
        'file_type': file_type or (Path(raw_name).suffix.lstrip('.').lower() if raw_name else None) or None,
        'declared_size_bytes': declared_size or None,
        'space_id': space_id or None,
        'file_id': file_id or None,
        'media_id': media_id or None,
        'expire_ms': expire_ms or None,
        'has_file_signal': has_file_signal,
        'kind': 'document' if doc_like else 'media',
    }


def attachments(value, roots, *, cache_index=None, extract_docs=True):
    """Retain references, archive cached files/documents, and classify missing reasons."""
    found = []
    effective_roots = cache_index['roots'] if cache_index else roots

    def append(ref):
        meta = _extract_attachment_meta(ref)
        result = None
        for candidate_path in meta['paths']:
            result = archive_file(candidate_path, effective_roots, filename_hint=meta['filename'], extract=extract_docs)
            if result is not None:
                break
        if result is None:
            indexed = _pick_cached_candidate(meta, cache_index)
            if indexed is not None:
                result = archive_file(str(indexed), effective_roots, filename_hint=meta['filename'], extract=extract_docs)

        preview = ref.get('blurredPath') or ref.get('thumbpath')
        if result is None and isinstance(preview, str) and preview:
            result = archive_file(preview, effective_roots, filename_hint=meta['filename'], extract=False)
            if result and result['status'] == 'available':
                result.update(status='preview_available', original_available=False,
                              reason='preview_only_original_not_cached')

        extra = {k: meta[k] for k in ('filename', 'file_type', 'declared_size_bytes', 'space_id', 'file_id', 'kind')
                 if meta.get(k) is not None and meta['has_file_signal']}
        if result is not None:
            entry = {'reference': ref, **extra, **result}
        elif not meta['has_file_signal']:
            entry = {'reference': ref, 'status': 'card_reference_only'}
        elif (meta['declared_size_bytes'] or 0) > MAX_BYTES:
            entry = {'reference': ref, **extra, 'status': 'too_large', 'max_bytes': MAX_BYTES,
                     'reason': 'attachment_too_large'}
        else:
            now_ms = int(time.time() * 1000)
            if (meta['space_id'] or meta['file_id']) and meta['expire_ms'] and meta['expire_ms'] < now_ms:
                reason = 'dingtalk_space_file_expired'
            elif meta['space_id'] or meta['file_id']:
                reason = 'dingtalk_space_file_not_cached'
            elif meta['paths']:
                reason = 'dingtalk_local_file_missing'
            else:
                reason = 'dingtalk_media_not_cached'
            entry = {'reference': ref, **extra, 'status': 'not_downloaded', 'reason': reason}
        found.append(entry)

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
            if 'contentType' in item and ('mediaId' in item or 'filepath' in item or 'authMedia' in item or 'fileId' in item):
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
