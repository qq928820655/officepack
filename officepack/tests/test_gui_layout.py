"""界面布局测试：不靠截图，靠几何坐标断言。

为什么要有这组测试：
  「编码器 / 压缩器」和「加密器 / 口令」并成两行两列，靠 grid 的 uniform 等宽实现；
  真正容易出问题的是**同一行的控件没有对齐**、以及**窗口高度不够把底部按钮裁掉**。
  截图肉眼看得见，但自动化跑不了 —— 所以直接问 Tk 要坐标。

这些测试需要能创建 Tk 窗口（Windows 桌面会话下即可，不弹到前台）。
"""

from __future__ import annotations

import os
import unittest

try:
    import tkinter as tk

    from officepack import gui as gui_mod
    from officepack import qr

    _HAS_TK = True
except Exception:  # pragma: no cover - 无 tkinter 的环境跳过
    _HAS_TK = False


def _row_y(widget) -> int:
    """控件在屏幕上的纵向位置：同一行必须相等。"""
    widget.update_idletasks()
    return widget.winfo_rooty()


def _left_x(widget) -> int:
    return widget.winfo_rootx()


@unittest.skipUnless(_HAS_TK, "需要 tkinter")
class LayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            # 和真实启动一致：先声明 DPI 感知，否则多屏不同缩放时坐标会被虚拟化
            gui_mod._enable_dpi_awareness()
            cls.root = tk.Tk()
        except tk.TclError as exc:      # pragma: no cover
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

    def test_codec_and_compress_share_one_row(self):
        """编码器与压缩器必须在同一行（需求：2 行 2 列）。"""
        self.assertAlmostEqual(_row_y(self.app.codec_box),
                               _row_y(self.app.compress_box), delta=2)

    def test_cipher_and_password_share_one_row(self):
        self.assertAlmostEqual(_row_y(self.app.cipher_box),
                               _row_y(self.app.pwd_entry), delta=2)

    def test_two_rows_are_stacked(self):
        """第二行必须比第一行低，且不超过两行控件的高度。"""
        y1 = _row_y(self.app.codec_box)
        y2 = _row_y(self.app.cipher_box)
        self.assertGreater(y2, y1)
        self.assertLess(y2 - y1, self.app.codec_box.winfo_height() * 3)

    def test_pairs_are_two_columns(self):
        """左列在左、右列在右，并且两列宽度接近（uniform="pair" 生效）。"""
        self.assertLess(_left_x(self.app.codec_box), _left_x(self.app.compress_box))
        self.assertLess(_left_x(self.app.cipher_box), _left_x(self.app.pwd_entry))
        w1 = self.app.codec_box.winfo_width()
        w2 = self.app.compress_box.winfo_width()
        self.assertLessEqual(abs(w1 - w2), 4, "两列应等宽：%d vs %d" % (w1, w2))

    def test_bottom_buttons_not_clipped(self):
        """窗口高度必须容得下最后一个按钮（老 bug：底部按钮被裁）。"""
        self.root.update_idletasks()
        help_btn = self.app.help_btn
        win_bottom = self.root.winfo_rooty() + self.root.winfo_height()
        self.assertLessEqual(help_btn.winfo_rooty() + help_btn.winfo_height(),
                             win_bottom + 1,
                             "「全部参数说明」按钮超出窗口底部")

    def test_every_widget_inside_window(self):
        """所有可交互控件都落在窗口可见区域内。"""
        self.root.update_idletasks()
        left = self.root.winfo_rootx()
        top = self.root.winfo_rooty()
        right = left + self.root.winfo_width()
        bottom = top + self.root.winfo_height()
        for name in ("go", "open_btn", "paste_btn", "shot_btn", "help_btn",
                     "codec_box", "compress_box", "cipher_box", "pwd_entry",
                     "seg_entry", "qr_chk", "split_chk"):
            w = getattr(self.app, name, None)
            if w is None:
                continue
            with self.subTest(widget=name):
                self.assertGreaterEqual(w.winfo_rootx(), left - 1)
                self.assertGreaterEqual(w.winfo_rooty(), top - 1)
                self.assertLessEqual(w.winfo_rootx() + w.winfo_width(), right + 1)
                self.assertLessEqual(w.winfo_rooty() + w.winfo_height(), bottom + 1)

    def test_qr_toggle_does_not_lock_codec(self):
        """二维码不再强制 base32：勾选前后编码器必须原样保留且可改。"""
        self.app.mode.set("pack")
        self.app._sync_mode()
        self.app.codec.set("base91")
        self.app.qr_enabled.set(True)
        self.app._on_qr_toggle()
        self.assertEqual(self.app.codec.get(), "base91")
        self.assertEqual(str(self.app.codec_box.cget("state")), "readonly")
        self.app.qr_enabled.set(False)
        self.app._on_qr_toggle()

    def test_qr_and_split_are_exclusive(self):
        self.app.mode.set("pack")
        self.app.split_enabled.set(True)
        self.app.qr_enabled.set(True)
        self.app._on_qr_toggle()
        self.assertFalse(self.app.split_enabled.get())
        self.app.qr_enabled.set(False)
        self.app._on_qr_toggle()

    def test_queue_call_runs_in_main_thread(self):
        """工作线程派回主线程的 call 必须真的执行 —— 框选扫描靠它收尾。"""
        hit = []
        self.app.queue.put(("call", lambda: hit.append(1)))
        self.app._poll_queue()
        self.assertTrue(hit, "队列里的 call 回调没有被执行")

    def test_scan_buffer_accumulates_frames(self):
        """两次框选要能累加：漏帧时再框一次就补上，不必从头来。"""
        part = ["#OPK1 aaa 001/002 x 10\nAAA"]
        self.assertIn("第 2 帧", self.app._missing_frames(part))
        merged = part + ["#OPK1 aaa 002/002 x 10\nBBB"]
        self.assertEqual(self.app._missing_frames(merged), "")

    def test_file_kind_labels(self):
        """表格里的类型列要能认出源码 / 压缩包 / PDF。"""
        self.assertEqual(gui_mod.preview_mod.kind_of(".java"), "纯文本/源码")
        self.assertEqual(gui_mod.preview_mod.kind_of(".rar"), "压缩包")
        self.assertEqual(gui_mod.preview_mod.kind_of(".pdf"), "PDF 文档")
        self.assertEqual(gui_mod.preview_mod.kind_of(".exe"), "可执行/库")

    def test_cursor_monitor_returns_screen_rect(self):
        """鼠标所在显示器的矩形必须真实可框，不能是 0×0。"""
        left, top, right, bottom = self.app._cursor_monitor()
        self.assertGreater(right - left, 100, "屏幕宽度异常：%r" % ((left, top, right, bottom),))
        self.assertGreater(bottom - top, 100, "屏幕高度异常：%r" % ((left, top, right, bottom),))
        # 副屏在主屏左边时 left 是负的，这正是「支持多屏」的关键
        self.assertGreaterEqual(right, 0)
        self.assertGreaterEqual(bottom, 0)

    def test_virtual_screen_wraps_every_monitor(self):
        """虚拟桌面必须包住所有显示器 —— 少包一块，那块屏就框不了。"""
        mons = self.app._monitors()
        self.assertTrue(mons, "枚举不到任何显示器")
        vx, vy, vw, vh = self.app._virtual_screen()
        for ml, mt, mr, mb in mons:
            self.assertGreaterEqual(ml, vx, "显示器左边界溢出了虚拟桌面")
            self.assertGreaterEqual(mt, vy, "显示器上边界溢出了虚拟桌面")
            self.assertLessEqual(mr, vx + vw, "显示器右边界溢出了虚拟桌面")
            self.assertLessEqual(mb, vy + vh, "显示器下边界溢出了虚拟桌面")

    def test_overlay_window_covers_all_monitors(self):
        """框选遮罩必须盖住**所有**屏幕，而不是只有鼠标所在那一块。

        这是 v1.4.0 的回归点：旧的实现按「鼠标所在显示器」铺遮罩，双屏时
        另一块屏不暗、点上去也没反应，用户会以为功能坏了。
        """
        import ctypes
        import ctypes.wintypes as wt

        top, canvas, vx, vy, vw, vh = self.app._build_overlay()
        try:
            self.root.update()
            u32 = ctypes.windll.user32
            hwnd = top.winfo_id()
            frame = u32.GetParent(hwnd) or hwnd
            rect = wt.RECT()
            self.assertTrue(u32.GetWindowRect(frame, ctypes.byref(rect)),
                            "拿不到遮罩窗口矩形")
            got = (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
            self.assertEqual(got, (vx, vy, vw, vh),
                             "遮罩没铺满虚拟桌面：实际 %r，应为 %r"
                             % (got, (vx, vy, vw, vh)))
            for ml, mt, mr, mb in self.app._monitors():
                self.assertTrue(rect.left <= ml and rect.top <= mt
                                and rect.right >= mr and rect.bottom >= mb,
                                "显示器 %r 没被遮罩盖住" % ((ml, mt, mr, mb),))
            # 每块屏顶部都要有提示文字，不然双屏时另一块屏上一片黑没说明
            texts = [i for i in canvas.find_all() if canvas.type(i) == "text"]
            self.assertEqual(len(texts), max(1, len(self.app._monitors())),
                             "提示文字数量与显示器数量不一致")
        finally:
            top.destroy()
            self.root.update()

    def test_qr_format_offers_sheet_only(self):
        """「只出总览」必须是下拉里的正式选项，不能藏在命令行参数里。"""
        values = self.app.qr_fmt_box.cget("values")
        self.assertIn("PNG（仅总览）", values)
        self.assertIn("PNG（单帧 + 总览）", values)

    def test_qr_format_offers_webp_only(self):
        """WebP 也要能**单独**出，不能只能跟 GIF 绑在一起。"""
        values = self.app.qr_fmt_box.cget("values")
        self.assertIn("WebP 动图", values)
        self.assertIn("GIF + WebP", values)

    def test_single_frame_with_anim_format_still_writes_something(self):
        """选了「WebP 动图」但文件一帧就装下时，不能一个产物都不写。"""
        import tempfile

        tmp = tempfile.mkdtemp(prefix="opk-single-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        src = os.path.join(tmp, "small.txt")
        with open(src, "wb") as fh:
            fh.write(b"tiny")            # 一帧就装得下
        outputs: list[str] = []
        self.app._pack_one_qr(src, "small.txt", tmp, None, "none", "auto",
                              "qr45", outputs, "WebP 动图", "3")
        self.assertTrue(outputs, "选了动图格式 + 单帧 ＝ 什么都没输出")
        self.assertTrue(any(p.endswith(".png") for p in outputs),
                        "单帧应当回落成 PNG，而不是硬塞一个动图")

    def test_paste_panel_opens_and_recovers(self):
        """粘贴识别面板要能开出来（含 Ditto 按钮），关掉后不留残窗。"""
        before = set(self.root.winfo_children())
        self.app.paste_unpack()
        self.root.update()
        panels = [w for w in self.root.winfo_children()
                  if isinstance(w, tk.Toplevel) and w not in before
                  and "粘贴" in w.title()]
        self.assertTrue(panels, "粘贴识别面板没有打开")
        panel = panels[-1]
        from tkinter import ttk as ttk_mod

        buttons = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, ttk_mod.Button):
                    buttons.append(child.cget("text"))
                walk(child)

        walk(panel)
        for text in ("读剪贴板", "打开 Ditto 剪贴板", "选图片文件…", "还原"):
            self.assertIn(text, buttons, "面板缺按钮：%s" % text)
        panel.destroy()
        self.root.update()

    # ---- 跨屏框选：逻辑坐标 vs 物理像素 -------------------------------------

    def test_grab_scale_detects_dpi_scaling(self):
        """整屏抓图比逻辑桌面宽 = 有 DPI 缩放，必须能算出来。

        本机实测过的真 bug：物理桌面 3840×1200，但 Tk / GetSystemMetrics 只说
        3072×960（125%）—— 框在副屏时按逻辑坐标去抓，抓到的是主屏右半边的空白，
        于是「同一块屏能识别、换块屏就框不准」。
        """
        from PIL import Image

        vw = self.app._virtual_screen()[2]

        class FakeGrab:
            def __init__(self, width):
                self.width_ = width

            def grab(self, bbox=None, all_screens=False):
                return Image.new("RGB", (self.width_, 1200))

        self.assertAlmostEqual(self.app._grab_scale(FakeGrab(vw)), 1.0, places=2)
        self.assertAlmostEqual(self.app._grab_scale(FakeGrab(int(vw * 1.25))), 1.25,
                               places=2)

    def test_bbox_is_scaled_to_physical_pixels(self):
        """副屏上的框（逻辑 1536 起）换算后必须落到主屏物理宽度之后。"""
        box = (1536, 100, 2300, 840)
        phys = self.app._scale_bbox(box, 1.25)
        self.assertEqual(phys, (1920, 125, 2875, 1050))
        self.assertGreater(phys[0], 1536, "换算后仍在主屏范围，等于没换算")
        self.assertEqual(self.app._scale_bbox(box, 1.0), box,
                         "scale=1 时不该动坐标")

    def test_frame_ranges_are_compressed(self):
        """978 帧缺 700 个时，逐个列是一千多字符的流水账，要压成区间。"""
        nums = list(range(1, 119)) + list(range(209, 548))
        from officepack.gui import _frame_ranges

        self.assertEqual(_frame_ranges(nums), "1–118、209–547")
        self.assertEqual(_frame_ranges([5]), "5")
        self.assertTrue(_frame_ranges([]) == "")
        many = sum((list(range(i * 10, i * 10 + 3)) for i in range(12)), [])
        self.assertIn("等", _frame_ranges(many), "段数太多时应当截断")

    def test_eta_is_said_in_human_words(self):
        """「这得识别到什么时候」—— 角落的小条必须给出人话版的剩余时间。"""
        from officepack.gui import _fmt_eta

        self.assertEqual(_fmt_eta(None), "正在估时")
        self.assertEqual(_fmt_eta(1.2), "就快好了")
        self.assertEqual(_fmt_eta(41), "还要 41 秒")
        self.assertEqual(_fmt_eta(100), "还要 1 分 40 秒")
        self.assertEqual(_fmt_eta(600), "还要约 10 分钟")

    def test_scan_fps_follows_the_render_setting(self):
        """识别时按界面上的「帧/秒」估算，别拿写死的 3 帧/秒硬套。

        屏幕上那段 GIF 就是本程序按这个帧率生成的，两者必须对得上 —— 对不上
        就会算错「播一轮要多久」。
        """
        self.app.qr_fps.set("6")
        self.assertAlmostEqual(self.app._scan_fps(), 6.0)
        self.app.qr_fps.set("abc")           # 手滑打错不能崩
        self.assertAlmostEqual(self.app._scan_fps(), 3.0)
        self.app.qr_fps.set("0")             # 0 帧/秒没有意义
        self.assertAlmostEqual(self.app._scan_fps(), 0.5)
        self.app.qr_fps.set("3")

    def test_replay_button_reuses_last_region(self):
        """补录几十个帧不该再拖一次框 —— 记住上次区域，一键重扫。"""
        self.app._last_phys = (100, 200, 600, 700)
        self.app._show_rescan(True)
        self.root_update()
        self.assertTrue(self.app.replay_btn.winfo_ismapped(),
                        "有上次区域时应当能「再来一轮」")
        self.assertTrue(self.app.rescan_btn.winfo_ismapped())
        self.app._show_rescan(False)
        self.root_update()
        self.assertFalse(self.app.replay_btn.winfo_ismapped())
        self.assertFalse(self.app.rescan_btn.winfo_ismapped())

    def root_update(self):
        try:
            self.root.update()
        except tk.TclError:
            pass

    def test_gdi_grabber_reads_a_real_code(self):
        """GDI 只抓框内那块（比 ImageGrab 快 4 倍），但必须抓对位置。"""
        import time as _t

        from PIL import ImageGrab, ImageTk

        if not hasattr(gui_mod, "_make_grabber"):
            self.skipTest("没有 GDI 抓图实现")
        pack = qr.ct.pack_bytes(b"gdi grabber " * 40, "g.bin", codec_name="qr45",
                                compress="none", cipher_name="none", preview=False)
        frames = qr.build_frames(pack, "L")
        img = qr._render_uniform(frames[:1], "L", 400)[0]
        vx, vy, vw, vh = self.app._virtual_screen()
        px, py, side = vw // 2 + 40, 60, 400
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.geometry("%dx%d+%d+%d" % (side, side, px, py))
        lbl = tk.Label(win, bg="white")
        lbl.pack(fill="both", expand=True)
        photo = ImageTk.PhotoImage(img)
        lbl.configure(image=photo)
        self.root.update()
        _t.sleep(0.4)
        self.root.update()
        try:
            scale = self.app._grab_scale(ImageGrab)
            bbox = self.app._scale_bbox((px, py, px + side, py + side), scale)
            grab = gui_mod._make_grabber(bbox)
            if grab is None:
                self.skipTest("这台机器 GDI 抓图不可用")
            shot = grab()
            self.assertEqual(shot.size, (bbox[2] - bbox[0], bbox[3] - bbox[1]))
            self.assertTrue(qr.decode_image(shot, deep=False),
                            "GDI 抓到的画面里没有码 —— 坐标空间搞错了")
        finally:
            win.destroy()
            self.root.update()

    def test_scan_keeps_last_frame_for_diagnosis(self):
        """扫描没认出东西时要留下最后一张画面，否则「识别不了」无从定位。"""
        from PIL import Image

        texts, stats = qr.scan_region(lambda: Image.new("RGB", (64, 64), "white"))
        self.assertEqual(texts, [])
        self.assertIsNotNone(stats.get("image"), "没留下最后一帧，没法诊断")


if __name__ == "__main__":
    unittest.main()
