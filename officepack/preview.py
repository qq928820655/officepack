"""语义预览：从各种文件里抽出一点可读内容，贴在容器文本里。

仅用于人工阅读，**不参与还原** —— 预览抽不出来或者抽歪了都不影响无损往返，
因为还原走的是 ``Payload`` 里的原始字节 + SHA-256 校验。

支持的路子（都只依赖标准库，抽不出来就返回空串）：
    纯文本 / 源码     按 BOM → UTF-8 → GBK → latin-1 逐级解码
    OOXML（docx…）    解 ZIP，剥 XML 标签取正文
    PDF               解 FlateDecode 流，取 BT…ET 里的字符串
    ZIP 系（zip/jar…）列目录 + 条目数
    OLE2（doc/xls/ppt）扫可打印串，聊胜于无
    图片              读文件头拿格式与像素尺寸
"""

from __future__ import annotations

import io
import re
import zipfile
import zlib

MAX_PREVIEW_CHARS = 4000

# --------------------------------------------------------------------------
# 扩展名分组（GUI / CLI 的过滤器也复用这些表）
# --------------------------------------------------------------------------
#: 老二进制 + OOXML 文档
OOXML_EXTS = (".docx", ".docm", ".dotx", ".pptx", ".pptm", ".xlsx", ".xlsm")
OLE2_EXTS = (".doc", ".dot", ".xls", ".xlt", ".ppt", ".pot", ".msg")

#: 纯文本 / 源码 / 配置，能整段当预览读
TEXT_EXTS = (
    ".txt", ".text", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".properties", ".env", ".sql", ".html", ".htm", ".xhtml",
    ".css", ".scss", ".less", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx",
    ".vue", ".svelte", ".py", ".pyi", ".java", ".kt", ".kts", ".scala",
    ".groovy", ".gradle", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".pl", ".lua", ".swift", ".m", ".mm",
    ".sh", ".bash", ".zsh", ".bat", ".cmd", ".ps1", ".tex", ".srt", ".vtt",
    ".diff", ".patch", ".po", ".r", ".jl", ".dart", ".sol", ".proto",
)

#: ZIP 容器（能列目录；OOXML 会先按文档抽正文）
ZIP_EXTS = (
    ".zip", ".jar", ".war", ".ear", ".apk", ".aar", ".whl", ".egg",
    ".nupkg", ".vsix", ".xpi", ".crx", ".ipa", ".odt", ".ods", ".odp",
    ".epub", ".kmz", ".3mf",
)

#: 其它压缩包（标准库解不开，只报个类型）
ARCHIVE_EXTS = (".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".tbz2",
                ".xz", ".txz", ".zst", ".lzma", ".cab", ".iso")

PDF_EXTS = (".pdf",)

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff")

MEDIA_EXTS = (
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".amr",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
)

#: 常用二进制可执行 / 库
EXEC_EXTS = (".exe", ".dll", ".so", ".dylib", ".msi", ".bin", ".dat", ".db",
             ".sqlite", ".class", ".pyc", ".wasm", ".jar")

#: 判「能不能读出内容」的所有文本类
READABLE_TEXT_EXTS = TEXT_EXTS + (".svg",)


