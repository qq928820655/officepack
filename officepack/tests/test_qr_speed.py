"""性能与并行的回归护栏。

这里不放「必须多快」的硬指标（机器差异太大），只钉住**量级**：
老实现出了名的慢 —— 单帧渲染 720ms（qrcode 纯 Python 生成 QR 40 版矩阵），
131 帧的动图出码 92 秒、识别 161 秒，用户点了按钮只看到界面不动，以为程序死了。

真正的断言是「别退回去」：阈值给到新实测值的 5~8 倍，正常机器不可能误报，
但只要有人把渲染后端换回纯 Python、或者把逐帧深挖改回来，立刻就会炸。
"""

from __future__ import annotations

import os
import random
import shutil
import tempfile
import time
import unittest

from officepack import container as ct
from officepack import qr


def _pack(size: int, name: str = "s.bin") -> ct.PackResult:
    raw = random.Random(5).randbytes(size)
    return ct.pack_bytes(raw, name, codec_name="qr45", compress="none",
                         cipher_name="none", preview=False)


class RenderBackendTest(unittest.TestCase):
    """写码后端换成 zxing-cpp 之后，产出的码必须和原来一样能读、能还原。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opk-speed-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pack = _pack(6000)
        self.frames = qr.build_frames(self.pack, "L")

    def test_rendered_frames_round_trip(self):
        """默认后端出的码，逐张识别要能把原文一字不差读回来。"""
        images = qr._render_uniform(self.frames, "L", 600)
        self.assertEqual(len(images), len(self.frames))
        got = []
        for im in images:
            got.extend(qr.decode_image(im, deep=False))
        self.assertEqual(sorted(got), sorted(self.frames))

    def test_fallback_backend_renders_the_same_content(self):
        """zxing 不可用时退回 qrcode，内容语义必须完全一致（不是"差不多"）。"""
        zx_images = qr._render_uniform(self.frames, "L", 600)
        saved = qr._ZX_WRITER
        qr._ZX_WRITER = None            # 强制走纯 Python 退路
        try:
            py_images = qr._render_uniform(self.frames, "L", 600)
        finally:
            qr._ZX_WRITER = saved
        self.assertEqual([im.size for im in zx_images], [im.size for im in py_images])
        for im in py_images:
            self.assertTrue(qr.decode_image(im, deep=False))

    def test_every_frame_shares_one_canvas(self):
        """统一画布是 GIF 不裁边的前提，两个后端都得做到。"""
        images = qr._render_uniform(self.frames, "L", 700)
        self.assertEqual(len({im.size for im in images}), 1, "帧尺寸参差不齐")


class ProgressTest(unittest.TestCase):
    """进度回调：界面靠它刷那条「渲染 45% (58/129)」的进度条。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opk-prog-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.frames = qr.build_frames(_pack(4000), "L")

    def test_render_images_reports_every_frame(self):
        seen = []
        qr.render_images(self.frames, self.tmp, "p", ec="L", sheet=False,
                         progress=lambda d, t, s: seen.append((d, t, s)))
        self.assertTrue(seen, "一条进度都没报")
        self.assertEqual(seen[-1][0], len(self.frames))
        self.assertEqual(seen[-1][1], len(self.frames))
        self.assertTrue(all(t == len(self.frames) for _d, t, _s in seen))
        self.assertEqual([d for d, _t, _s in seen], list(range(1, len(self.frames) + 1)))

    def test_render_gif_reports_marker_frames_too(self):
        seen = []
        path = os.path.join(self.tmp, "g.gif")
        qr.render_gif(self.frames, path, "L", fps=3, target_px=500,
                      progress=lambda d, t, s: seen.append((d, t, s)))
        # 首尾标识帧也参与渲染，所以总数是数据帧 + 2
        self.assertEqual(seen[-1][1], len(self.frames) + 2)
        self.assertEqual(seen[-1][0], seen[-1][1])

    def test_broken_callback_does_not_break_rendering(self):
        """回调是界面侧的代码，它抛异常不能把出码搞崩。"""
        def boom(_d, _t, _s):
            raise RuntimeError("界面炸了")
        path = os.path.join(self.tmp, "b.gif")
        qr.render_gif(self.frames, path, "L", fps=3, target_px=400, progress=boom)
        self.assertTrue(os.path.isfile(path))


