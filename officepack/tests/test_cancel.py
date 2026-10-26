"""「中止」按钮的取消链路测试。

用户要的是「生成和还原过程都能中途停」。这条链路横跨 qr 的五个阶段
（排版 → 渲染 → 编码 → 拼容器 → 写盘），任何一环漏了检查点，用户点了
中止都得干等 —— 所以每个阶段都要有一条测试盯着。

判定标准只有两条：
  1. 命中取消就抛 ``qr.Cancelled``（不是静默返回半成品，也不是被当成坏图吞掉）；
  2. 中止后**不留下半个文件**（写盘是最后一步，前面停都不该有产物）。
"""

import os
import shutil
import tempfile
import unittest

from officepack import qr
from officepack.gui import _fmt_spent


def _always_stop():
    return True


def _never_stop():
    return False


class CancelRenderTest(unittest.TestCase):
    """出码侧：四个渲染入口都要能立刻停。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opk-cancel-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # 4 帧就够：渲染入口的检查点都在帧边界，与帧数无关
        self.texts = ["OPKCANCEL-%02d" % i for i in range(4)]

    def test_render_png_stops_and_writes_nothing(self):
        with self.assertRaises(qr.Cancelled):
            qr.render_images(self.texts, self.tmp, "doc",
                             should_stop=_always_stop)
        self.assertEqual([n for n in os.listdir(self.tmp) if n.endswith(".png")],
                         [], "中止发生在写盘之前，不该留下任何 png")

    def test_render_gif_stops_and_writes_nothing(self):
        path = os.path.join(self.tmp, "a.gif")
        with self.assertRaises(qr.Cancelled):
            qr.render_gif(self.texts, path, should_stop=_always_stop)
        self.assertFalse(os.path.exists(path), "中止后不该留下半个 gif")

    def test_render_webp_stops_and_writes_nothing(self):
        path = os.path.join(self.tmp, "a.webp")
        with self.assertRaises(qr.Cancelled):
            qr.render_webp(self.texts, path, should_stop=_always_stop)
        self.assertFalse(os.path.exists(path), "中止后不该留下半个 webp")

    def test_anim_images_stops_midway(self):
        """流式出帧：先出几帧再喊停，剩下的不再来。

        阈值要跨过「预排」阶段：zxing 预排会**先**逐帧调一次 should_stop
        （6 帧就是 6 次），阈值定低了会在预排里就停掉，一帧都出不来 ——
        那样测的就不是「出帧中途能停」了。
        """
        if qr._zx_writer() is None:      # 没有 zxing 就没有预排阶段，阈值语义不同
            self.skipTest("没有 zxing，出帧路径与断言前提不同")
        texts = ["OPKCANCEL-%02d" % i for i in range(6)]
        got = []
        calls = {"n": 0}

        def stop_late():
            calls["n"] += 1
            return calls["n"] > 8        # 6 次预排 + 出 2 帧之后才喊停

        with self.assertRaises(qr.Cancelled):
            for im in qr.anim_images(texts, qr.DEFAULT_EC, 240,
                                     markers=False, should_stop=stop_late):
                got.append(im)
        self.assertGreaterEqual(len(got), 1, "中止前应该已经出了几帧")
        self.assertLess(len(got), len(texts), "中止后不该把剩下的帧也出完")

    def test_plan_stops_during_presort(self):
        """zxing 预排是整条链路里最长的一段静默期（3825 帧约一分钟）。"""
        many = ["OPKPLAN-%04d" % i for i in range(40)]
        with self.assertRaises(qr.Cancelled):
            qr.render_images(many, self.tmp, "doc", should_stop=_always_stop)


class CancelDecodeTest(unittest.TestCase):
    """还原侧：识别阶段的取消不能被「坏图」兜底逻辑吞掉。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opk-cancel-dec-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        texts = ["OPKDEC-%02d" % i for i in range(2)]
        qr.render_images(texts, self.tmp, "doc", sheet=False)
        self.imgs = sorted(os.path.join(self.tmp, n) for n in os.listdir(self.tmp)
                           if n.endswith(".png"))
        self.assertTrue(self.imgs)

    def test_decode_images_raises_cancelled_not_qrerror(self):
        # 关键回归点：decode_images._one 里有个 except Exception 兜底，
        # Cancelled 若不单独放行，用户点了中止会看到「没有识别到二维码」
        with self.assertRaises(qr.Cancelled):
            qr.decode_images(self.imgs, should_stop=_always_stop)

    def test_unpack_any_raises_cancelled(self):
        with self.assertRaises(qr.Cancelled):
            qr.unpack_any(self.imgs, self.tmp, should_stop=_always_stop)

    def test_decode_still_works_without_cancel(self):
        texts = qr.decode_images(self.imgs, should_stop=_never_stop)
        self.assertTrue(texts)


class SpentFormatTest(unittest.TestCase):
    """「本次总用时」的文案：秒 / 分 / 小时三档。"""

    def test_seconds(self):
        self.assertEqual(_fmt_spent(8.44), "8.4 秒")

    def test_minutes(self):
        self.assertEqual(_fmt_spent(83), "1 分 23 秒")

    def test_hours(self):
        self.assertTrue(_fmt_spent(3900).startswith("1 小时"))

    def test_negative_and_none(self):
        self.assertEqual(_fmt_spent(0), "0.0 秒")
        self.assertEqual(_fmt_spent(None), "0.0 秒")


if __name__ == "__main__":
    unittest.main()
