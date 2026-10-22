"""二维码容量调优：qr45 编码器、meta 压缩、重复帧容错、单码上限。

这几个用例锁的是「一张码能装多少」和「多张码扫重扫花怎么办」——
两个最容易悄悄退化的地方。
"""

import os
import random
import shutil
import tempfile
import unittest

from officepack import codecs as codec_mod
from officepack import container as ct
from officepack import qr


class Qr45CodecTest(unittest.TestCase):
    """qr45：二维码字母数字模式专用，2 字节 → 3 字符。"""

    def setUp(self):
        self.codec = codec_mod.get_codec("qr45")

    def test_roundtrip_every_length(self):
        """0~300 字节全覆盖：奇偶尾、空输入、单字节都要对。"""
        for n in list(range(0, 40)) + [100, 101, 255, 256, 257, 300]:
            data = bytes((i * 37 + n) & 0xFF for i in range(n))
            enc = self.codec.encode(data)
            self.assertEqual(self.codec.decode(enc, n), data, "长度 %d 往返失败" % n)

    def test_expansion_is_one_and_half(self):
        data = os.urandom(4000)
        self.assertEqual(len(self.codec.encode(data)), 6000)

    def test_alphabet_avoids_space(self):
        """字母表不能含空格：折行后行首行尾的空格会被 strip() 吃掉。"""
        self.assertNotIn(" ", codec_mod._QR45_ALPHABET)
        self.assertEqual(len(codec_mod._QR45_ALPHABET), 44)
        self.assertEqual(len(set(codec_mod._QR45_ALPHABET)), 44)

    def test_alphabet_is_qr_alphanumeric(self):
        """44 个字符必须全在二维码字母数字模式的 45 字符集里，否则掉进字节模式。"""
        alnum = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:")
        self.assertTrue(set(codec_mod._QR45_ALPHABET) <= alnum)

    def test_rejects_truncated_text(self):
        """长度 %3 == 1 一定是被截断了，不能硬解出半个字节。"""
        with self.assertRaises(codec_mod.CodecError):
            self.codec.decode("0123", 3)

    def test_rejects_illegal_char(self):
        with self.assertRaises(codec_mod.CodecError):
            self.codec.decode("AB_C", 2)