class SpeedTest(unittest.TestCase):
    """量级护栏：慢了 5 倍以内算正常波动，慢 20 倍一定是退化。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opk-fast-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_rendering_many_frames_is_not_minutes(self):
        """20 帧渲染：新实现 ~0.4 秒，老实现 ~14 秒。卡 8 秒。"""
        frames = qr.build_frames(_pack(60000), "L")
        self.assertGreater(len(frames), 15)
        start = time.monotonic()
        qr.render_images(frames, self.tmp, "many", ec="L", sheet=False,
                         target_px=600)
        self.assertLess(time.monotonic() - start, 8.0,
                        "渲染退化成纯 Python 了？老实现 20 帧要 14 秒")

    def test_many_frame_gif_decodes_in_seconds(self):
        """20 帧以上的动图识别：新实现 ~1 秒，老实现每帧 1.1 秒。卡 15 秒。"""
        frames = qr.build_frames(_pack(20000), "L")
        path = os.path.join(self.tmp, "many.gif")
        qr.render_gif(frames, path, "L", fps=3, target_px=600)
        start = time.monotonic()
        got = qr.decode_image(path)
        elapsed = time.monotonic() - start
        self.assertEqual(sorted(got), sorted(frames))
        self.assertLess(elapsed, 15.0,
                        "又在逐帧跑 6 秒重试预算了？%d 帧花了 %.1fs"
                        % (len(frames), elapsed))


class GifDisposalTest(unittest.TestCase):
    """GIF 的调色板 0 号必须是白。

    我们用 ``disposal=2``（每帧先清成背景色再叠差异区域），GIF 的背景色取的是
    调色板 0 号。0 号一旦是黑，播放器就会把整屏刷黑再叠差异 —— 画面成了满屏
    噪点，肉眼和扫码器都认不出。老实现靠 ADAPTIVE 量化「碰巧」让 0 号是白，
    现在的固定调色板必须显式保证。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opk-pal-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_palette_zero_is_white(self):
        from PIL import Image

        frames = qr.build_frames(_pack(3000), "L")
        path = os.path.join(self.tmp, "c.gif")
        qr.render_gif(frames, path, "L", fps=3, target_px=400)
        with Image.open(path) as im:
            palette = im.getpalette()
            self.assertEqual(palette[:3], [255, 255, 255],
                             "调色板 0 号不是白：disposal=2 会把背景刷黑")
            self.assertEqual(im.info.get("background", 0), 0)

    def test_frames_are_pixel_identical_to_png(self):
        """固定调色板不能有损：GIF 每帧要和单独渲染的 PNG 逐像素一致。"""
        from PIL import Image, ImageSequence

        frames = qr.build_frames(_pack(3000), "L")
        path = os.path.join(self.tmp, "d.gif")
        qr.render_gif(frames, path, "L", fps=3, target_px=560)
        want = [im.convert("L").tobytes()
                for im in qr._render_uniform(list(frames), "L", 560)]
        with Image.open(path) as im:
            got = [f.convert("L").tobytes() for f in ImageSequence.Iterator(im)]
        self.assertEqual(got[1:-1], want)


class QuietZoneTest(unittest.TestCase):
    """静区被切掉是最常见的「怎么扫都扫不出」，识别端得能自己补回来。"""

    def setUp(self):
        self.frames = qr.build_frames(_pack(2000), "L")

    def test_cropped_quiet_zone_is_recovered(self):
        im = qr._render_uniform(self.frames[:1], "L", 600)[0]
        cropped = im.crop((0, 0, im.width - 10, im.height - 10))   # 切掉右/下静区
        self.assertTrue(qr._read_one(cropped, deep=False),
                        "补白变体没接住被切掉静区的图")


class GarbageTextTest(unittest.TestCase):
    """误识别碎片不能把整批还原带崩。

    真实事故：129 帧的动图里 zxing 多"读"出一个 ``B40B``，它被当成一块独立容器
    去解析，抛「缺少起始标记」，于是**所有帧都扫到了却还原失败**。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opk-junk-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.raw = random.Random(3).randbytes(3000)
        self.pack = ct.pack_bytes(self.raw, "j.bin", codec_name="qr45",
                                  compress="none", cipher_name="none", preview=False)
        self.frames = qr.build_frames(self.pack, "L")

    def test_junk_is_dropped_when_frames_are_present(self):
        kept = qr._drop_garbage(list(self.frames) + ["B40B"])
        self.assertEqual(sorted(kept), sorted(self.frames))

    def test_foreign_codes_are_kept_as_is(self):
        """读完不是本工具的二维码时不能自作主张删东西。"""
        foreign = ["https://example.com", "WIFI:S:home;T:WPA;P:12345678;;"]
        self.assertEqual(qr._drop_garbage(foreign), foreign)

    def test_unpack_tolerates_one_unparsable_block(self):
        """解不动的那块要忽略，正常分段照常还原。"""
        results = ct.unpack_texts(list(self.frames) + ["B40B"],
                                  out_dir=os.path.join(self.tmp, "o"))
        self.assertEqual(len(results), 1)
        with open(results[0][0], "rb") as fh:
            self.assertEqual(fh.read(), self.raw)

    def test_unpack_still_raises_when_nothing_parses(self):
        """全都解不动时必须报错，不能静悄悄返回空。"""
        with self.assertRaises(ct.ContainerError):
            ct.unpack_texts(["B40B"], out_dir=os.path.join(self.tmp, "p"))


if __name__ == "__main__":
    unittest.main()