# --------------------------------------------------------------------------
# 纯文本
# --------------------------------------------------------------------------
def _decode(raw: bytes) -> str:
    """按 BOM → UTF-8 → GBK → latin-1 逐级降级解码，尽量不丢内容。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def _extract_text(raw: bytes) -> str:
    text = _decode(raw).replace("\x00", "")
    # 行尾统一，砍掉右侧空白，别让预览撑出一堆空行
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# OOXML
# --------------------------------------------------------------------------
_NS_TEXT_TAGS = (
    "w:t",   # Word
    "a:t",   # PowerPoint
    "t",     # Excel 共享字符串
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_xml(xml_bytes: bytes) -> str:
    try:
        text = xml_bytes.decode("utf-8", "replace")
    except Exception:
        return ""
    # 段落/换行标记先替换为换行，再剥标签
    text = re.sub(r"</w:p>|</a:p>|<w:br\s*/>|<a:br\s*/>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _extract_docx(zf: zipfile.ZipFile) -> str:
    if "word/document.xml" not in zf.namelist():
        return ""
    return _strip_xml(zf.read("word/document.xml"))


def _extract_pptx(zf: zipfile.ZipFile) -> str:
    names = sorted(
        (n for n in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
        key=lambda s: int(re.search(r"(\d+)", s.rsplit("/", 1)[-1]).group(1)),
    )
    parts = []
    for n in names:
        body = _strip_xml(zf.read(n))
        if body:
            parts.append("[%s]\n%s" % (n.rsplit("/", 1)[-1], body))
    return "\n".join(parts)


def _extract_xlsx(zf: zipfile.ZipFile) -> str:
    parts = []
    if "xl/sharedStrings.xml" in zf.namelist():
        parts.append(_strip_xml(zf.read("xl/sharedStrings.xml")))
    sheets = sorted(n for n in zf.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    for n in sheets[:8]:
        body = _strip_xml(zf.read(n))
        if body:
            parts.append("[%s]\n%s" % (n.rsplit("/", 1)[-1], body))
    return "\n".join(parts)


_OOXML_EXTRACTORS = {
    ".docx": _extract_docx,
    ".docm": _extract_docx,
    ".dotx": _extract_docx,
    ".pptx": _extract_pptx,
    ".pptm": _extract_pptx,
    ".xlsx": _extract_xlsx,
    ".xlsm": _extract_xlsx,
}


# --------------------------------------------------------------------------
# ZIP 目录
# --------------------------------------------------------------------------
def _extract_zip_listing(zf: zipfile.ZipFile, limit: int = 40) -> str:
    infos = zf.infolist()
    total = len(infos)
    raw_size = sum(i.file_size for i in infos)
    packed = sum(i.compress_size for i in infos)
    head = "共 %d 个条目，解压后 %s，压缩包 %s" % (
        total, _human(raw_size), _human(packed))
    rows = []
    for info in infos[:limit]:
        rows.append("  %10s  %s" % (_human(info.file_size), info.filename))
    if total > limit:
        rows.append("  … 另有 %d 个条目未列出" % (total - limit))
    return "\n".join([head] + rows)


# --------------------------------------------------------------------------
# PDF：解流 + 抠 BT…ET 里的字符串
# --------------------------------------------------------------------------
_PDF_STRING_RE = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>")
_PDF_ESCAPE_RE = re.compile(rb"\\([nrtbf()\\]|[0-7]{1,3})")
_PDF_ESCAPES = {
    b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
    b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\",
}


def _pdf_unescape(token: bytes) -> bytes:
    def repl(m):
        g = m.group(1)
        if g in _PDF_ESCAPES:
            return _PDF_ESCAPES[g]
        try:
            return bytes([int(g, 8) & 0xFF])
        except ValueError:
            return b""
    return _PDF_ESCAPE_RE.sub(repl, token)


def _pdf_content_streams(raw: bytes):
    """把所有能解开的 stream 逐个吐出来（解不开的跳过）。"""
    pos = 0
    while True:
        m = re.compile(rb"stream\r?\n").search(raw, pos)
        if not m:
            return
        start = m.end()
        end = raw.find(b"endstream", start)
        if end < 0:
            return
        blob = raw[start:end]
        pos = end + len(b"endstream")
        try:
            yield zlib.decompress(blob)
        except zlib.error:
            # 有些 PDF 流不压缩，直接给原文
            if b"BT" in blob:
                yield blob


def _extract_pdf(raw: bytes) -> str:
    chunks: list[str] = []
    seen = 0
    for data in _pdf_content_streams(raw):
        # 只处理文本对象内部
        for block in re.findall(rb"BT(.*?)ET", data, re.S):
            out = []
            for tok in _PDF_STRING_RE.findall(block):
                if tok.startswith(b"<"):
                    hexpart = re.sub(rb"\s", b"", tok[1:-1])
                    if len(hexpart) % 2:
                        hexpart += b"0"
                    try:
                        val = bytes.fromhex(hexpart.decode("ascii"))
                    except ValueError:
                        continue
                else:
                    val = _pdf_unescape(tok[1:-1])
                # PDF 里的中文常是 UTF-16BE，粗判一下
                if val[:2] in (b"\xfe\xff",) or (len(val) >= 4 and val[1:2] == b"\x00"):
                    try:
                        val = val.decode("utf-16-be", "ignore").encode("utf-8")
                    except Exception:
                        pass
                out.append(val.decode("utf-8", "replace"))
            line = "".join(out).strip()
            if line:
                chunks.append(line)
        seen += 1
        if seen >= 200 or sum(len(c) for c in chunks) > MAX_PREVIEW_CHARS * 2:
            break
    return "\n".join(chunks).strip()


# --------------------------------------------------------------------------
# OLE2 老格式 / 任意二进制：扫可打印串
# --------------------------------------------------------------------------
def _scan_strings(raw: bytes, min_len: int = 4) -> str:
    found: list[str] = []
    # ASCII 串
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, raw):
        found.append(m.group().decode("latin-1"))
        if len(found) > 400:
            break
    # UTF-16LE 串（Word 正文常见）
    if len(found) < 20:
        for m in re.finditer(rb"(?:[\x20-\x7e]\x00){%d,}" % min_len, raw):
            try:
                found.append(m.group().decode("utf-16-le"))
            except Exception:
                pass
            if len(found) > 800:
                break
    # 去重保序，太长的噪声行截断
    out, seen = [], set()
    for s in found:
        s = s.strip()
        if len(s) < min_len or s in seen:
            continue
        seen.add(s)
        out.append(s[:200])
    return "\n".join(out).strip()


# --------------------------------------------------------------------------
# 图片
# --------------------------------------------------------------------------
def _extract_image(raw: bytes) -> str:
    try:
        from PIL import Image
    except Exception:
        return ""
    try:
        with Image.open(io.BytesIO(raw)) as im:
            return "%s  %d × %d 像素  %s" % (
                im.format or "?", im.width, im.height, im.mode or "")
    except Exception:
        return ""


def _human(n: int) -> str:
    step = 1024.0
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < step or unit == "TB":
            return ("%d %s" % (val, unit)) if unit == "B" else ("%.1f %s" % (val, unit))
        val /= step
    return "%d B" % n


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def extract_preview(path_ext: str, raw: bytes) -> str:
    """返回可读预览文本；不支持的类型或抽取失败时返回空串。

    预览只是附赠的人眼参考，任何异常都不该让打包失败。
    """
    ext = (path_ext or "").lower()
    text = ""
    try:
        if ext in TEXT_EXTS:
            text = _extract_text(raw)
        elif ext in _OOXML_EXTRACTORS:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                text = _OOXML_EXTRACTORS[ext](zf)
        elif ext == ".pdf":
            text = _extract_pdf(raw)
        elif ext in IMAGE_EXTS:
            text = _extract_image(raw)
        elif ext in ZIP_EXTS:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                text = _extract_zip_listing(zf)
        elif ext in OLE2_EXTS:
            text = _scan_strings(raw)
        elif ext in ARCHIVE_EXTS or ext in MEDIA_EXTS or ext in EXEC_EXTS:
            text = ""   # 标准库解不开，宁可不给预览
        else:
            # 未知扩展名：是 ZIP 就当压缩包列目录，否则扫串兜底
            if zipfile.is_zipfile(io.BytesIO(raw)):
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    text = _extract_zip_listing(zf)
            else:
                text = _scan_strings(raw)
    except Exception:
        # BadZipFile / KeyError / OSError / zlib.error 一律当「没有预览」
        return ""

    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > MAX_PREVIEW_CHARS:
        text = text[:MAX_PREVIEW_CHARS] + "\n…（预览已截断）"
    return text


def kind_of(path_ext: str) -> str:
    """给 GUI 的类型列用：一句话说明这是什么文件。"""
    ext = (path_ext or "").lower()
    if ext in _OOXML_EXTRACTORS:
        return "Office 文档"
    if ext in OLE2_EXTS:
        return "Office 老格式"
    if ext == ".pdf":
        return "PDF 文档"
    if ext in TEXT_EXTS:
        return "纯文本/源码"
    if ext in IMAGE_EXTS:
        return "图片"
    if ext in ZIP_EXTS:
        return "压缩包"
    if ext in ARCHIVE_EXTS:
        return "压缩包"
    if ext in MEDIA_EXTS:
        return "音视频"
    if ext in EXEC_EXTS:
        return "可执行/库"
    return "任意二进制"
