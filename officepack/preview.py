"""语义预览：从 OOXML 文档中提取可读纯文本。

仅用于人工阅读，不参与还原。老二进制格式（doc/xls/ppt）不提取，
不影响无损还原能力。
"""

from __future__ import annotations

import re
import zipfile

MAX_PREVIEW_CHARS = 4000

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
    names = [n for n in zf.namelist() if n == "word/document.xml"]
    return "\n".join(_strip_xml(zf.read(n)) for n in names)


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


_EXTRACTORS = {
    ".docx": _extract_docx,
    ".docm": _extract_docx,
    ".doc": _extract_docx,
    ".pptx": _extract_pptx,
    ".pptm": _extract_pptx,
    ".xlsx": _extract_xlsx,
    ".xlsm": _extract_xlsx,
    ".xls": _extract_xlsx,
}


def extract_preview(path_ext: str, raw: bytes) -> str:
    """返回可读预览文本；不支持或失败时返回空串。"""
    ext = (path_ext or "").lower()
    if ext not in _EXTRACTORS:
        return ""
    try:
        import io

        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            text = _EXTRACTORS[ext](zf)
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError):
        # 老二进制格式（OLE2）不是 ZIP，直接跳过预览
        return ""
    text = text.strip()
    if len(text) > MAX_PREVIEW_CHARS:
        text = text[:MAX_PREVIEW_CHARS] + "\n…（预览已截断）"
    return text
