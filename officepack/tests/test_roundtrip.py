"""往返一致性测试：打包 → 还原 → SHA-256 必须完全一致。

运行： python -m unittest discover -s officepack/tests -v
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import officepack as op  # noqa: E402


def make_docx(paragraphs: list[str]) -> bytes:
    buf = io.BytesIO()
    body = "".join(
        "<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % p.replace("&", "&amp;") for p in paragraphs
    )
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>%s</w:body></w:document>" % body,
        )
        zf.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
    return buf.getvalue()


def make_xlsx(cells: list[str]) -> bytes:
    buf = io.BytesIO()
    sheet = "".join("<row><c t=\"inlineStr\"><is><t>%s</t></is></c></row>" % c for c in cells)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("xl/worksheets/sheet1.xml", '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>%s</sheetData></worksheet>' % sheet)
    return buf.getvalue()


def make_pptx(slides: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        for i, text in enumerate(slides, 1):
            zf.writestr(
                "ppt/slides/slide%d.xml" % i,
                '<?xml version="1.0"?><p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>%s</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>" % text,
            )
    return buf.getvalue()


class RoundTripTest(unittest.TestCase):
    def _roundtrip(self, raw: bytes, name: str = "样本.docx", **kwargs) -> dict:
        packed = op.pack_bytes(raw, name, **kwargs)
        text = op.render(packed)
        self.assertIn(op.BEGIN, text)
        self.assertIn(op.END, text)
        # 载荷段必须是纯 ASCII，能通过任何只允许文本的渠道
        lines = text.splitlines()
        start = lines.index(op.PAYLOAD_HEADER)
        payload = "".join(
            l for l in lines[start + 1:] if l.strip() and l.strip() != op.END
        )
        payload.encode("ascii")
        result = op.unpack_text(text, temp_password(kwargs))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), hashlib.sha256(result.raw).hexdigest())
        self.assertTrue(result.verified)
        self.assertEqual(len(result.raw), len(raw))
        return {"text": text, "meta": result.meta}

    def test_all_codecs(self):
        raw = make_docx(["编码器逐个验证", "第二段 中文 & 符号 <tag>"])
        for name in ("base91", "base85", "base64", "base32", "hex"):
            with self.subTest(codec=name):
                res = self._roundtrip(raw, codec_name=name, compress="auto")
                self.assertEqual(res["meta"]["codec"], name)

    def test_all_compressors(self):
        raw = b"AAAABBBBCCCC" * 5000 + os.urandom(4096)
        for mode in ("none", "zlib", "bz2", "lzma", "auto"):
            with self.subTest(compress=mode):
                self._roundtrip(raw, name="blob.bin", compress=mode)

    def test_encrypted(self):
        raw = make_docx(["机密文件", "口令加密"])
        for compress in ("auto", "none"):
            with self.subTest(compress=compress):
                res = self._roundtrip(
                    raw,
                    cipher_name="aes-256-gcm",
                    password="p@ss 中文口令",
                    compress=compress,
                )
                self.assertEqual(res["meta"]["cipher"]["alg"], "aes-256-gcm")

    def test_wrong_password_rejected(self):
        raw = b"secret data"
        packed = op.pack_bytes(raw, "s.bin", cipher_name="aes-256-gcm", password="right")
        text = op.render(packed)
        with self.assertRaises(op.CryptoError):
            op.unpack_text(text, "wrong")

    def test_tamper_detected(self):
        raw = b"0123456789" * 100
        packed = op.pack_bytes(raw, "t.bin", codec_name="base32", cipher_name="aes-256-gcm",
                               password="k")
        text = op.render(packed)
        lines = text.splitlines()
        idx = lines.index(op.PAYLOAD_HEADER) + 1
        # 篡改一个载荷字符
        lines[idx] = ("B" if lines[idx][0] != "B" else "C") + lines[idx][1:]
        with self.assertRaises((op.CryptoError, op.ContainerError, op.CodecError)):
            op.unpack_text("\n".join(lines), "k")

    def test_empty_and_tiny(self):
        for raw in (b"", b"\x00", b"A", b"\xff" * 59, b"\xff" * 60, b"\xff" * 61):
            with self.subTest(size=len(raw)):
                self._roundtrip(raw, name="边界.bin", compress="auto")

    def test_multichunk(self):
        # 跨 1MiB 加密块边界
        for size in (1 << 20, (1 << 20) + 1, (1 << 20) * 2 + 12345):
            with self.subTest(size=size):
                raw = os.urandom(size)
                self._roundtrip(raw, name="big.bin", cipher_name="aes-256-gcm",
                                password="pw", compress="none")

    def test_office_samples(self):
        samples = {
            "报告.docx": make_docx(["第一章 总述", "数据见附表"]),
            "台账.xlsx": make_xlsx(["项目", "金额", "2026"]),
            "汇报.pptx": make_pptx(["第一页 封面", "第二页 结论"]),
            "老格式.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + os.urandom(5000),
        }
        for name, raw in samples.items():
            with self.subTest(name=name):
                self._roundtrip(raw, name=name, codec_name="base91",
                                cipher_name="aes-256-gcm", password="x")

    def test_preview_extracted(self):
        raw = make_docx(["可读预览内容 ABC", "第二段"])
        packed = op.pack_bytes(raw, "预览.docx", preview=True)
        self.assertIn("可读预览内容", packed.preview)
        text = op.render(packed)
        self.assertIn("可读预览内容", text)

    def test_pack_unpack_files(self):
        raw = make_xlsx(["文件级测试"])
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "台账 2026.xlsx")
            with open(src, "wb") as fh:
                fh.write(raw)
            outs, meta = op.pack_file(src, codec_name="base32", cipher_name="aes-256-gcm",
                                     password="pw")
            out = outs[0]
            self.assertTrue(os.path.exists(out))
            dest_dir = os.path.join(tmp, "restore")
            restored, meta2 = op.unpack_file(out, dest_dir, "pw")
            self.assertTrue(os.path.basename(restored).startswith("台账 2026"))
            with open(restored, "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(),
                                 hashlib.sha256(raw).hexdigest())

    def test_custom_alphabet(self):
        raw = b"custom codec test" * 20
        alphabet = "ZYXWVUTSRQPONMLKJIHGFEDCBAzyxwvutsrqponmlkjihgfedcba9876543210~_"
        packed = op.pack_bytes(raw, "c.bin", codec_name="custom", alphabet=alphabet,
                               compress="none")
        text = op.render(packed)
        result = op.unpack_text(text)
        self.assertEqual(result.raw, raw)

    def test_text_size_ordering(self):
        """base91 文本应比 base32 明显更短。"""
        raw = make_docx(["体积对比" * 200])
        small = op.render(op.pack_bytes(raw, "x.docx", codec_name="base91", compress="auto"))
        large = op.render(op.pack_bytes(raw, "x.docx", codec_name="base32", compress="auto"))
        self.assertLess(len(small), len(large))

    def test_cjk_codec(self):
        """汉字编码：字符数应比 base91 少一半以上，且能完整还原。"""
        raw = make_docx(["汉字编码压缩测试" * 80, "第二段内容"])
        cjk = op.pack_bytes(raw, "中文文档.docx", codec_name="cjk", compress="max")
        b91 = op.pack_bytes(raw, "中文文档.docx", codec_name="base91", compress="max")
        self.assertLess(cjk.meta["text_chars"], b91.meta["text_chars"] * 0.55)
        text = op.render(cjk)
        self.assertEqual(op.unpack_text(text).raw, raw)

    def test_segments_roundtrip(self):
        """分段：每段不超限，乱序合并后完整还原。"""
        raw = make_docx(["分段传输测试" * 400]) + os.urandom(30000)
        packed = op.pack_bytes(
            raw, "分段.docx", codec_name="cjk", compress="max",
            cipher_name="aes-256-gcm", password="pw",
        )
        segs = op.split_pack(packed, max_chars=10000)
        self.assertGreater(len(segs), 1)
        for seg in segs:
            self.assertLessEqual(sum(len(l) for l in seg.splitlines()), 10000)
            self.assertTrue(seg.startswith(op.SEG_PREFIX))

        import random

        shuffled = segs[:]
        random.shuffle(shuffled)
        merged = op.join_segments(op.parse_segments("".join(shuffled)))
        result = op.unpack_text(merged, "pw")
        self.assertEqual(result.raw, raw)

    def test_segments_by_parts(self):
        raw = os.urandom(40000)
        packed = op.pack_bytes(raw, "份数.bin", codec_name="cjk", compress="none")
        segs = op.split_pack(packed, parts=5)
        self.assertEqual(len(segs), 5)
        merged = op.join_segments(op.parse_segments("\n".join(segs)))
        self.assertEqual(op.unpack_text(merged).raw, raw)

    def test_segments_missing_detected(self):
        raw = os.urandom(30000)
        packed = op.pack_bytes(raw, "缺段.bin", codec_name="cjk", compress="none")
        segs = op.split_pack(packed, max_chars=5000)
        self.assertGreater(len(segs), 2)
        with self.assertRaises(op.ContainerError):
            op.join_segments(op.parse_segments("".join(segs[:-1])))

    def test_segment_corruption_detected(self):
        raw = os.urandom(12000)
        packed = op.pack_bytes(raw, "损坏.bin", codec_name="cjk", compress="none")
        segs = op.split_pack(packed, max_chars=4000)
        broken = segs[0].splitlines()
        # 篡改第 1 段尾部一个载荷字符
        line = broken[-1]
        broken[-1] = ("一" if line[0] != "一" else "二") + line[1:]
        with self.assertRaises(op.ContainerError):
            op.join_segments(op.parse_segments("\n".join(broken)))

    def test_segments_in_single_file(self):
        """多段粘进同一个文件（中间夹空行和无关文字）也能解析。"""
        raw = os.urandom(9000)
        packed = op.pack_bytes(raw, "单文件.bin", codec_name="cjk", compress="none")
        segs = op.split_pack(packed, max_chars=3000)
        blob = "\n\n".join(["前后无关的说明文字", *segs, "结尾"])
        merged = op.join_segments(op.parse_segments(blob))
        self.assertEqual(op.unpack_text(merged).raw, raw)


class PasteUnpackTest(unittest.TestCase):
    """手动粘贴文本还原：完整容器、单个分片、多分片混在一个文本框。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="opk_paste_")
        self.tmp = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def test_single_container(self):
        raw = make_docx(["粘贴还原测试"])
        packed = op.pack_bytes(raw, "粘贴.docx")
        path, meta = op.unpack_pasted(op.render(packed), self.tmp)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), raw)
        self.assertEqual(os.path.basename(path), "粘贴.docx")

    def test_multi_segments_jumbled(self):
        """乱序 + 每段后夹说明文字 + 首尾直接相接，仍要能还原。"""
        raw = make_docx(["分段粘贴" * 300]) + os.urandom(20000)
        packed = op.pack_bytes(raw, "混合.docx", codec_name="cjk", compress="auto")
        segs = op.split_pack(packed, max_chars=2000)
        self.assertGreater(len(segs), 3)
        mixed: list[str] = []
        for idx in reversed(range(len(segs))):
            mixed.append(segs[idx])
            mixed.append("——以上是第 %d 段的分隔说明，请忽略——\n" % (idx + 1))
        path, _ = op.unpack_pasted("".join(mixed), self.tmp)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), raw)

    def test_missing_segment_rejected(self):
        raw = make_docx(["缺段" * 400]) + os.urandom(10000)
        packed = op.pack_bytes(raw, "缺段.docx", codec_name="cjk")
        segs = op.split_pack(packed, max_chars=2000)
        self.assertGreater(len(segs), 2)
        with self.assertRaises(op.ContainerError):
            op.unpack_pasted("".join(segs[1:]), self.tmp)

    def test_encrypted_paste(self):
        raw = make_docx(["加密粘贴"])
        packed = op.pack_bytes(raw, "加密.docx",
                               cipher_name="aes-256-gcm", password="s3cret")
        text = op.render(packed)
        with self.assertRaises(op.CryptoError):
            op.unpack_pasted(text, self.tmp, "wrong")
        path, _ = op.unpack_pasted(text, self.tmp, "s3cret")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), raw)

    def test_unpack_texts_merges_segments(self):
        """多段分散在不同文本里（模拟分两次粘贴），一次调用合并还原。"""
        raw = make_docx(["多文本来源" * 200]) + os.urandom(8000)
        packed = op.pack_bytes(raw, "多文本.docx", codec_name="cjk")
        segs = op.split_pack(packed, max_chars=1500)
        half = max(1, len(segs) // 2)
        results = op.unpack_texts(["".join(segs[:half]), "".join(segs[half:])], self.tmp)
        self.assertEqual(len(results), 1)
        with open(results[0][0], "rb") as fh:
            self.assertEqual(fh.read(), raw)


def temp_password(kwargs: dict) -> str | None:
    return kwargs.get("password")


if __name__ == "__main__":
    unittest.main(verbosity=2)
