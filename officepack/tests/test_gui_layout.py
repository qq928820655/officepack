"""界面布局测试：不靠截图，靠几何坐标断言。

为什么要有这组测试：
  「编码器 / 压缩器」和「加密器 / 口令」并成两行两列，靠 grid 的 uniform 等宽实现；
  真正容易出问题的是**同一行的控件没有对齐**、以及**窗口高度不够把底部按钮裁掉**。
  截图肉眼看得见，但自动化跑不了 —— 所以直接问 Tk 要坐标。

这些测试需要能创建 Tk 窗口（Windows 桌面会话下即可，不弹到前台）。
"""

from __future__ import annotations

import unittest

try:
    import tkinter as tk

    from officepack import gui as gui_mod

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

    def test_file_kind_labels(self):
        """表格里的类型列要能认出源码 / 压缩包 / PDF。"""
        self.assertEqual(gui_mod.preview_mod.kind_of(".java"), "纯文本/源码")
        self.assertEqual(gui_mod.preview_mod.kind_of(".rar"), "压缩包")
        self.assertEqual(gui_mod.preview_mod.kind_of(".pdf"), "PDF 文档")
        self.assertEqual(gui_mod.preview_mod.kind_of(".exe"), "可执行/库")

    def test_cursor_monitor_returns_screen_rect(self):
        """框选要覆盖鼠标所在那块屏：矩形必须真实可框，不能是 0×0。"""
        left, top, right, bottom = self.app._cursor_monitor()
        self.assertGreater(right - left, 100, "屏幕宽度异常：%r" % ((left, top, right, bottom),))
        self.assertGreater(bottom - top, 100, "屏幕高度异常：%r" % ((left, top, right, bottom),))
        # 副屏在主屏左边时 left 是负的，这正是「支持多屏」的关键
        self.assertGreaterEqual(right, 0)
        self.assertGreaterEqual(bottom, 0)

    def test_qr_format_offers_sheet_only(self):
        """「只出总览」必须是下拉里的正式选项，不能藏在命令行参数里。"""
        values = self.app.qr_fmt_box.cget("values")
        self.assertIn("PNG（仅总览）", values)
        self.assertIn("PNG（单帧 + 总览）", values)

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


if __name__ == "__main__":
    unittest.main()
