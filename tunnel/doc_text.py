"""Signature-verified MIME detection and bounded document text extraction."""
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

MAX_EXTRACTED_CHARS = int(os.environ.get('MUSE_MAX_EXTRACTED_TEXT', '24000'))
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_TEXT_FILE_BYTES = 256 * 1024
OLE2_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'

OOXML_MIMES = {
    'word/document.xml': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xl/workbook.xml': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'ppt/presentation.xml': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}
OLE2_SUFFIX_MIMES = {
    '.doc': 'application/msword',
    '.xls': 'application/vnd.ms-excel',
    '.ppt': 'application/vnd.ms-powerpoint',
}
DOC_MIMES = {
    'application/pdf',
    'application/msword',
    'application/vnd.ms-excel',
    'application/vnd.ms-powerpoint',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'text/plain',
    'text/csv',
}
DOC_SUFFIXES = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.md', '.csv', '.tsv'}


def detect_mime(header: bytes, path: Path, filename: str = '') -> str:
    """Identify MIME from magic bytes and verified container structure."""
    for signature, kind in (
        (b'%PDF-', 'application/pdf'),
        (b'\x89PNG\r\n\x1a\n', 'image/png'),
        (b'\xff\xd8\xff', 'image/jpeg'),
        (b'GIF87a', 'image/gif'),
        (b'GIF89a', 'image/gif'),
    ):
        if header.startswith(signature):
            return kind
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'image/webp'
    suffix = Path(filename or path.name).suffix.lower()
    if header.startswith((b'PK\x03\x04', b'PK\x05\x06')):
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
            for marker, mime in OOXML_MIMES.items():
                if marker in names:
                    return mime
            return 'application/zip'
        except (OSError, zipfile.BadZipFile):
            return 'application/octet-stream'
    if header.startswith(OLE2_MAGIC):
        if suffix in OLE2_SUFFIX_MIMES:
            return OLE2_SUFFIX_MIMES[suffix]
        try:
            with path.open('rb') as f:
                probe = f.read(65536)
            if 'WordDocument'.encode('utf-16le') in probe:
                return 'application/msword'
            if 'Workbook'.encode('utf-16le') in probe or 'Book'.encode('utf-16le') in probe:
                return 'application/vnd.ms-excel'
            if 'PowerPoint Document'.encode('utf-16le') in probe:
                return 'application/vnd.ms-powerpoint'
        except OSError:
            pass
        return 'application/octet-stream'
    if suffix in ('.txt', '.md', '.csv', '.tsv') and header and b'\x00' not in header:
        for enc in ('utf-8-sig', 'utf-8', 'gb18030'):
            try:
                header.decode(enc)
                return 'text/csv' if suffix in ('.csv', '.tsv') else 'text/plain'
            except UnicodeDecodeError:
                continue
    return 'application/octet-stream'


def is_document(mime_type: str = '', filename: str = '') -> bool:
    return mime_type in DOC_MIMES or Path(filename).suffix.lower() in DOC_SUFFIXES


def _clean_text(text: str | None) -> tuple[str | None, bool]:
    if not text:
        return None, False
    lines = [re.sub(r'[ \t\r\f\v]+', ' ', line).strip() if '\t' not in line else line.rstrip()
             for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    compact = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()
    if not compact:
        return None, False
    truncated = len(compact) > MAX_EXTRACTED_CHARS
    return compact[:MAX_EXTRACTED_CHARS], truncated


def _local_tag(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _extract_docx_xml(xml_bytes: bytes) -> str:
    root = ET.fromstring(xml_bytes)
    body = next((el for el in root if _local_tag(el.tag) == 'body'), root)

    def para_text(p) -> str:
        parts = []
        for node in p.iter():
            t = _local_tag(node.tag)
            if t == 't' and node.text:
                parts.append(node.text)
            elif t == 'tab':
                parts.append('\t')
            elif t in ('br', 'cr'):
                parts.append('\n')
        return ''.join(parts).strip()

    blocks = []
    for child in body:
        kind = _local_tag(child.tag)
        if kind == 'p':
            line = para_text(child)
            if line:
                blocks.append(line)
        elif kind == 'tbl':
            for tr in child:
                if _local_tag(tr.tag) != 'tr':
                    continue
                cells = []
                for tc in tr:
                    if _local_tag(tc.tag) != 'tc':
                        continue
                    cell_lines = [para_text(p) for p in tc if _local_tag(p.tag) == 'p']
                    cells.append(' '.join(x for x in cell_lines if x))
                if any(cells):
                    blocks.append('\t'.join(cells))
    return '\n'.join(blocks)


def _col_index(ref: str) -> int:
    col = 0
    for ch in ref:
        if 'A' <= ch <= 'Z':
            col = col * 26 + (ord(ch) - ord('A') + 1)
        elif 'a' <= ch <= 'z':
            col = col * 26 + (ord(ch) - ord('a') + 1)
        else:
            break
    return max(0, col - 1)


def _extract_xlsx(zf: zipfile.ZipFile) -> str | None:
    names = set(zf.namelist())
    xml_targets = [n for n in names if n in ('xl/sharedStrings.xml', 'xl/workbook.xml')
                   or re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n)]
    if sum(zf.getinfo(n).file_size for n in xml_targets) > MAX_XML_BYTES:
        return None
    shared: list[str] = []
    if 'xl/sharedStrings.xml' in names:
        ss_root = ET.fromstring(zf.read('xl/sharedStrings.xml'))
        for si in ss_root:
            if _local_tag(si.tag) == 'si':
                shared.append(''.join(n.text for n in si.iter() if _local_tag(n.tag) == 't' and n.text))
    sheet_titles: list[str] = []
    if 'xl/workbook.xml' in names:
        wb_root = ET.fromstring(zf.read('xl/workbook.xml'))
        sheet_titles = [el.attrib.get('name', '') for el in wb_root.iter() if _local_tag(el.tag) == 'sheet']
    sheets = sorted(
        (n for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n)),
        key=lambda s: int(re.search(r'(\d+)\.xml$', s)[1]),
    )
    sections = []
    for idx, sheet_name in enumerate(sheets):
        ws_root = ET.fromstring(zf.read(sheet_name))
        rows_out = []
        for row in ws_root.iter():
            if _local_tag(row.tag) != 'row':
                continue
            vals: list[tuple[int, str]] = []
            for cell in row:
                if _local_tag(cell.tag) != 'c':
                    continue
                ctype = cell.attrib.get('t', '')
                ref = cell.attrib.get('r', '')
                pos = _col_index(ref) if ref else (vals[-1][0] + 1 if vals else 0)
                text = ''
                if ctype == 'inlineStr':
                    text = ''.join(n.text for n in cell.iter() if _local_tag(n.tag) == 't' and n.text)
                else:
                    v_node = next((n for n in cell if _local_tag(n.tag) == 'v'), None)
                    raw = (v_node.text or '').strip() if v_node is not None else ''
                    if ctype == 's' and raw.isdigit() and int(raw) < len(shared):
                        text = shared[int(raw)]
                    elif ctype == 'b':
                        text = 'TRUE' if raw == '1' else 'FALSE'
                    else:
                        text = raw
                text = text.replace('\t', ' ').replace('\n', ' ').strip()
                if text:
                    vals.append((pos, text))
            if vals:
                # Preserve adjacent column alignment up to a small gap; avoid huge sparse padding.
                rendered = []
                prev_col = vals[0][0]
                for col, text in vals:
                    gap = min(max(0, col - prev_col - 1), 3) if rendered else 0
                    rendered.extend([''] * gap)
                    rendered.append(text)
                    prev_col = col
                rows_out.append('\t'.join(rendered))
        if rows_out:
            title = sheet_titles[idx] if idx < len(sheet_titles) and sheet_titles[idx] else f'Sheet{idx + 1}'
            sections.append(f'[{title}]\n' + '\n'.join(rows_out))
    return '\n\n'.join(sections)


def _run_tool(cmd: list[str], timeout: int = 15) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout:
            return None
        return r.stdout[:MAX_EXTRACTED_CHARS * 4].decode('utf-8', 'replace')
    except (OSError, subprocess.TimeoutExpired):
        return None


def extract_document_text(path: Path, mime_type: str = '', filename: str = '') -> tuple[str | None, bool]:
    """Extract auxiliary readable text from office/PDF/plain-text files safely."""
    suffix = Path(filename or path.name).suffix.lower()
    raw_text: str | None = None
    try:
        if mime_type == OOXML_MIMES['word/document.xml'] or suffix == '.docx':
            with zipfile.ZipFile(path) as zf:
                targets = [n for n in zf.namelist()
                           if n == 'word/document.xml' or re.fullmatch(r'word/(header|footer)\d*\.xml', n)]
                if targets and sum(zf.getinfo(n).file_size for n in targets) <= MAX_XML_BYTES:
                    ordered = sorted(targets, key=lambda n: (0 if n == 'word/document.xml' else 1, n))
                    raw_text = '\n\n'.join(filter(None, (_extract_docx_xml(zf.read(n)) for n in ordered)))
        elif mime_type == OOXML_MIMES['xl/workbook.xml'] or suffix == '.xlsx':
            with zipfile.ZipFile(path) as zf:
                raw_text = _extract_xlsx(zf)
        elif mime_type == OOXML_MIMES['ppt/presentation.xml'] or suffix == '.pptx':
            with zipfile.ZipFile(path) as zf:
                slides = sorted(
                    (n for n in zf.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)),
                    key=lambda s: int(re.search(r'(\d+)\.xml$', s)[1]),
                )
                if slides and sum(zf.getinfo(n).file_size for n in slides) <= MAX_XML_BYTES:
                    parts = []
                    for s in slides:
                        num = int(re.search(r'(\d+)\.xml$', s)[1])
                        tree = ET.fromstring(zf.read(s))
                        body = '\n'.join(e.text.strip() for e in tree.iter()
                                         if _local_tag(e.tag) == 't' and e.text and e.text.strip())
                        if body:
                            parts.append(f'[Slide {num}]\n{body}')
                    raw_text = '\n\n'.join(parts)
        elif mime_type == 'application/pdf' or suffix == '.pdf':
            raw_text = _run_tool(['pdftotext', '-enc', 'UTF-8', '-nopgbrk', str(path), '-'])
        elif mime_type == 'application/msword' or suffix == '.doc':
            raw_text = _run_tool(['antiword', str(path)]) or _run_tool(['catdoc', str(path)])
        elif mime_type == 'application/vnd.ms-excel' or suffix == '.xls':
            raw_text = _run_tool(['xls2csv', str(path)])
        elif mime_type in ('text/plain', 'text/csv') or suffix in ('.txt', '.md', '.csv', '.tsv'):
            with path.open('rb') as f:
                data = f.read(MAX_TEXT_FILE_BYTES + 1)
            if b'\x00' not in data[:4096]:
                truncated_bytes = len(data) > MAX_TEXT_FILE_BYTES
                sample = data[:MAX_TEXT_FILE_BYTES]
                for enc in ('utf-8-sig', 'utf-8', 'gb18030'):
                    try:
                        decoded = sample.decode(enc)
                        text, truncated = _clean_text(decoded)
                        return text, (truncated or truncated_bytes)
                    except UnicodeDecodeError:
                        continue
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return None, False
    return _clean_text(raw_text)


def cached_extract(path: Path, asset_id: str, mime_type: str = '', filename: str = '') -> dict:
    """Reuse persisted extracted text by SHA-256 asset_id to avoid repeat CPU/IO."""
    if not is_document(mime_type, filename):
        return {}
    from api import connection
    with connection() as c:
        row = c.execute('SELECT text, truncated FROM asset_texts WHERE asset_id=?', (asset_id,)).fetchone()
    if row is not None:
        return {'extracted_text': row['text'], 'text_truncated': bool(row['truncated'])} if row['text'] else {}
    text, truncated = extract_document_text(path, mime_type, filename)
    if not text:
        return {}
    with connection() as c:
        c.execute('INSERT OR REPLACE INTO asset_texts VALUES (?,?,?)', (asset_id, text, int(truncated)))
    return {'extracted_text': text, 'text_truncated': truncated}
