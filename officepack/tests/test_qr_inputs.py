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


if __name__ == "__main__":
    unittest.main()
