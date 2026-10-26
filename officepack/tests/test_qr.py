"""二维码通道测试：生成 → 截图 → 扫码 → 还原，全链路逐字节校验。"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest

from officepack import container as ct
from officepack import qr


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class QRFramesTest(unittest.TestCase):
    """分帧逻辑（不涉及图片，跑得快）。"""

    def _pack(self, raw: bytes, name="a.bin", **kw):
        kw.setdefault("codec_name", "base32")
        kw.setdefault("compress", "none")
        kw.setdefault("preview", False)
        return ct.pack_bytes(raw, name, **kw)

    def test_single_frame_carries_meta(self):
        frames = qr.build_frames(self._pack(b"hello world"))
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].startswith(ct.SEG_PREFIX))
        self.assertIn(ct.META_PREFIX, frames[0])

    def test_multi_frame_splits_and_keeps_meta_in_first(self):
        raw = os.urandom(20 * 1024)
        frames = qr.build_frames(self._pack(raw))
        self.assertGreater(len(frames), 3)
        self.assertIn(ct.META_PREFIX, frames[0])
        for frame in frames[1:]:
            self.assertNotIn(ct.META_PREFIX, frame)

    def test_frames_respect_bit_budget(self):
        """每帧都要能真正编成二维码 —— 字符数没超但比特数超了就会在这里炸。"""
        import qrcode

        raw = os.urandom(24 * 1024)
        for ec in ("L", "M", "Q", "H"):
            frames = qr.build_frames(self._pack(raw, cipher_name="aes-256-gcm", password="pw"), ec=ec)
            for frame in frames:
                q = qrcode.QRCode(error_correction=qr._ec_const(ec), border=4, box_size=1)
                q.add_data(frame, optimize=20)
                q.make(fit=True)   # 超容量会抛异常
                self.assertLessEqual(q.version, 40)

    def test_any_codec_is_allowed(self):
        """二维码不再锁 base32：任意编码器都能出帧，也要能原样解码还原。"""
        raw = os.urandom(3 * 1024)
        for codec_name in ("base32", "hex", "base91", "base64", "base85", "cjk"):
            with self.subTest(codec=codec_name):
                pack = self._pack(raw, codec_name=codec_name)
                frames = qr.build_frames(pack)
                self.assertEqual(frames[0].count(ct.META_PREFIX), 1)
                with tempfile.TemporaryDirectory() as tmp:
                    results = ct.unpack_texts(frames, tmp)
                    with open(results[0][0], "rb") as fh:
                        self.assertEqual(_sha(fh.read()), _sha(raw))

    def test_alnum_codec_needs_fewer_frames(self):
        """base32 / hex 落在字母数字模式，同样体积的帧数应少于字节模式编码器。"""
        raw = os.urandom(16 * 1024)
        alnum = qr.build_frames(self._pack(raw, codec_name="base32"))
        byte_mode = qr.build_frames(self._pack(raw, codec_name="base64"))
        self.assertLess(len(alnum), len(byte_mode))
        self.assertGreater(qr.overhead_ratio("base64"), 1.0)
        self.assertAlmostEqual(qr.overhead_ratio("base32"), 1.0)

    def test_rejects_unknown_ec_level(self):
        with self.assertRaises(qr.QRError):
            qr.build_frames(self._pack(b"abc"), ec="Z")

    def test_chars_per_frame_override(self):
        raw = os.urandom(8 * 1024)
        loose = qr.build_frames(self._pack(raw))
        tight = qr.build_frames(self._pack(raw), chars_per_frame=500)
        self.assertGreater(len(tight), len(loose))

    def test_segments_reassemble_without_images(self):
        """纯文本路径：帧文本直接合并也要能还原（不依赖扫码）。"""
        raw = os.urandom(12 * 1024)
        frames = qr.build_frames(self._pack(raw))
        with tempfile.TemporaryDirectory() as tmp:
            results = ct.unpack_texts(frames, tmp)
            path = results[0][0]
            with open(path, "rb") as fh:
                self.assertEqual(_sha(fh.read()), _sha(raw))


class QRImageRoundTripTest(unittest.TestCase):
    """真出图、真扫码。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opk_qr_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pack_bytes(self, raw: bytes, name="样本.bin", password=None):
        return ct.pack_bytes(
            raw, name, codec_name="base32", compress="none", preview=False,
            cipher_name="aes-256-gcm" if password else "none", password=password,
        )

    def test_single_qr_roundtrip(self):
        raw = b"officepack qr test payload " * 20
        frames = qr.build_frames(self._pack_bytes(raw, "小.txt"))
        res = qr.render_images(frames, self.tmp, "小.txt", ec="L", sheet=True)
        self.assertEqual(len(res["images"]), 1)
        self.assertIsNone(res["sheet"])          # 单帧不拼总览图

        texts = qr.decode_images(res["images"])
        self.assertEqual(texts, frames)

        results = qr.unpack_images(res["images"], out_dir=self.tmp)
        with open(results[0][0], "rb") as fh:
            self.assertEqual(_sha(fh.read()), _sha(raw))

    def test_multi_qr_via_sheet(self):
        raw = os.urandom(16 * 1024)
        frames = qr.build_frames(self._pack_bytes(raw), ec="L")
        self.assertGreater(len(frames), 1)
        self.assertLessEqual(len(frames), qr.MAX_SHEET_TILES)

        res = qr.render_images(frames, self.tmp, "多帧.bin", ec="L", sheet=True)
        self.assertIsNotNone(res["sheet"])

        texts = qr.decode_images([res["sheet"]])
        self.assertEqual(sorted(texts), sorted(frames))

        results = qr.unpack_images([res["sheet"]], out_dir=self.tmp)
        with open(results[0][0], "rb") as fh:
            self.assertEqual(_sha(fh.read()), _sha(raw))

    def test_too_many_frames_pages_the_sheet(self):
        """帧数超过一页时不再放弃总览图，而是分页 —— 19 帧 = 2 张图。"""
        raw = os.urandom(48 * 1024)
        frames = qr.build_frames(self._pack_bytes(raw), ec="L")
        self.assertGreater(len(frames), qr.MAX_SHEET_TILES)
        res = qr.render_images(frames, self.tmp, "大.bin", ec="L", sheet=True)
        self.assertEqual(len(res["images"]), len(frames))
        expect_pages = -(-len(frames) // qr.MAX_SHEET_TILES)
        self.assertEqual(len(res["sheets"]), expect_pages)
        self.assertEqual(res["sheet"], res["sheets"][0])

        # 逐页扫码，拼起来必须是完整的帧集合（顺序无关）
        texts = []
        for page in res["sheets"]:
            texts.extend(qr.decode_images([page]))
        self.assertEqual(sorted(texts), sorted(frames))

    def test_encrypted_qr_roundtrip(self):
        raw = os.urandom(4 * 1024)
        frames = qr.build_frames(self._pack_bytes(raw, "密.bin", password="口令123"))
        res = qr.render_images(frames, self.tmp, "密.bin", ec="L", sheet=False)
        results = qr.unpack_images(res["images"], out_dir=self.tmp, password="口令123")
        with open(results[0][0], "rb") as fh:
            self.assertEqual(_sha(fh.read()), _sha(raw))

    def test_wrong_password_fails(self):
        raw = os.urandom(1024)
        frames = qr.build_frames(self._pack_bytes(raw, "密.bin", password="right"))
        res = qr.render_images(frames, self.tmp, "密.bin", ec="L", sheet=False)
        with self.assertRaises(Exception):
            qr.unpack_images(res["images"], out_dir=self.tmp, password="wrong")

    def test_missing_frame_is_reported(self):
        raw = os.urandom(24 * 1024)
        frames = qr.build_frames(self._pack_bytes(raw), ec="L")
        self.assertGreater(len(frames), 2)
        kept = frames[:1] + frames[2:]        # 故意漏掉第 2 帧
        res = qr.render_images(kept, self.tmp, "缺.bin", ec="L", sheet=False)
        with self.assertRaises(ct.ContainerError) as ctx:
            qr.unpack_images(res["images"], out_dir=self.tmp)
        self.assertIn("缺少第 2 段", str(ctx.exception))

    def test_inspect_reports_coverage(self):
        raw = os.urandom(24 * 1024)
        frames = qr.build_frames(self._pack_bytes(raw), ec="L")
        res = qr.render_images(frames[:2], self.tmp, "体检.bin", ec="L", sheet=False)
        report = qr.inspect_images(res["images"])
        self.assertEqual(report["frames"], 2)
        self.assertEqual(report["docs"][0]["total"], len(frames))
        self.assertEqual(report["docs"][0]["missing"][0], 3)

    def test_unpack_any_mixes_images_and_text(self):
        """二维码图片和 .opk.txt 混着给，也能各自还原。"""
        raw_a = os.urandom(2 * 1024)
        raw_b = os.urandom(2 * 1024)
        frames = qr.build_frames(self._pack_bytes(raw_a, "a.bin"))
        res = qr.render_images(frames, self.tmp, "a.bin", ec="L", sheet=False)

        text_path = os.path.join(self.tmp, "b.opk.txt")
        packed = ct.pack_bytes(raw_b, "b.bin", codec_name="base32", compress="none",
                               preview=False)
        with open(text_path, "w", encoding="utf-8") as fh:
            fh.write(ct.render(packed))

        results, stats = qr.unpack_any([res["images"][0], text_path], out_dir=self.tmp)
        hashes = {m["name"]: m["sha256"] for _p, m in results}
        self.assertEqual(stats["images"], 1)
        self.assertEqual(stats["texts"], 1)
        self.assertEqual(hashes["a.bin"], _sha(raw_a))
        self.assertEqual(hashes["b.bin"], _sha(raw_b))

    def test_no_qr_found_raises(self):
        from PIL import Image

        blank = os.path.join(self.tmp, "blank.png")
        Image.new("RGB", (200, 200), "white").save(blank)
        with self.assertRaises(qr.QRError):
            qr.decode_images([blank])


class WebPEncodeTest(unittest.TestCase):
    """WebP 动图的编码档位：小文件省体积、大文件省时间。"""

    def test_method_scales_with_frame_count(self):
        """几百帧的大文件自动切到最快档，小文件仍用省体积档。"""
        self.assertEqual(qr.webp_method_for(10), qr.WEBP_METHOD)
        self.assertEqual(qr.webp_method_for(qr.WEBP_FAST_FRAMES),
                         qr.WEBP_FAST_METHOD)
        self.assertLess(qr.WEBP_FAST_METHOD, qr.WEBP_METHOD,
                        "「最快档」的 method 必须比默认档更小（libwebp 里越小越快）")

    def test_webp_only_export_is_playable(self):
        """单 WebP 导出：写出来的必须是能解码的动图，帧数对得上。"""
        from PIL import Image

        pack = ct.pack_bytes(os.urandom(9000), "w.bin", codec_name="qr45",
                             compress="none", cipher_name="none", password=None,
                             preview=False, ascii_only=True)
        frames = qr.build_frames(pack, ec="M")
        self.assertGreater(len(frames), 1, "样本压成了一帧，测不出动图")
        tmp = tempfile.mkdtemp(prefix="opk-webp-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        out = qr.render_webp(frames, os.path.join(tmp, "w.webp"), ec="M",
                             fps=3, target_px=480)
        with Image.open(out) as im:
            self.assertEqual(getattr(im, "n_frames", 1), len(frames) + 2,
                             "动图帧数应当 = 数据帧 + 首尾标识帧")
            self.assertEqual(im.format, "WEBP")


if __name__ == "__main__":
    unittest.main()