class MetaCompressionTest(unittest.TestCase):
    """Meta 行压缩：省下的每个字符，都是一张码能多装的字节。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opk-meta-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _pack(self, data=b"hello world" * 20, **kw):
        return ct.pack_bytes(data, "a.txt", codec_name="qr45",
                             compress="max", preview=False, **kw)

    def test_meta_line_is_compressed(self):
        pack = self._pack(cipher_name="aes-256-gcm", password="pw")
        line = ct._meta_b64(pack.meta)
        self.assertTrue(line.startswith("~"), "加密 meta 应该走压缩分支")
        self.assertEqual(ct._meta_decode(line), pack.meta)

    def test_compressed_is_shorter(self):
        pack = self._pack(cipher_name="aes-256-gcm", password="pw")
        plain = ct._meta_b64(pack.meta).lstrip("~")
        self.assertLess(len(plain), 640, "压缩后的 meta 应明显短于明文 base64")

    def test_plain_meta_still_readable(self):
        """兼容：手写一个用明文 meta 的容器，新版必须照样读得出来。"""
        import base64
        import json

        pack = self._pack()
        plain = base64.b64encode(
            json.dumps(pack.meta, ensure_ascii=False, separators=(",", ":")).encode()
        ).decode("ascii")
        text = ct.render(pack).replace(ct.META_PREFIX + ct._meta_b64(pack.meta),
                                       ct.META_PREFIX + plain, 1)
        out = os.path.join(self.tmp, "restored")
        results = ct.unpack_texts([text], out_dir=out)
        with open(results[0][0], "rb") as fh:
            self.assertEqual(fh.read(), b"hello world" * 20)

    def test_roundtrip_after_compression(self):
        data = os.urandom(700)
        pack = self._pack(data, cipher_name="aes-256-gcm", password="pw")
        out = os.path.join(self.tmp, "rt")
        results = ct.unpack_texts([ct.render(pack)], out_dir=out, password="pw")
        path, meta = results[0]
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), data)
        self.assertEqual(meta["sha256"], __import__("hashlib").sha256(data).hexdigest())


class FrameCapacityTest(unittest.TestCase):
    """单码上限与编码器优劣，必须和「真的能编成二维码」一致。"""

    def _pack(self, n, codec="qr45", compress="max"):
        raw = random.Random(42 + n).randbytes(n)   # 高熵，压缩器榨不出东西
        return ct.pack_bytes(raw, "x.bin", codec_name=codec, compress=compress,
                             cipher_name="none", preview=False)

    def test_qr45_needs_fewest_frames(self):
        pack = {c: self._pack(9000, c) for c in ("qr45", "base32", "base91", "cjk")}
        counts = {c: len(qr.build_frames(p, "L")) for c, p in pack.items()}
        self.assertLessEqual(counts["qr45"], counts["base32"], counts)
        self.assertLessEqual(counts["base32"], counts["base91"], counts)
        self.assertLessEqual(counts["base91"], counts["cjk"], counts)

    def test_single_frame_limit_is_about_2_5k(self):
        """不加密时，单张码（L 级、qr45）应能装下 2.4KB 以上压不动的数据。"""
        self.assertEqual(len(qr.build_frames(self._pack(2400), "L")), 1)
        self.assertGreater(len(qr.build_frames(self._pack(3000), "L")), 1)

    def test_single_frame_bytes_matches(self):
        """估算值必须真的装得下，同时不能过分保守（差 < 10%）。"""
        room = qr.single_frame_bytes("L", "qr45")
        self.assertEqual(len(qr.build_frames(self._pack(room), "L")), 1)
        self.assertLess(2495 / room, 1.10, "估算 %d 比实测 2495 保守太多" % room)

    def test_every_frame_really_encodes(self):
        """帧数少不代表能出码：每帧都得过 qrcode 的真实容量检查。"""
        pack = self._pack(7000)
        for ec in ("L", "M", "Q", "H"):
            for frame in qr.build_frames(pack, ec):
                qr._make_qr_image(frame, ec, 400)

    def test_capacity_report(self):
        pack = self._pack(1200)
        rep = qr.capacity_report(pack, "L")
        self.assertEqual(rep["frames"], 1)
        self.assertTrue(rep["fits_single"])
        self.assertGreater(rep["single_frame_bytes"], 2400)


class DuplicateFrameTest(unittest.TestCase):
    """扫码现实：同一张扫两遍、某张扫花了、顺序乱着给。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opk-dup-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        raw = random.Random(7).randbytes(9000)
        pack = ct.pack_bytes(raw, "big.bin", codec_name="qr45", compress="none",
                             cipher_name="none", preview=False)
        self.raw = raw
        self.frames = qr.build_frames(pack, "L")
        self.assertGreater(len(self.frames), 2, "样本要足够多帧")

    def _unpack(self, frames):
        return ct.unpack_texts(list(frames), out_dir=os.path.join(self.tmp, "o"))

    def _raw(self, frames):
        path = self._unpack(frames)[0][0]
        with open(path, "rb") as fh:
            return fh.read()

    def test_same_frame_twice_is_harmless(self):
        text = "\n".join(self.frames + [self.frames[1]])
        self.assertEqual(self._raw([text]), self.raw)

    def test_unordered_still_works(self):
        text = "\n".join(reversed(self.frames))
        self.assertEqual(self._raw([text]), self.raw)

    def test_one_corrupted_copy_is_ignored(self):
        """同一段一份好一份坏 → 用好的那份，不报错。"""
        broken = self.frames[1][:-5] + "XXXXX"
        text = "\n".join(self.frames + [broken])
        self.assertEqual(self._raw([text]), self.raw)

    def test_conflicting_duplicates_raise(self):
        """同序号两份内容都合法但不同 → 必须报错，绝不瞎猜。"""
        seg = ct.parse_segments(self.frames[1])[0]
        body = seg.body[:-1] + ("A" if seg.body[-1] != "A" else "B")
        head = "%s %s %03d/%03d %s %d" % (
            ct.SEG_PREFIX, seg.doc_id, seg.index, seg.total,
            ct._crc91(body), len(body))
        conflict = "\n".join([head] + ct._wrap(body))
        with self.assertRaises(ct.ContainerError) as ctx:
            self._unpack(["\n".join(self.frames + [conflict])])
        self.assertIn("不同内容", str(ctx.exception))

    def test_missing_reports_which_to_rescan(self):
        keep = [f for i, f in enumerate(self.frames) if i != 2]
        with self.assertRaises(ct.ContainerError) as ctx:
            self._unpack(["\n".join(keep)])
        self.assertIn("缺少第 3 段", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class GifTest(unittest.TestCase):
    """动态 GIF：手机扫屏用；黑白图对 GIF 调色板是无损的。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opk-gif-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.raw = random.Random(11).randbytes(6000)
        pack = ct.pack_bytes(self.raw, "g.bin", codec_name="qr45", compress="none",
                             cipher_name="none", preview=False)
        self.frames = qr.build_frames(pack, "L")
        self.assertGreater(len(self.frames), 1)

    def test_gif_keeps_every_frame(self):
        """动图存下来再读，帧数一帧不能少（逐帧识别）。"""
        path = qr.render_gif(self.frames, os.path.join(self.tmp, "a.gif"), "L",
                             fps=3, target_px=600)
        self.assertEqual(sorted(qr.decode_image(path)), sorted(self.frames))

    def test_gif_roundtrip_restores_file(self):
        path = qr.render_gif(self.frames, os.path.join(self.tmp, "b.gif"), "L",
                             fps=2, target_px=600)
        results = qr.unpack_images([path], out_dir=os.path.join(self.tmp, "out"))
        with open(results[0][0], "rb") as fh:
            self.assertEqual(fh.read(), self.raw)

    def test_gif_is_pixel_lossless(self):
        """GIF 用 2 色调色板，黑白二维码应逐像素无损（不是"看起来差不多"）。

        首尾各有一张 ``#OPKmark`` 标识帧，比对时剔掉它们 —— 其余数据帧必须
        与单独渲染的 PNG 逐像素一致。
        """
        from PIL import Image, ImageSequence

        path = qr.render_gif(self.frames, os.path.join(self.tmp, "c.gif"), "L",
                             fps=3, target_px=600)
        with Image.open(path) as im:
            got = [f.convert("L").tobytes() for f in ImageSequence.Iterator(im)]
        self.assertEqual(len(got), len(self.frames) + 2, "应有首尾两张标识帧")
        got_data = got[1:-1]
        want = qr._render_uniform(list(self.frames), "L", 600)
        want = [im.convert("L").tobytes() for im in want]
        self.assertEqual(got_data, want)

    def test_gif_frames_share_one_canvas(self):
        """所有帧必须同尺寸：尺寸不一致时 GIF 会把大的那帧裁掉，播出残缺码。"""
        from PIL import Image, ImageSequence

        path = qr.render_gif(self.frames, os.path.join(self.tmp, "u.gif"), "L",
                             fps=3, target_px=620)
        with Image.open(path) as im:
            canvas = im.size
            sizes = [f.size for f in ImageSequence.Iterator(im)]
        self.assertTrue(sizes)
        self.assertTrue(all(s == canvas for s in sizes),
                        "帧尺寸与画布不一致：%r vs %r" % (sizes[:3], canvas))
        self.assertEqual(len(set(sizes)), 1, "帧尺寸参差不齐：%r" % (set(sizes),))

    def test_single_frame_rejects_gif(self):
        pack = ct.pack_bytes(b"tiny", "t.txt", codec_name="qr45", compress="none",
                             cipher_name="none", preview=False)
        frames = qr.build_frames(pack, "L")
        self.assertEqual(len(frames), 1)
        with self.assertRaises(qr.QRError):
            qr.render_gif(frames, os.path.join(self.tmp, "one.gif"), "L")

    def test_sheet_works_without_freetype(self):
        """瘦身版删掉了 _imagingft（FreeType），总览图不能因此崩。

        真实事故：v1.2 删了 _imagingft.pyd，`ImageFont.truetype` 抛 ImportError，
        而 except 只写了 (OSError, IOError) —— 兜不住，多帧出码直接失败。
        """
        from unittest import mock

        import PIL.ImageFont
        from PIL import Image, ImageFont

        def boom(*_a, **_k):
            raise ImportError("cannot import name '_imagingft' from 'PIL'")

        with mock.patch.object(PIL.ImageFont, "truetype", boom):
            res = qr.render_images(self.frames, os.path.join(self.tmp, "nofont"),
                                   "nf", ec="L", sheet=True)
        self.assertTrue(res["sheets"])
        self.assertEqual(sorted(qr.decode_images(res["images"])), sorted(self.frames))

    def test_fps_is_clamped(self):
        """离谱的帧率不该崩，夹到 0.5~10。"""
        path = qr.render_gif(self.frames, os.path.join(self.tmp, "d.gif"), "L",
                             fps=999, target_px=400)
        self.assertTrue(os.path.isfile(path))
