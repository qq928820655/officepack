"""「进度看不见」这一组问题的回归测试。

背景（用户实测 16.5MB 的 rar）：
  1. 压缩阶段 max 档全量试 4 组 lzma 要 34 秒，全程不报进度 → 界面「压缩加密 0%」
  2. zxing 预排 3825 帧要一分钟，同样不报进度
  3. WebP 合成用 PIL 的 save_all 一次性写完，进度条停在 100% 还要再等 40 秒

这里锁住四件事：压缩会跳过压不动的数据、流式压缩与一次性结果一致、
WebP 动画容器拼得对、进度条不再把内部刻度或 100% 摆给用户看。
"""

from __future__ import annotations

import io
import lzma
import os
import time
import unittest

from PIL import Image

from officepack import compress as compress_mod
from officepack import container as ct
from officepack import qr

try:
    import tkinter as tk

    from officepack import gui as gui_mod

    _HAS_TK = True
except Exception:  # pragma: no cover
    _HAS_TK = False


class CompressProbeTest(unittest.TestCase):
    """已压缩格式（rar/zip/jpg）不该让 CPU 白转几十秒。"""

    def test_incompressible_big_file_is_skipped_fast(self):
        data = os.urandom(4 * 1024 * 1024)
        started = time.monotonic()
        out, info = compress_mod.compress(data, "max")
        spent = time.monotonic() - started
        self.assertTrue(info.get("probe_skipped"), "没识别出「压不动」：%s" % info)
        self.assertIs(out, data, "跳过压缩时应原样返回")
        # 老逻辑全量试 4 组要十几秒，现在只跑 256KB 采样
        self.assertLess(spent, 3.0, "采样判定太慢（%.1fs），等于没省时间" % spent)

    def test_compressible_data_is_still_compressed(self):
        data = b"the quick brown fox jumps over the lazy dog %d\n" * 60000
        out, info = compress_mod.compress(data, "max")
        self.assertEqual(info["alg"], "lzma")
        self.assertLess(len(out), len(data) * 0.1)
        self.assertEqual(compress_mod.decompress(out, info), data)

    def test_stream_output_matches_one_shot(self):
        """分块喂 lzma 只是为了报进度，结果必须和一次性压缩逐字节相同。"""
        data = os.urandom(300000)
        filt = {"id": lzma.FILTER_LZMA2, "preset": 6}
        one = lzma.compress(data, format=lzma.FORMAT_XZ, filters=[filt])
        self.assertEqual(compress_mod._lzma_stream(data, filt), one)

    def test_progress_grows_to_total(self):
        seen = []
        data = b"line %d some log text\n" * 80000
        compress_mod.compress(data, "lzma",
                              progress=lambda d, t, s: seen.append((d, t, s)))
        self.assertTrue(seen, "压缩阶段一帧进度都没报")
        self.assertEqual(seen[-1][:2], (len(data), len(data)))
        self.assertEqual([d for d, _t, _s in seen], sorted(d for d, _t, _s in seen))


class PackProgressTest(unittest.TestCase):
    """打包流水线（压缩 → 加密 → 编码）必须能报进度。"""

    def test_stages_are_reported_in_order(self):
        seen = []
        ct.pack_bytes(b"hello world " * 200000, "a.txt", codec_name="qr45",
                      cipher_name="none", password=None, compress="lzma",
                      preview=False, ascii_only=True,
                      progress=lambda d, t, s: seen.append((d, t, s)))
        steps = []
        for _d, _t, step in seen:
            if not steps or steps[-1] != step:
                steps.append(step)
        self.assertEqual(steps, ["压缩", "加密", "编码"])
        self.assertTrue(all(t == ct.PACK_SCALE for _d, t, _s in seen))
        self.assertEqual(seen[-1][0], ct.PACK_SCALE)

    def test_encoded_text_is_reused_not_recomputed(self):
        """打包算过一遍编码，render / split_pack 不该再算第二遍。"""
        pack = ct.pack_bytes(b"abc" * 5000, "a.bin", codec_name="qr45",
                             compress="none", preview=False, ascii_only=True)
        self.assertIsNotNone(pack.encoded)
        first = pack.encoded
        self.assertEqual(pack.encode_text(), first)
        ct.render(pack)
        self.assertEqual(pack.encoded, first)


