"""任意文件类型测试：不限于 Office —— pdf / zip / 源码 / 图片 / 未知二进制都要能无损往返。

重点只有一条：**字节进、字节出，SHA-256 完全一致**。
预览（preview）是附赠信息，抽不出来不算失败。
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
import zipfile

from officepack import container as ct
from officepack import preview as pv


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tiny_pdf(text: str = "Hello PDF") -> bytes:
    """造一个最小的、流不压缩的 PDF —— 足够让抽取器走到 BT…ET 分支。"""
    body = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET" % text.encode("latin-1")
    return (b"%PDF-1.4\n1 0 obj\n<</Length " + str(len(body)).encode() + b">>\nstream\n"
            + body + b"\nendstream\nendobj\ntrailer\n<</Root 1 0 R>>\n%%EOF\n")


def _tiny_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("readme.txt", "hello zip")
        zf.writestr("src/Main.java", "public class Main {}")
    return buf.getvalue()


class AnyFileRoundTripTest(unittest.TestCase):
    """打包 → 还原，逐字节校验（不涉及二维码，跑得快）。"""

    def _roundtrip(self, name: str, raw: bytes, codec_name: str = "base91"):
        with tempfile.TemporaryDirectory() as tmp:
            pack = ct.pack_bytes(raw, name, codec_name=codec_name, compress="auto")
            text = ct.render(pack)
            results = ct.unpack_texts([text], tmp)
            path = results[0][0]
            with open(path, "rb") as fh:
                self.assertEqual(_sha(fh.read()), _sha(raw))
            return os.path.basename(path)

    def test_source_code_files(self):
        for name in ("README.md", "Main.java", "schema.sql", "app.py", "config.json"):
            with self.subTest(name=name):
                raw = ("// %s\n" % name + "x" * 500).encode("utf-8")
                self.assertEqual(self._roundtrip(name, raw), name)

    def test_pdf(self):
        self.assertEqual(self._roundtrip("doc.pdf", _tiny_pdf()), "doc.pdf")

    def test_zip(self):
        self.assertEqual(self._roundtrip("pack.zip", _tiny_zip()), "pack.zip")

    def test_rar_like_unknown_binary(self):
        """没有 rar 库也能打包 —— 只需要当二进制搬运，不解析。"""
        raw = b"Rar!\x1a\x07\x00" + os.urandom(2048)
        self.assertEqual(self._roundtrip("a.rar", raw), "a.rar")

    def test_binary_matches_with_encryption(self):
        raw = os.urandom(6 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            pack = ct.pack_bytes(raw, "blob.bin", codec_name="base64",
                                 cipher_name="aes-256-gcm", password="pw-123")
            results = ct.unpack_texts([ct.render(pack)], tmp, "pw-123")
            with open(results[0][0], "rb") as fh:
                self.assertEqual(_sha(fh.read()), _sha(raw))


class PreviewTest(unittest.TestCase):
    """预览抽取：能抽就抽，抽不出返回空串（绝不抛异常）。"""

    def test_text_preview(self):
        raw = "# 标题\n\n正文内容\n".encode("utf-8")
        text = pv.extract_preview(".md", raw)
        self.assertIn("标题", text)

    def test_gbk_text_preview(self):
        raw = "这是 GBK 编码的纯文本".encode("gbk")
        self.assertIn("GBK", pv.extract_preview(".txt", raw))

    def test_zip_listing(self):
        text = pv.extract_preview(".zip", _tiny_zip())
        self.assertIn("readme.txt", text)
        self.assertIn("Main.java", text)

    def test_pdf_text(self):
        self.assertIn("Hello PDF", pv.extract_preview(".pdf", _tiny_pdf()))

    def test_ole2_strings(self):
        """老 doc 是 OLE2，不是 ZIP —— 退化成扫可打印串。"""
        raw = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64 + b"Quarterly Report 2026" + b"\x00" * 64
        self.assertIn("Quarterly", pv.extract_preview(".doc", raw))

    def test_unknown_binary_does_not_raise(self):
        for ext in (".exe", ".rar", ".7z", ".mp4", ".dat", ""):
            with self.subTest(ext=ext):
                pv.extract_preview(ext, os.urandom(512))   # 只要不炸

    def test_empty_and_broken_input(self):
        self.assertEqual(pv.extract_preview(".docx", b"not a zip"), "")
        self.assertEqual(pv.extract_preview(".zip", b"\x00\x01"), "")
        self.assertEqual(pv.extract_preview(".txt", b""), "")

    def test_preview_is_truncated(self):
        raw = ("A" * (pv.MAX_PREVIEW_CHARS * 2)).encode()
        text = pv.extract_preview(".log", raw)
        self.assertLess(len(text), pv.MAX_PREVIEW_CHARS + 40)
        self.assertIn("预览已截断", text)

    def test_kind_labels(self):
        self.assertEqual(pv.kind_of(".docx"), "Office 文档")
        self.assertEqual(pv.kind_of(".pdf"), "PDF 文档")
        self.assertEqual(pv.kind_of(".java"), "纯文本/源码")
        self.assertEqual(pv.kind_of(".zip"), "压缩包")
        self.assertEqual(pv.kind_of(".xyz"), "任意二进制")


class PreviewInsideContainerTest(unittest.TestCase):
    def test_preview_written_into_container_text(self):
        raw = "# 项目说明\nhello".encode("utf-8")
        pack = ct.pack_bytes(raw, "README.md", codec_name="base32")
        text = ct.render(pack)
        self.assertIn(ct.PREVIEW_HEADER, text)
        self.assertIn("项目说明", text)

    def test_ascii_only_strips_preview(self):
        raw = "# 项目说明".encode("utf-8")
        pack = ct.pack_bytes(raw, "README.md", codec_name="base32", ascii_only=True)
        self.assertNotIn(ct.PREVIEW_HEADER, ct.render(pack))


if __name__ == "__main__":
    unittest.main()
