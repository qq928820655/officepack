"""抓屏模块的单测：只测「不会把程序搞崩」和「拿不到就退回」这两件事。

DXGI 桌面复制在虚拟机/核显上可能直接访问违例（进程级崩溃，Python 拦不住），
所以这里的重点是：**任何失败都必须返回 None，绝不能抛异常**。
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from officepack import screengrab as sg

IS_WIN = sys.platform.startswith("win")


class GdiGrabberTest(unittest.TestCase):
    def test_returns_callable_and_right_size(self):
        if not IS_WIN:
            self.skipTest("仅 Windows")
        fn = sg.gdi_grabber((0, 0, 120, 90))
        self.assertIsNotNone(fn, "GDI BitBlt 应该可用")
        im = fn()
        self.assertEqual(im.size, (120, 90))
        self.assertEqual(im.mode, "RGB")

    def test_two_grabs_do_not_share_buffer(self):
        """第二枪不能覆盖第一枪的结果（复用同一个 DIB 缓冲区时的经典坑）。"""
        if not IS_WIN:
            self.skipTest("仅 Windows")
        fn = sg.gdi_grabber((0, 0, 200, 200))
        a = fn()
        b = fn()
        self.assertIsNot(a, b)
        self.assertIsNot(a.getpixel((0, 0)), None)

    def test_bad_bbox_returns_none(self):
        self.assertIsNone(sg.gdi_grabber((10, 10, 10, 50)))
        self.assertIsNone(sg.gdi_grabber((50, 50, 10, 10)))


class DxgiGrabberTest(unittest.TestCase):
    def test_never_raises(self):
        """不可用就返回 None；可用则必须能连续抓两帧。"""
        if not IS_WIN:
            self.skipTest("仅 Windows")
        try:
            fn = sg.dxgi_grabber((0, 0, 120, 90))
        except Exception as exc:  # noqa: BLE001
            self.fail("dxgi_grabber 不该抛异常：%r" % exc)
        if fn is None:
            return                                    # 这台机器没有桌面复制，正常
        im = fn()
        self.assertEqual(im.size, (120, 90))
        self.assertEqual(fn().size, (120, 90))

    def test_bad_bbox_returns_none(self):
        self.assertIsNone(sg.dxgi_grabber((0, 0, 0, 0)))


class PickGrabberTest(unittest.TestCase):
    def test_falls_back_to_gdi(self):
        if not IS_WIN:
            self.skipTest("仅 Windows")
        g = sg.pick_grabber((0, 0, 160, 120), allow_gpu=False)
        self.assertIsNotNone(g)
        self.assertIn("GDI", g.method)
        self.assertGreater(g.ms, 0)
        self.assertEqual(g().size, (160, 120))
        self.assertIn("ms/枪", g.info)

    def test_none_for_empty_region(self):
        self.assertIsNone(sg.pick_grabber((5, 5, 5, 5), allow_gpu=False))


class PrefetcherTest(unittest.TestCase):
    def test_delivers_and_closes(self):
        from PIL import Image

        n = {"i": 0}

        def grab():
            n["i"] += 1
            return Image.new("RGB", (8, 8), (n["i"] % 256, 0, 0))

        p = sg.Prefetcher(grab)
        try:
            got = [p() for _ in range(4)]
            self.assertTrue(all(im is not None for im in got))
            self.assertEqual([im.size for im in got], [(8, 8)] * 4)
            self.assertGreaterEqual(n["i"], 4)
        finally:
            p.close()

    def test_survives_grab_exception(self):
        """抓图抛异常（锁屏/切桌面）不能把后台线程弄死，也不能卡住主循环。"""
        from PIL import Image

        def grab():
            raise OSError("BitBlt 失败")

        p = sg.Prefetcher(grab)
        try:
            t = time.perf_counter()
            im = p()
            self.assertLess(time.perf_counter() - t, 2.0, "不该卡住")
            self.assertIsNone(im)
        finally:
            p.close()

    def test_close_is_idempotent(self):
        from PIL import Image

        p = sg.Prefetcher(lambda: Image.new("RGB", (4, 4)))
        p.close()
        p.close()                                     # 第二次不该抛


class CacheTest(unittest.TestCase):
    def test_roundtrip(self):
        old = os.environ.get("LOCALAPPDATA")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LOCALAPPDATA"] = tmp
            try:
                self.assertEqual(sg._read_cache(), {})
                sg._write_cache({"gpu": "X", "dxgi": True, "ms": "3.1", "ts": 1})
                self.assertEqual(sg._read_cache()["gpu"], "X")
                # 坏文件不该炸
                with open(sg._cache_path(), "w", encoding="utf-8") as f:
                    f.write("{ not json")
                self.assertEqual(sg._read_cache(), {})
            finally:
                if old is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = old

    def test_probe_argv_shape(self):
        if getattr(sys, "frozen", False):
            self.skipTest("打包环境不测命令行拼接")
        cmd = sg.probe_argv((1, 2, 3, 4), "out.json")
        self.assertIsNotNone(cmd)
        self.assertIn("--probe-grab", cmd)
        self.assertIn("1,2,3,4", cmd)
        self.assertIn("officepack.screengrab", cmd)

    def test_probe_in_child_never_raises(self):
        """子进程自检失败（崩溃/超时）只能返回 None。"""
        res = sg.probe_in_child((0, 0, 64, 64), timeout=15)
        self.assertTrue(res is None or isinstance(res, dict))


class GpuNameTest(unittest.TestCase):
    def test_returns_string(self):
        name = sg.gpu_name()
        self.assertIsInstance(name, str)


if __name__ == "__main__":
    unittest.main()
