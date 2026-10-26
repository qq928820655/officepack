"""界面布局测试：不靠截图，靠几何坐标断言。

为什么要有这组测试：
  「编码器 / 压缩器」和「加密器 / 口令」并成两行两列，靠 grid 的 uniform 等宽实现；
  真正容易出问题的是**同一行的控件没有对齐**、以及**窗口高度不够把底部按钮裁掉**。
  截图肉眼看得见，但自动化跑不了 —— 所以直接问 Tk 要坐标。

这些测试需要能创建 Tk 窗口（Windows 桌面会话下即可，不弹到前台）。
"""

from __future__ import annotations

import os
import time
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
class _AppFixture(unittest.TestCase):
    """只提供「建一个真实 App」的夹具，本身不含用例（被下面两个类继承）。"""

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


class LayoutTest(_AppFixture):

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
        """二维码不再强制锁死编码器：下拉框始终可改（预设会自动切，但不锁）。"""
        self.app.mode.set("pack")
        self.app._sync_mode()
        self.app.codec.set("base91")
        self.app.qr_enabled.set(True)
        self.app._on_qr_toggle()
        self.assertEqual(self.app.codec.get(), "base91")
        self.assertEqual(str(self.app.codec_box.cget("state")), "readonly")
        self.app.qr_enabled.set(False)
        self.app._on_qr_toggle()

    def test_qr_click_switches_preset(self):
        """勾「生成」→ 预设切 qr（qr45+max）；取消 → 退回 chars。"""
        self.app.mode.set("pack")
        self.app._sync_mode()
        self.app.preset.set("fast（速度快）")
        self.app.codec.set("base64")
        self.app.qr_enabled.set(True)
        self.app._on_qr_click()
        self.assertEqual(self.app.preset.get().split("（")[0], "qr")
        self.assertEqual(self.app.codec.get(), "qr45")
        self.app.qr_enabled.set(False)
        self.app._on_qr_click()
        self.assertEqual(self.app.preset.get().split("（")[0], "chars")
        self.assertEqual(self.app.codec.get(), "cjk")

    def test_mode_switch_keeps_preset(self):
        """打包/还原来回切不该把用户手选的预设冲掉。"""
        self.app.mode.set("pack")
        self.app._sync_mode()
        self.app.preset.set("fast（速度快）")
        self.app.mode.set("unpack")
        self.app._sync_mode()
        self.app.mode.set("pack")
        self.app._sync_mode()
        self.assertEqual(self.app.preset.get().split("（")[0], "fast")

    def test_webp_help_has_content(self):
        """WebP 设置上的 ? 必须能弹出说明（原来 key 不存在，点了什么都不显示）。"""
        self.assertIn("webp", gui_mod.HELP)
        title, intro, items = gui_mod.HELP["webp"]
        self.assertTrue(title and intro and len(items) >= 2)

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
        """选了「WebP 动图」但文件一帧就装下时，要出一张**静态 WebP**。

        以前这里会回落成 PNG，用户于是以为「根本没有单 WebP 导出」。
        选了 WebP 就得给 WebP —— 一帧也是一张合法的 WebP。
        """
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
        self.assertTrue(any(p.endswith(".webp") for p in outputs),
                        "选了 WebP 就该出 WebP，不能偷偷换成 PNG")
        self.assertFalse(any(p.endswith(".png") for p in outputs),
                         "「只出 WebP」不该再写 PNG")

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
        """识别时按界面上的「帧/秒」估算，别拿写死的值硬套。

        屏幕上那段 GIF 就是本程序按这个帧率生成的，两者必须对得上 —— 对不上
        就会算错「播一轮要多久」。默认值 1.4.6 起是 10（旧版是 3），
        手滑打错时退回默认值，不能崩。
        """
        self.app.qr_fps.set("6")
        self.assertAlmostEqual(self.app._scan_fps(), 6.0)
        self.app.qr_fps.set("abc")           # 手滑打错不能崩
        self.assertAlmostEqual(self.app._scan_fps(), 10.0)
        self.app.qr_fps.set("0")             # 0 帧/秒没有意义
        self.assertAlmostEqual(self.app._scan_fps(), 0.5)
        self.app.qr_fps.set("99")            # 超出上限按 20 算
        self.assertAlmostEqual(self.app._scan_fps(), 20.0)
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

    def test_rescan_buttons_tell_how_many_are_missing(self):
        """按钮文字要写清「只补缺的 N 帧」—— 不然用户以为又要从头收一遍。"""
        self.app._last_phys = (100, 200, 600, 700)
        self.app._show_rescan(True, 7)
        self.root_update()
        self.assertIn("7", self.app.replay_btn.cget("text"))
        self.assertIn("只补", self.app.replay_btn.cget("text"))
        self.assertIn("7", self.app.rescan_btn.cget("text"))
        # 两个按钮都能真正拿到高度：日志区 expand 时最容易把后 pack 的挤成 0 高
        self.assertGreater(self.app.replay_btn.winfo_height(), 10)
        self.assertGreater(self.app.rescan_btn.winfo_height(), 10)
        self.app._show_rescan(False)
        self.root_update()

    def test_give_up_clears_collected_frames(self):
        """「放弃本次识别」要把攒下的帧清干净，下次从零开始。

        用户的原话：识别不成功得给个放弃的出口，放弃后把收录的图删掉，
        免得一直占着资源、下次重扫还背着「差 12 帧」的包袱。
        """
        import unittest.mock as mock

        app = self.app
        app._scan_texts = ["#OPK1|x|1|3|aaaa", "#OPK1|x|2|3|bbbb"]
        app._scan_rate = 18.0
        app._show_rescan(True, 1)
        self.root_update()
        self.assertTrue(app.giveup_btn.winfo_ismapped(), "缺帧时该出现「放弃」按钮")
        with mock.patch.object(gui_mod.messagebox, "askyesno", return_value=True), \
                mock.patch.object(gui_mod, "clean_scan_shots", return_value=(3, 2048)):
            app._give_up_scan()
        self.root_update()
        self.assertEqual(app._scan_texts, [], "放弃后帧没清掉")
        self.assertEqual(app._scan_rate, 0.0)
        self.assertFalse(app.giveup_btn.winfo_ismapped(), "放弃后按钮还挂着")

    def test_give_up_keeps_frames_when_cancelled(self):
        """弹窗点「否」就什么都不动 —— 手滑点了放弃不能把几千帧弄丢。"""
        import unittest.mock as mock

        app = self.app
        app._scan_texts = ["#OPK1|x|1|3|aaaa"]
        with mock.patch.object(gui_mod.messagebox, "askyesno", return_value=False):
            app._give_up_scan()
        self.assertEqual(app._scan_texts, ["#OPK1|x|1|3|aaaa"])
        app._scan_texts = []

    def test_fresh_grab_region_discards_previous(self):
        """菜单里的「框选屏幕区域」= 重新开始，上一次攒的帧必须作废。

        用户实测：拿一份**新文件**来框选，工具却「接着上次」，把老文件的帧拼进来 ——
        老文件碰巧已经齐了才没出事。只有「再来一轮 / 重新框选（接着收）」才该续扫。
        """
        import inspect

        import unittest.mock as mock

        src = inspect.getsource(gui_mod.App.grab_region)
        self.assertIn("resume: bool = False", src, "默认必须是「重新开始」")
        self.assertIn("_drop_scan_accum", src)
        self.assertIn("resume=True", inspect.getsource(gui_mod.App._replay_scan),
                      "「再来一轮」必须续扫，不能把已收的丢掉")

        app = self.app
        app._scan_texts = ["#OPK1|x|1|3|aaaa", "#OPK1|x|2|3|bbbb"]
        app._scan_rate = 12.0
        with mock.patch.object(gui_mod, "clean_scan_shots", return_value=(2, 1024)):
            n = app._drop_scan_accum("重新框选")
        self.assertEqual(n, 2)
        self.assertEqual(app._scan_texts, [], "作废后还留着帧")
        self.assertEqual(app._scan_rate, 0.0)

    def test_idle_hides_the_progress_bar(self):
        """空闲时不该露出横杠；而且只该有**一条** 6px 的。

        以前有两条（文件级 + 帧级），文件级在处理单个文件时永远停在 0/100%，
        用户直接问「这道横杠有什么用」，还白占 22px 日志高度。

        注意：**行本身**常驻（固定高度占位），藏的是行里的条子和文字。
        行也跟着藏的话，处理到合成阶段整窗需要的高度会突然多 28px，
        窗口长不高就只好去压右侧选项区（→ 滚动条）和底部（→ 进度行被裁）。
        """
        self.app._show_bars(False)
        self.root_update()
        self.assertFalse(self.app.sub_progress.winfo_ismapped(), "空闲时进度条还露着")
        self.assertFalse(hasattr(self.app, "progress"), "文件级那条已经删了")
        self.app._show_bars(True)
        self.root_update()
        self.assertTrue(self.app.sub_progress.winfo_ismapped(), "干活时进度条要出来")
        self.assertLessEqual(self.app.sub_progress.winfo_height(), 8,
                             "进度条必须压到 6px 左右，否则又抢日志的高度")
        self.app._show_bars(False)
        self.root_update()

    def test_progress_row_never_changes_height(self):
        """切到进度条状态时，整窗需要的高度必须**一点不变**。

        这是「合成阶段底部显示不全」的根因回归：那一行原来是按需 pack 的，
        出现时总高 +28px，窗口已经贴到工作区下沿就压别处。
        """
        self.app._show_bars(False)
        self.root_update()
        idle_h = self.app._sub_row.winfo_height()
        idle_req = self.root.winfo_reqheight()
        self.app._show_bars(True)
        self.app._set_bar(425, 425, "合成 WebP·写入 WebP")
        self.root_update()
        self.assertEqual(self.app._sub_row.winfo_height(), idle_h,
                         "进度行高度不许随「有没有活」变化")
        self.assertEqual(self.root.winfo_reqheight(), idle_req,
                         "出现进度条时整窗需要的高度不许变")
        self.app._set_bar(100, 425, "渲染矩阵")
        self.root_update()
        self.assertEqual(self.root.winfo_reqheight(), idle_req,
                         "换阶段名（文字变长变短）也不许改变整窗高度")
        self.app._show_bars(False)
        self.root_update()

    def test_progress_bar_is_capped_and_text_never_clipped(self):
        """进度条要封顶（用户要求「缩短一点」），文字必须完整显示。

        老写法用 ``width=31``（字符数）框住文字，阶段名一长，末尾的
        「100% (3825/3825)」就被硬裁掉 —— 那才是最该看到的数字。
        """
        self.app._show_bars(True)
        # 注意别用带「写入」的阶段名：那会切到来回滚动的动画，只显示
        # 「大文件要几十秒」，看不到百分比 —— 这里要验的是百分比文字完不完整
        self.app._set_bar(3825, 3825, "合成动图·渲染矩阵")
        self.root_update()
        self.assertLessEqual(self.app.sub_progress.winfo_width(),
                             self.app._bar_w + 2, "进度条超过封顶长度了")
        text = self.app.prog_text.cget("text")
        self.assertIn("100%", text)
        self.assertIn("3825/3825", text)
        self.assertLessEqual(self.app.prog_text.winfo_reqwidth(),
                             self.app.prog_text.winfo_width() + 1,
                             "文字控件比文字窄，末尾会被裁掉")
        # 超长的（多文件序号 + 长阶段名）自己省略，绝不能顶到窗口外面
        long_text = "12/34 · 合成 WebP 写入 WebP 100% (3825/3825) · 99.9s"
        self.app._set_bar_text(long_text)
        self.root_update()
        self.assertLessEqual(len(self.app.prog_text.cget("text")), 42)
        self.assertTrue(self.app.prog_text.cget("text").endswith("…"))
        self.app._show_bars(False)
        self.root_update()

    def test_hud_remembers_alpha_and_position(self):
        """悬浮条的透明度和位置要记住，下次扫描沿用。"""
        import json
        import tempfile

        tmp = tempfile.mkdtemp(prefix="opk-hud-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        cfg = os.path.join(tmp, "hud.json")
        old = gui_mod.HUD_CFG
        gui_mod.HUD_CFG = cfg
        self.addCleanup(lambda: setattr(gui_mod, "HUD_CFG", old))

        self.assertEqual(gui_mod._load_hud_cfg(), (0.92, None))   # 没配置时的默认值
        gui_mod._save_hud_cfg(0.55, (120, 240))
        with open(cfg, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["pos"], [120, 240])
        self.assertAlmostEqual(gui_mod._load_hud_cfg()[0], 0.55)
        self.assertEqual(gui_mod._load_hud_cfg()[1], (120, 240))

        gui_mod._save_hud_cfg(0.01, (1, 2))       # 越界的透明度要夹回来
        self.assertGreaterEqual(gui_mod._load_hud_cfg()[0], 0.35)
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("{ 坏掉的 json")
        self.assertEqual(gui_mod._load_hud_cfg(), (0.92, None), "配置坏了不能崩")

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
        win.attributes("-topmost", True)      # 全量跑测试时别的窗口可能刚好压在它上面
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
            # 桌面刚画出来时可能还抓到上一帧（别的测试的窗口），重试几次再判定
            found, shot = [], None
            for _ in range(6):
                try:
                    shot = grab()
                except OSError as exc:
                    # 锁屏 / 远程桌面断开时 BitBlt 会直接失败，这是环境限制，
                    # 不是抓图代码写错了 —— 跳过，别让全量测试红一片。
                    self.skipTest("这台机器现在抓不到桌面：%s" % exc)
                found = qr.decode_image(shot, deep=False)
                if found:
                    break
                win.lift()
                self.root.update()
                _t.sleep(0.3)
            self.assertEqual(shot.size, (bbox[2] - bbox[0], bbox[3] - bbox[1]))
            self.assertTrue(found, "GDI 抓到的画面里没有码 —— 坐标空间搞错了")
        finally:
            win.destroy()
            self.root.update()

    def test_scan_keeps_last_frame_for_diagnosis(self):
        """扫描没认出东西时要留下最后一张画面，否则「识别不了」无从定位。"""
        from PIL import Image

        texts, stats = qr.scan_region(lambda: Image.new("RGB", (64, 64), "white"))
        self.assertEqual(texts, [])
        self.assertIsNotNone(stats.get("image"), "没留下最后一帧，没法诊断")


class ScanWindowAvoidTest(unittest.TestCase):
    """扫描期间窗口该「最小化」还是「藏起来」。

    用户的原话：**「框选时程序只是最小化，任务栏还有图标可以再打开」**。
    所以不能再 withdraw（任务栏连影子都没有，中途想回来看日志、点停止都无从
    下手）；用户把窗口点回来时也不能再强行藏掉（点了没反应），只能挪到框外。
    """

    def test_overlaps_counts_edge_touching_as_free(self):
        self.assertFalse(gui_mod._overlaps(0, 0, 100, 100, (100, 0, 300, 300)))
        self.assertTrue(gui_mod._overlaps(50, 50, 100, 100, (100, 0, 300, 300)))

    def test_free_spot_picks_a_corner_outside_the_box(self):
        # 双屏：右屏被框住，左屏空着 —— 窗口应该挪到左屏的角上
        mons = [(0, 0, 1536, 960), (1536, 0, 3072, 864)]
        box = (1912, 72, 2680, 832)
        spot = gui_mod._free_spot(900, 700, box, mons)
        self.assertIsNotNone(spot, "左屏整块空着却没找到位置")
        self.assertFalse(gui_mod._overlaps(spot[0], spot[1], 900, 700, box),
                         "挪过去还是压着框选区域")

    def test_free_spot_returns_none_when_the_box_covers_every_screen(self):
        mons = [(0, 0, 1536, 960)]
        self.assertIsNone(gui_mod._free_spot(900, 700, (-10, -10, 1600, 1000), mons))

    def test_scan_minimizes_instead_of_withdrawing(self):
        """回归：扫描期间必须是 iconify（任务栏留图标），不是 withdraw。"""
        import inspect

        src = inspect.getsource(gui_mod.App.grab_region)
        self.assertIn("self.root.iconify()", src,
                      "扫描期间不最小化，任务栏图标就没了，用户中途回不来")
        beat = inspect.getsource(gui_mod.App._bar_heartbeat)
        self.assertNotIn("withdraw", beat,
                         "用户点回来的窗口又被藏掉，等于点不动")
        self.assertIn("_avoid_scan_bbox", beat)


class MatrixFitTest(_AppFixture):
    """矩阵尺寸与屏幕适配：3×3 在 1080p 上「能播但认不全」是算得出来的。

    继承 LayoutTest 只为复用那套 Tk 夹具（矩阵账要读真实工作区）。
    """

    # ---- 矩阵尺寸与屏幕适配 -------------------------------------------------

    def test_matrix_fit_uses_work_area_and_code_size(self):
        """每张码的边长 = 工作区短边 / N；像素/模块按 177 模块算。

        这条守住的是「3×3 在 1080p 上只能认出 6/9」那个结论的**计算前提**：
        单码边长必须和渲染时一致（720），否则算出来的提示是假的。
        """
        from officepack import gui as g

        app = self.app
        fit = app._matrix_display_fit(2)
        self.assertIsNotNone(fit)
        need, avail, per, ppm = fit
        self.assertEqual(need, app.QR_CODE_PX * 2 + app.QR_GAP_PX)
        self.assertGreater(avail, 0)
        self.assertAlmostEqual(per, avail / 2.0, places=3)
        self.assertAlmostEqual(ppm, per / 177.0, places=6)

    def test_matrix_fit_grows_with_grid(self):
        """帧数降到 1/N² 的代价必须体现在「每码像素」上，否则提示没意义。"""
        app = self.app
        p1 = app._matrix_display_fit(1)[3]
        p2 = app._matrix_display_fit(2)[3]
        p3 = app._matrix_display_fit(3)[3]
        self.assertGreater(p1, p2)
        self.assertGreater(p2, p3)

    def test_warn_matrix_fit_speaks_only_when_needed(self):
        """够用时给一行确认，不够时才出 ⚠ 和「改用 N×N」的建议。"""
        app = self.app
        logs = []
        app.queue.put = lambda item: logs.append(item)   # 拦下日志
        try:
            app._warn_matrix_fit(1)
            self.assertTrue(any("矩阵：原图" in str(x) for x in logs), logs)
            self.assertFalse(any("⚠" in str(x) for x in logs), logs)

            logs.clear()
            app._warn_matrix_fit(3)
            text = " ".join(str(x) for x in logs)
            self.assertIn("像素/模块", text)
            # 3×3 的门槛是 2.5 像素/模块；不够时必须给出警示和替代方案，
            # 并且要讲清「正方形被切掉一条边 = 整行码丢失」这个真实原因
            if app._matrix_display_fit(3)[3] < 2.5:
                self.assertIn("⚠", text)
                self.assertIn("建议", text)
                self.assertIn("F11", text)
        finally:
            del app.queue.put

    def test_qr_help_mentions_module_pixels(self):
        """帮助里必须写清真实约束（每模块像素），不能只说「窗口放得下」。"""
        def flat(node):
            if isinstance(node, str):
                return node
            if isinstance(node, (tuple, list)):
                return " ".join(flat(x) for x in node)
            return ""

        text = flat(list(gui_mod.HELP.values()))
        self.assertIn("模块", text, "帮助没讲「每张码还剩多少像素」这件事")
        self.assertIn("3×3", text)
        self.assertIn("像素/模块", text)


@unittest.skipUnless(_HAS_TK, "需要 tkinter")
class SpeedTuneTest(_AppFixture):
    """补录前自动提速：只在**算得出来更快**的时候动用户的帧率设置。"""

    def root_update(self):
        try:
            self.root.update()
        except tk.TclError:
            pass

    def test_raises_fps_when_rounds_would_be_many(self):
        self.app.qr_fps.set("3")
        self.app._scan_plan = {"fastest": "raise_fps", "target_fps": 12.0,
                               "rounds_left": 3}
        self.app._tune_fps_for_speed()
        self.root_update()
        self.assertEqual(self.app.qr_fps.get(), "12",
                         "补 3 轮 vs 一轮：应当把帧率提到播放率的 1.2 倍")

    def test_keeps_fps_when_one_round_is_enough(self):
        self.app.qr_fps.set("3")
        self.app._scan_plan = {"fastest": "one_more_round", "target_fps": 12.0,
                               "rounds_left": 1}
        self.app._tune_fps_for_speed()
        self.root_update()
        self.assertEqual(self.app.qr_fps.get(), "3", "一轮就齐时不该改用户的设置")

    def test_never_lowers_the_user_setting(self):
        """已经比目标快就别动 —— 不能把 20 帧/秒 "优化" 成 12。"""
        self.app.qr_fps.set("20")
        self.app._scan_plan = {"fastest": "raise_fps", "target_fps": 12.0,
                               "rounds_left": 4}
        self.app._tune_fps_for_speed()
        self.root_update()
        self.assertEqual(self.app.qr_fps.get(), "20")

    def test_caps_at_the_slider_limit(self):
        """目标超过 20 时夹到 20（滑块上限）；超过 6 帧/秒本就不再加人为间隔。"""
        self.app.qr_fps.set("3")
        self.app._scan_plan = {"fastest": "raise_fps", "target_fps": 48.0,
                               "rounds_left": 5}
        self.app._tune_fps_for_speed()
        self.root_update()
        self.assertEqual(self.app.qr_fps.get(), "20")

    def test_no_plan_is_a_no_op(self):
        self.app.qr_fps.set("3")
        self.app._scan_plan = None
        self.app._tune_fps_for_speed()
        self.root_update()
        self.assertEqual(self.app.qr_fps.get(), "3")

    def test_rescan_buttons_do_not_squeeze_the_options(self):
        """补录按钮出现后，选项区不许被压出滚动条。

        回归：收尾只安排在 80ms 后跑一次，而那时 Tk 还没完成布局、量到的是
        旧高度，于是判成「装得下」直接返回 —— 之后再没人复查，右侧就冒出
        滚动条。现在 80ms 和 300ms 各收尾一次。
        """
        self.app._last_phys = (100, 100, 600, 700)
        self.app._show_rescan(True, 12)
        deadline = time.time() + 5.0
        content = 0
        while time.time() < deadline:
            self.root_update()
            time.sleep(0.02)
            bbox = self.app.opt_canvas.bbox("all")
            content = bbox[3] - bbox[1] if bbox else 0
            if content and content <= self.app.opt_canvas.winfo_height() + 2:
                break
        self.app._show_rescan(False)
        self.root_update()
        self.assertLessEqual(
            content, self.app.opt_canvas.winfo_height() + 2,
            "补录按钮出现后选项区被压出滚动条（内容 %d > 画布 %d）"
            % (content, self.app.opt_canvas.winfo_height()))


if __name__ == "__main__":
    unittest.main()
