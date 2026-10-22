"""还原入口与识别鲁棒性测试。

覆盖三类真实踩过的坑：
  1. 多选图片还原时被当文本读（PNG 直接 open(..., "r") 炸 UnicodeDecodeError）；
  2. 多张图顺序打乱 / 重复 / 缺帧；
  3. 截图上叠了水印，原像素识别不出来。
以及「只输出总览」这个新开关。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from officepack import clipboard as clip_mod
from officepack import container as ct
from officepack import qr


def _noise(size: int) -> bytes:
    """哈希链拼出来的不可压缩字节：样本必须拆成多帧，才测得出多图行为。"""
    import hashlib

    out = bytearray()
    seed = b"officepack-qr-inputs"
    while len(out) < size:
        seed = hashlib.sha256(seed).digest()
        out += seed
    return bytes(out[:size])


def _make_frames(raw_size: int = 12000, codec: str = "qr45", ec: str = "M"):
    raw = _noise(raw_size)
    pack = ct.pack_bytes(raw, "样本.bin", codec_name=codec, cipher_name="none",
                         password=None, compress="max", preview=False,
                         ascii_only=True)
    return qr.build_frames(pack, ec=ec), raw


class MultiImageInputTest(unittest.TestCase):
    """多张图片一起还原：不许只认最后一张，顺序也不许影响结果。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = cls.tmp.name
        cls.frames, cls.raw = _make_frames()
        res = qr.render_images(cls.frames, cls.dir, "multi", ec="M",
                               sheet=False, target_px=640)
        cls.pngs = res["images"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_frames_are_multiple(self):
        self.assertGreater(len(self.frames), 2, "样本太容易被压成 1 帧，测不出多图行为")

    def test_container_unpack_sources_accepts_images(self):
        """container 层直接吃 PNG —— 以前这里会抛 UnicodeDecodeError。"""
        out = os.path.join(self.dir, "out_sources")
        results = ct.unpack_sources(self.pngs, out)
        self.assertEqual(len(results), 1)
        with open(results[0][0], "rb") as fh:
            self.assertEqual(fh.read(), self.raw)

    def test_shuffled_images_restore(self):
        out = os.path.join(self.dir, "out_shuffle")
        results = qr.unpack_images(list(reversed(self.pngs)), out)
        self.assertEqual(len(results), 1)
        with open(results[0][0], "rb") as fh:
            self.assertEqual(fh.read(), self.raw)

    def test_duplicate_images_are_tolerated(self):
        out = os.path.join(self.dir, "out_dup")
        doubled = self.pngs + self.pngs[:2]
        results = qr.unpack_images(doubled, out)
        self.assertEqual(len(results), 1)

    def test_decode_images_collects_every_frame(self):
        texts = qr.decode_images(self.pngs)
        self.assertEqual(len(texts), len(self.frames))

    def test_mixed_images_and_text(self):
        """图片 + 文本容器混着还原：两条路都要走得通。"""
        out = os.path.join(self.dir, "out_mixed")
        pack = ct.pack_bytes(b"hello officepack", "a.txt", codec_name="base32",
                             cipher_name="none", password=None, compress="none",
                             preview=False, ascii_only=True)
        text_path = os.path.join(self.dir, "a.opk.txt")
        with open(text_path, "w", encoding="utf-8") as fh:
            fh.write(ct.render(pack))
        results = ct.unpack_sources([text_path] + self.pngs, out)
        names = {os.path.basename(p) for p, _ in results}
        self.assertEqual(len(results), 2, names)


class WatermarkTest(unittest.TestCase):
    """水印压在码上时，多策略重试要能把帧捞回来。"""

    def test_heavy_watermark_still_decoded(self):
        from PIL import Image, ImageDraw, ImageFont

        frames, _raw = _make_frames(9000)
        with tempfile.TemporaryDirectory() as tmp:
            res = qr.render_images(frames, tmp, "wm", ec="M", sheet=True,
                                   target_px=640)
            sheet = res["sheet"]
            with Image.open(sheet) as im:
                base = im.convert("RGB")

        # 半透明灰字铺满整张图：不处理的话大量模块被判成非黑非白
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        try:
            font = ImageFont.truetype("arial.ttf", 26)
        except Exception:                       # pragma: no cover
            font = ImageFont.load_default()
        for y in range(6, base.height, 70):
            for x in range(0, base.width, 240):
                draw.text((x, y), "内部资料 禁止外传", font=font, fill=(90, 90, 90, 215))
        marked = Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")

        got = qr.decode_image(marked)
        self.assertEqual(len(got), len(frames),
                         "水印图上漏帧：%d/%d" % (len(got), len(frames)))

    def test_otsu_threshold_in_range(self):
        from PIL import Image

        img = Image.new("L", (8, 8), 255)
        for x in range(4):
            for y in range(4):
                img.putpixel((x, y), 0)
        out = qr._otsu(img)
        self.assertEqual(out.mode, "L")
        self.assertEqual(out.size, img.size)


class RenderOnlySheetTest(unittest.TestCase):
    """「只出总览」开关：少写 N 个文件，但数据一点不少。"""

    @classmethod
    def setUpClass(cls):
        cls.frames, cls.raw = _make_frames(12000)

    def test_single_false_writes_only_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = qr.render_images(self.frames, tmp, "only", ec="M", sheet=True,
                                   target_px=640, single=False)
            self.assertEqual(res["images"], [])
            self.assertEqual(len(res["sheets"]), 1)
            texts = qr.decode_image(res["sheets"][0])
            self.assertEqual(len(texts), len(self.frames))

    def test_single_frame_falls_back_to_png(self):
        one = [self.frames[0]]
        with tempfile.TemporaryDirectory() as tmp:
            res = qr.render_images(one, tmp, "one", ec="M", sheet=True,
                                   target_px=640, single=False)
            self.assertEqual(len(res["images"]), 1, "只有一帧时必须老老实实写单帧图")
            self.assertEqual(res["sheets"], [])


class ClipboardModuleTest(unittest.TestCase):
    """剪贴板模块在完全取不到东西时只能返回空，绝不能抛异常。"""

    def test_snapshot_shape(self):
        snap = clip_mod.snapshot()
        for key in ("images", "files", "text", "formats", "sequence"):
            self.assertIn(key, snap)
        self.assertIsInstance(snap["images"], list)
        self.assertIsInstance(snap["files"], list)

    def test_ditto_path_is_probed_not_hardcoded(self):
        path = clip_mod.ditto_path()
        if path:
            self.assertTrue(os.path.isfile(path), "探测出的 Ditto 路径必须真实存在")
            self.assertEqual(os.path.basename(path).lower(), "ditto.exe")

    def test_candidate_dirs_are_derived(self):
        dirs = clip_mod._candidate_dirs()
        self.assertTrue(dirs)
        self.assertTrue(all(os.path.isabs(d) for d in dirs[:5]))


class ScanRegionTest(unittest.TestCase):
    """框选连拍：一张截图只有动图里的一帧，必须连着抓才能收全。

    这是 v1.4.1 的回归点：框选以前只抓一张就解码，碰上正在播放的动图必然
    只得到 1/N 帧，表现为「框选了也识别不出来」。
    """

    @classmethod
    def setUpClass(cls):
        from PIL import Image

        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = cls.tmp.name
        cls.frames, cls.raw = _make_frames(14000)
        res = qr.render_images(cls.frames, cls.dir, "scan", ec="M",
                               sheet=False, target_px=720)
        cls.images = []
        for path in res["images"]:
            with Image.open(path) as im:
                cls.images.append(im.convert("RGB"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def _cycler(images, repeat=2):
        """模拟「屏幕上的动图正在循环播放」：每次抓图给下一帧。"""
        seq = list(images) * repeat
        state = {"i": 0}

        def grab():
            img = seq[min(state["i"], len(seq) - 1)]
            state["i"] += 1
            return img

        return grab

    def test_animation_frames_are_all_collected(self):
        """动图：连拍应收全所有帧并判定收齐，还原后逐字节一致。"""
        self.assertGreater(len(self.frames), 2, "样本太少，测不出连拍价值")
        texts, stats = qr.scan_region(self._cycler(self.images),
                                      idle=0.5, interval=0.0, cap=30)
        self.assertTrue(stats["complete"], "没判成收齐：%r" % (stats,))
        self.assertGreaterEqual(len(texts), len(self.frames))
        out = os.path.join(self.dir, "out_scan")
        results = ct.unpack_texts(texts, out)
        with open(results[0][0], "rb") as fh:
            self.assertEqual(fh.read(), self.raw)

    def test_static_code_returns_immediately(self):
        """完整单码：第一枪就齐，不该傻等 idle 秒（否则每次框选都卡 2 秒）。"""
        raw = b"officepack one frame sample" * 2
        pack = ct.pack_bytes(raw, "one.txt", codec_name="qr45", cipher_name="none",
                             password=None, compress="max", preview=False,
                             ascii_only=True)
        frames = qr.build_frames(pack, ec="M")
        self.assertEqual(len(frames), 1, "样本压不成单帧，测试前提不成立")
        res = qr.render_images(frames, self.dir, "one", ec="M",
                               sheet=False, target_px=480)
        from PIL import Image

        with Image.open(res["images"][0]) as im:
            still = im.convert("RGB")
        texts, stats = qr.scan_region(lambda: still, idle=5.0, interval=0.0)
        self.assertTrue(stats["complete"])
        self.assertEqual(len(texts), 1)
        self.assertLessEqual(stats["shots"], 2, "静态码不该连拍")
        self.assertLess(stats["elapsed"], 2.0, "静态码不该等待")

    def test_gif_has_start_and_end_markers(self):
        """动图首尾各插一张标识帧：识别端靠它判断一轮从哪开始、到哪结束。"""
        gif = qr.render_gif(self.frames, os.path.join(self.dir, "marked.gif"),
                            ec="M", fps=3, target_px=480)
        plain = qr.decode_image(gif)
        self.assertFalse([t for t in plain if qr.is_marker(t)],
                         "还原路径不该看到标识帧")
        self.assertEqual(len(plain), len(self.frames))
        marked = qr.decode_image(gif, keep_markers=True)
        kinds = [qr.parse_marker(t) for t in marked if qr.is_marker(t)]
        self.assertEqual([k[0] for k in kinds], ["S", "E"], "首尾标识帧缺失")
        self.assertEqual(kinds[0][2], len(self.frames), "首帧里的总帧数不对")

    def test_scan_with_markers_finishes_in_one_cycle(self):
        """带标识的动图：见首帧开始、到尾帧收齐，一轮就够，不用等超时。"""
        from PIL import Image

        gif = qr.render_gif(self.frames, os.path.join(self.dir, "cycle.gif"),
                            ec="M", fps=3, target_px=600)
        seq = []
        with Image.open(gif) as im:
            for i in range(im.n_frames):
                im.seek(i)
                seq.append(im.convert("RGB"))
        self.assertEqual(len(seq), len(self.frames) + 2, "首尾标识帧没写进 GIF")
        texts, stats = qr.scan_region(self._cycler(seq, repeat=3),
                                      idle=5.0, interval=0.0, cap=30)
        self.assertTrue(stats["complete"], "一轮没收齐：%r" % (stats,))
        self.assertEqual(len(texts), len(self.frames))
        self.assertFalse([t for t in texts if qr.is_marker(t)], "标识帧混进了数据")
        self.assertLessEqual(stats["cycles"], 1, "本该一轮收齐")

    def test_blank_region_gives_up_quickly(self):
        """框错了地方：不能磨很久，也不能抛异常。"""
        from PIL import Image

        blank = Image.new("RGB", (420, 420), "white")
        texts, stats = qr.scan_region(lambda: blank, idle=0.3, interval=0.0)
        self.assertEqual(texts, [])
        self.assertFalse(stats["complete"])
        self.assertLess(stats["elapsed"], 8.0, "空白区域磨太久")

    def test_fast_miss_is_retried_on_the_same_frame(self):
        """裸读失手不能等于永久漏帧：前几枪必须换个法子再试一次。

        真实场景：屏幕 125% 缩放下，码是被双线性拉伸过的，``fast`` 只跑原图
        经常差一口气。而画面不变时 ``_frame_key`` 会跳过解码 —— 第一枪失手，
        后面每一枪都不会再看它，于是「明明在屏幕上却识别不了」。
        """
        from PIL import Image

        blank = Image.new("RGB", (420, 420), "white")
        real = qr._read_one
        calls = {"n": 0}

        def fake_read(img, deep=True, fast=False, budget=None):
            calls["n"] += 1
            return ["#OPK1 000000 001/001 AB\nDATA"]

        qr._read_one = fake_read
        try:
            texts, stats = qr.scan_region(lambda: blank, idle=1.0,
                                          interval=0.0, cap=5.0)
        finally:
            qr._read_one = real
        self.assertTrue(texts, "快扫失手后没有重试，等于永久漏帧")
        self.assertLessEqual(calls["n"], 3, "重试次数失控，动图会被拖慢")
        self.assertLessEqual(stats["shots"], 4, "不是在前几枪就补救的")


if __name__ == "__main__":
    unittest.main()