class WebpMuxTest(unittest.TestCase):
    """PIL 的 save_all 一次性写完（40 秒无进度），改成逐帧并行 + 自拼容器。"""

    def setUp(self):
        raw = os.urandom(60000)
        pack = ct.pack_bytes(raw, "a.bin", codec_name="qr45", cipher_name="none",
                             password=None, compress="none", preview=False,
                             ascii_only=True)
        self.frames = qr.build_frames(pack, ec="M")[:6]

    def test_animation_roundtrip(self):
        import tempfile
        imgs = list(qr._anim_source(self.frames, "M", 420, True, None))
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.webp")
            qr.render_webp(self.frames, path, ec="M", fps=10, target_px=420,
                           progress=lambda d, t, s: seen.append((d, t, s)))
            with Image.open(path) as im:
                self.assertEqual(getattr(im, "n_frames", 1), len(imgs))
                self.assertEqual(im.size, imgs[0].size)
                for idx in range(len(imgs)):
                    im.seek(idx)
                    self.assertEqual(im.convert("L").tobytes(),
                                     imgs[idx].convert("L").tobytes(),
                                     "第 %d 帧像素与原图不一致" % idx)
        # 写入阶段必须有逐帧进度，且停在最后一帧（不是一上来就 100%）
        write = [(d, t) for d, t, s in seen if s == "写入"]
        self.assertTrue(write, "写入 WebP 阶段没报进度")
        self.assertEqual(write[0][0], 0, "写入一开始就报了非零进度")
        self.assertEqual(write[-1], (len(imgs), len(imgs)))

    def test_single_frame_chunks_are_extracted(self):
        buf = io.BytesIO()
        Image.new("L", (60, 60), 255).save(buf, "WEBP", lossless=True, quality=100)
        blob = compress_mod and buf.getvalue()
        chunks = qr._webp_image_chunks(blob)
        self.assertTrue(chunks.startswith(b"VP8"))


@unittest.skipUnless(_HAS_TK, "需要 tkinter")
class ProgressTextTest(unittest.TestCase):
    """进度条文字：内部刻度不外露、一次性写入阶段不能停在 100%。"""

    @classmethod
    def setUpClass(cls):
        try:
            gui_mod._enable_dpi_awareness()
            cls.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover
            raise unittest.SkipTest("无法创建 Tk 窗口：%s" % exc)
        cls.app = gui_mod.App(cls.root, has_dnd=False)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app._on_close()
        except Exception:
            pass
        try:
            cls.root.destroy()
        except Exception:
            pass

    def _pump(self):
        for _ in range(3):
            self.root.update()

    def test_internal_scale_is_hidden(self):
        self.app._set_bar(350, 1000, "压缩", True)
        self._pump()
        text = self.app.prog_text.cget("text")
        self.assertIn("35%", text)
        self.assertNotIn("350", text, "把内部刻度摆给用户看了：%s" % text)

    def test_frame_count_is_shown_when_meaningful(self):
        self.app._set_bar(45, 200, "渲染")
        self._pump()
        text = self.app.prog_text.cget("text")
        self.assertIn("45/200", text)

    def test_one_shot_write_never_shows_100_percent(self):
        class _Worker:
            def is_alive(self):
                return True

        self.app.worker = _Worker()
        self.app._bar_t0 = time.monotonic() - 30.0
        self.app._set_bar(957, 957, "合成动图·写入 GIF")
        self._pump()
        self.assertNotIn("100%", self.app.prog_text.cget("text"))
        # 心跳一秒一次，以前它会把文案覆盖成「100%」，正好盖掉「还要几十秒」
        self.app._bar_tick = 0.0
        self.app._bar_heartbeat()
        self._pump()
        text = self.app.prog_text.cget("text")
        self.assertNotIn("100%", text, "心跳把一次性写入的估时盖掉了：%s" % text)
        self.assertIn("957", text)
        self.app.worker = None

    def test_determinate_write_shows_real_progress(self):
        """WebP 现在能逐帧报进度，就该走正常百分比，而不是动画条。"""
        self.app._set_bar(300, 957, "合成 WebP·写入")
        self._pump()
        text = self.app.prog_text.cget("text")
        self.assertIn("31%", text)
        self.assertIn("300/957", text)
        self.assertFalse(getattr(self.app, "_bar_writing", False))


if __name__ == "__main__":
    unittest.main()
