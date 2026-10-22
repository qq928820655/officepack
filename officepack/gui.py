"""图形界面：拖拽或选择文件，一键打包 / 还原。

依赖 tkinter（Python 自带）。若安装了 tkinterdnd2 则支持拖拽，否则退回按钮选择。
    pip install tkinterdnd2     # 可选，仅用于拖拽
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import clipboard as clip_mod
from . import container as ct
from . import codecs as codec_mod
from . import compress as compress_mod
from . import crypto as crypto_mod
from . import preview as preview_mod
from . import qr

try:  # 拖拽支持为可选项
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _HAS_DND = True
except Exception:  # pragma: no cover
    _HAS_DND = False

APP_TITLE = "officepack — 任意文件 ↔ 纯文本"

#: 打包（文件 → 文本）其实**任意文件都能进**，这里只是给对话框分组、给表格标类型。
#: 列表来自 preview.py，两边共用一套定义，避免两处各写一份还写歪。
OFFICE_EXTS = preview_mod.OOXML_EXTS + preview_mod.OLE2_EXTS + (".rtf", ".odt", ".ods", ".odp")
DOC_EXTS = preview_mod.TEXT_EXTS + preview_mod.PDF_EXTS
PKG_EXTS = preview_mod.ZIP_EXTS + preview_mod.ARCHIVE_EXTS
MEDIA_EXTS = preview_mod.IMAGE_EXTS + preview_mod.MEDIA_EXTS

#: 还原（文本 → 文件）能吃的：文本容器 + 二维码图片
TEXT_EXTS = (".txt", ".opk") + qr.IMAGE_EXTS

# 每个参数的说明：(标题, 一句话说明, [(选项名, 详细作用)])
# 显示时会把「当前选中的选项」高亮出来，方便对照
HELP: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "preset": (
        "预设",
        "一键组合「编码器 + 压缩器」。改下面任意一项，预设会自动变成「自定义」。",
        [
            ("chars", "汉字编码 + 最强压缩。字符数最少（1 个汉字装 14 bit），适合「每次只能输入少量字」的渠道；"
                      "代价是 UTF-8 字节数最大，按字节限额的渠道不划算。"),
            ("compact", "base91 + auto 压缩。ASCII 文本里体积最小，适合按字节计费的渠道（邮件、短信、接口）。"),
            ("safe", "base32 + auto 压缩。纯大写字母+数字，不会被邮件/IM 改大小写或插转义符，"
                     "体积比 compact 大约 30%，换来的是「怎么转手都不坏」。"),
            ("fast", "base64 + zlib。编解码最快，压缩率一般，适合大批量小文件。"),
            ("plain", "base64 + 不压缩。最透明，肉眼能看出是 base64，适合小文件或需要人工核对。"),
            ("自定义", "不用预设，完全按下面手动选的编码器 / 压缩器组合。"),
        ],
    ),
    "codec": (
        "编码器",
        "把二进制字节映射成可打印字符。数字是「每个原始字节要占几个字符」，越小则字符数越少。",
        [
            ("cjk", "0.571 字符/字节 —— 每 14 bit 编成一个常用汉字（U+4E00 起 16384 个），"
                    "字符数最少，是唯一比原始字节还少的编码。但 UTF-8 一个字占 3 字节，"
                    "所以「字节数」最大（约 1.71 倍）。按字符限额选它，按字节限额别选。"),
            ("base91", "1.231 字符/字节 —— 纯 ASCII 里最紧凑，字母表已剔除 - ' \\ 等易被转义或折断的符号。"),
            ("base85", "1.250 字符/字节 —— 略逊于 base91，且含 \" ' \\，部分渠道粘贴会被转义。"),
            ("base64", "1.334 字符/字节 —— 最通用，含 + / = 三个符号，某些渠道会把 + 变成空格。"),
            ("base32", "1.600 字符/字节 —— 纯大写字母+数字，最耐复制粘贴，代价是体积最大。"),
            ("hex", "2.000 字符/字节 —— 十六进制，最直观可审计，体积是 base91 的 1.6 倍。"),
            ("custom", "1.334 字符/字节 —— 用自备的 64 个可打印字符做码表（命令行 --alphabet 指定），可做轻度混淆。"),
        ],
    ),
    "compress": (
        "压缩器",
        "编码之前先压缩，压得越狠文本越短、速度越慢。顺序固定为「先压缩再加密」。",
        [
            ("auto", "推荐。先试 lzma，压不动就自动跳过 —— docx/xlsx/pptx 本身已是 ZIP，"
                     "再压没有收益，auto 不会让它越压越大。"),
            ("max", "lzma 试 4 组参数取体积最小的一组，最慢但压得最狠。纯文本、老格式 doc 收益明显。"),
            ("lzma", "标准 lzma，压缩率高，速度中等，是 auto 实际使用的算法。"),
            ("zlib", "速度快、兼容性最好，压缩率一般。"),
            ("bz2", "压缩率介于 zlib 和 lzma 之间，速度也居中。"),
            ("none", "完全不压。适合本身已压缩的内容（jpg / png / zip），省掉无用计算。"),
        ],
    ),
    "cipher": (
        "加密器",
        "压缩之后加密，还原时必须提供同一口令；内容被改动 1 个字符就会校验失败。",
        [
            ("none", "不加密。谁拿到文本都能还原，适合内部传阅或不敏感的文档。"),
            ("aes-256-gcm", "AES-256-GCM 加密 + scrypt 派生密钥，带认证标签：既防偷看也防篡改。"
                            "必须在下面填口令，口令丢了文件无法恢复。"),
        ],
    ),
    "password": (
        "口令",
        "加密用的密码，只在内存里使用，不保存、不上传、不写进产出的文本。",
        [
            ("", "选 aes-256-gcm 时必填；选 none 时留空即可。"
                 "口令区分大小写，建议用「一句话+数字」而不是「123456」。"),
        ],
    ),
    "outdir": (
        "输出目录",
        "产出的文件写到哪里。",
        [
            ("", "留空时：打包存到「源文件所在目录」；粘贴还原存到「桌面」。"),
            ("set", "点右侧「…」选文件夹，也可以直接把路径粘进输入框。处理完会弹窗给出完整路径，"
                    "并可一键打开。"),
        ],
    ),
    "split": (
        "分片输出",
        "把一份文本切成若干段，专给「每次只能输入固定字数」的渠道用。默认关闭。",
        [
            ("off", "不勾选：整份输出一个文件，最省事。"),
            ("on", "勾选后按「字/段」切分。每段自带段头（文档ID / 序号 / 总段数 / 校验）与段内字符数，"
                   "可乱序收集、段尾多粘文字也能自动截断；凑齐后一起还原，或直接粘进「粘贴文本还原」。"),
        ],
    ),
    "qr": (
        "输出二维码",
        "不生成文本，改为输出二维码图片，适合走扫码通道（AirScan-QR / 鹊桥 / 手机相机）传文件。",
        [
            ("off", "不勾选：按文本输出（.opk.txt）。"),
            ("on", "勾选后输出二维码：一张码装不下时自动拆成多帧，文件名带 001/002 序号。\n"
                   "单张码的上限是固定的（QR 40 版、纠错 L、qr45 编码器）——\n"
                   "压缩后约 2.4KB；超过就必然是多张，这是二维码标准的物理上限。"),
            ("PNG（单帧 + 总览）", "每帧一张 PNG，另附总览图（每页 12 帧，多页自动续）。\n"
                                  "总览图一张就能把整页帧全带回来，电脑上还原最快。"),
            ("GIF 动图", "把全部帧合成一张循环播放的动图，适合手机对着屏幕扫。\n"
                        "二维码只有黑白两色，GIF 对它是**完全无损**的，数据不会掉；\n"
                        "体积约等于同样分辨率的 PNG（黑白图没法靠帧间压缩再省）。\n"
                        "帧率别贪高：相机对焦+解码要 150ms 以上，2~4 帧/秒最稳。"),
            ("PNG + GIF", "两种都出。先拿总览图在电脑上还原；手机端用动图。" ),
            ("帧/秒", "动图播放速度。太快手机跟不上会更漏帧，太慢要等太久；3 最好用。"),
            ("back", "还原：点「还原二维码 → 框选屏幕区域」，拖框圈住码，松开就识别；\n"
                     "也可以读剪贴板截图、选图片文件，或直接把 PNG/GIF 拖进列表。\n"
                     "顺序无关：每帧都带序号和校验，扫到哪帧收哪帧。"),
        ],
    ),
}



def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%.1f GiB" % n


class App:
    def __init__(self, root: tk.Tk, has_dnd: bool):
        self.root = root
        self.has_dnd = has_dnd
        self.files: list[str] = []
        self.queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.last_out_dir: str | None = None   # 上一轮实际写出文件的目录
        self._log_lines = 6                    # 日志框行数，屏幕不够时会被自动压缩
        self._poll_id: str | None = None       # 队列轮询的 after id

        root.title(APP_TITLE)

        self._build_style()
        self._build_ui()
        # 布局建完才知道内容真实需要多大：不能写死 geometry，
        # 否则在 125%/150% 缩放的屏幕上底部按钮会被裁掉。
        self._fit_window()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_queue()

    # ---------------- UI ----------------
    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Go.TButton", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_ui(self):
        pad = {"padx": 10, "pady": 3}

        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=14, pady=(12, 4))
        ttk.Label(header, text="任意文件 ↔ 纯文本 无损装换", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Word / Excel / PPT / PDF / 压缩包 / 源码 / 图片……任意文件 → 文本编码载体（可加密）；"
                 "还原后与原文件 SHA-256 完全一致",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=10, pady=6)

        # 左：文件列表
        left = ttk.LabelFrame(body, text="文件（可拖拽或点击添加）")
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        cols = ("name", "size", "info")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("name", text="文件名")
        self.tree.heading("size", text="大小")
        self.tree.heading("info", text="说明")
        self.tree.column("name", width=300, anchor="w")
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("info", width=140, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")

        btns = ttk.Frame(left)
        btns.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(btns, text="添加文件", command=self.add_files).pack(side="left")
        ttk.Button(btns, text="移除选中", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(btns, text="清空", command=self.clear_files).pack(side="left")

        if self.has_dnd:
            try:
                self.tree.drop_target_register(DND_FILES)
                self.tree.dnd_bind("<<Drop>>", self._on_drop)
                left.configure(labelwidget=ttk.Label(left, text="文件（把文件拖到这里）"))
            except tk.TclError:
                # root 不是 TkinterDnD.Tk()，降级为按钮选择
                self.has_dnd = False

        # 右：选项。放进可滚动画布 —— 高缩放或小屏上窗口再矮，
        # 选项区也能滚动到底，不会像以前那样把底部按钮直接裁掉。
        holder = ttk.Frame(body)
        holder.pack(side="right", fill="y", padx=(8, 0))

        self.opt_canvas = tk.Canvas(holder, highlightthickness=0, borderwidth=0,
                                    width=232, height=560)
        self.opt_vsb = ttk.Scrollbar(holder, orient="vertical", command=self.opt_canvas.yview)
        self.opt_canvas.configure(yscrollcommand=self.opt_vsb.set)
        self.opt_canvas.pack(side="left", fill="both", expand=True)

        right = ttk.LabelFrame(self.opt_canvas, text="选项")
        self.opt_frame = right
        self._opt_window = self.opt_canvas.create_window((0, 0), window=right, anchor="nw")
        right.bind("<Configure>", lambda _e: self._sync_opt_scroll())
        self.opt_canvas.bind("<Configure>", self._on_opt_canvas_resize)
        self.opt_canvas.bind("<Enter>", lambda _e: self.opt_canvas.bind_all(
            "<MouseWheel>", self._on_opt_wheel))
        self.opt_canvas.bind("<Leave>", lambda _e: self.opt_canvas.unbind_all("<MouseWheel>"))

        self.mode = tk.StringVar(value="pack")
        ttk.Label(right, text="操作模式").grid(row=0, column=0, sticky="w", **pad)
        ttk.Radiobutton(right, text="打包（文件 → 文本）", variable=self.mode,
                        value="pack").grid(row=1, column=0, sticky="w", padx=10)
        ttk.Radiobutton(right, text="还原（文本 → 文件）", variable=self.mode,
                        value="unpack").grid(row=2, column=0, sticky="w", padx=10)

        self.preset = tk.StringVar(value="chars")
        self.codec = tk.StringVar(value=ct.PRESETS["chars"]["codec"])
        self.compress = tk.StringVar(value=ct.PRESETS["chars"]["compress"])
        self.cipher = tk.StringVar(value="none")
        self.password = tk.StringVar()
        self.show_pwd = tk.BooleanVar(value=False)

        self._param_label(right, 3, "预设", "preset")
        preset_box = ttk.Combobox(right, textvariable=self.preset, width=22, state="readonly",
                                  values=["qr（单张码装最多）", "chars（字数最少）",
                                          "compact（ASCII 体积最小）",
                                          "safe（纯大写+数字，耐复制）",
                                          "fast（速度快）", "plain（不压缩）", "自定义"])
        preset_box.grid(row=4, column=0, sticky="ew", padx=10)
        preset_box.set("chars（字数最少）")
        preset_box.bind("<<ComboboxSelected>>", self._on_preset)

        # 编码器 / 压缩器的下拉项都很短，并成一行两列，省下的高度留给日志
        self._param_pair(right, 5, [
            ("编码器", "codec", self._build_codec),
            ("压缩器", "compress", self._build_compress),
        ])
        self._param_pair(right, 6, [
            ("加密器", "cipher", self._build_cipher),
            ("口令", "password", self._build_password),
        ])

        self._param_label(right, 9, "输出目录", "outdir")
        self.out_dir = tk.StringVar(value="")
        out_row = ttk.Frame(right)
        out_row.grid(row=10, column=0, sticky="ew", padx=10)
        ttk.Entry(out_row, textvariable=self.out_dir, width=16).pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="…", width=3, command=self.pick_out_dir).pack(side="left", padx=(4, 0))

        self._param_label(right, 11, "分片输出", "split")
        self.split_enabled = tk.BooleanVar(value=False)
        seg_row = ttk.Frame(right)
        seg_row.grid(row=12, column=0, sticky="ew", padx=10)
        self.split_chk = ttk.Checkbutton(seg_row, text="启用", variable=self.split_enabled,
                                         command=self._on_split_toggle)
        self.split_chk.pack(side="left")
        self.segment = tk.StringVar(value=str(ct.DEFAULT_SEGMENT_CHARS))
        self.seg_entry = ttk.Entry(seg_row, textvariable=self.segment, width=8, state="disabled")
        self.seg_entry.pack(side="left", padx=(6, 4))
        ttk.Label(seg_row, text="字/段").pack(side="left")

        # 二维码输出（与分片互斥：二维码有自己的分帧规则）
        self._param_label(right, 13, "输出二维码", "qr")
        self.qr_enabled = tk.BooleanVar(value=False)
        self.qr_format = tk.StringVar(value="PNG（单帧 + 总览）")
        self.qr_fps = tk.StringVar(value="3")
        qr_row = ttk.Frame(right)
        qr_row.grid(row=14, column=0, sticky="ew", padx=10)
        self.qr_chk = ttk.Checkbutton(qr_row, text="生成", variable=self.qr_enabled,
                                      command=self._on_qr_toggle)
        self.qr_chk.pack(side="left")
        self.qr_fmt_box = ttk.Combobox(qr_row, textvariable=self.qr_format, width=17,
                                       state="readonly",
                                       values=["PNG（单帧 + 总览）", "PNG（仅总览）",
                                               "GIF 动图", "PNG + GIF"])
        self.qr_fmt_box.pack(side="left", padx=(8, 0))
        ttk.Entry(qr_row, textvariable=self.qr_fps, width=3).pack(side="left", padx=(6, 0))
        ttk.Label(qr_row, text="帧/秒", style="Hint.TLabel").pack(side="left", padx=(3, 0))

        self.go = ttk.Button(right, text="开始处理", style="Go.TButton", command=self.start)
        self.go.grid(row=15, column=0, sticky="ew", padx=10, pady=(12, 3))

        self.open_btn = ttk.Button(right, text="打开输出文件夹", command=self.open_out_dir)
        self.open_btn.grid(row=16, column=0, sticky="ew", padx=10, pady=(0, 3))

        self.paste_btn = ttk.Button(right, text="粘贴文本还原…", command=self.paste_unpack)
        self.paste_btn.grid(row=17, column=0, sticky="ew", padx=10, pady=(0, 3))

        self.shot_btn = ttk.Menubutton(right, text="还原二维码 ▾")
        shot_menu = tk.Menu(self.shot_btn, tearoff=False)
        shot_menu.add_command(label="粘贴识别面板…（贴图/连 Ditto，推荐）",
                              command=self.paste_unpack)
        shot_menu.add_command(label="框选屏幕区域…（支持多屏）", command=self.grab_region)
        shot_menu.add_command(label="读取剪贴板截图", command=self.qr_unpack)
        shot_menu.add_command(label="选择图片文件…（可多选）",
                              command=lambda: self.qr_unpack(from_file=True))
        self.shot_btn["menu"] = shot_menu
        self.shot_btn.grid(row=18, column=0, sticky="ew", padx=10, pady=(0, 3))

        self.help_btn = ttk.Button(right, text="全部参数说明…",
                                   command=lambda: self._show_help(None))
        self.help_btn.grid(row=19, column=0, sticky="ew", padx=10, pady=(0, 8))

        self._on_split_toggle()
        try:
            self.mode.trace_add("write", lambda *_: self._sync_mode())
        except AttributeError:  # Tk < 8.6
            self.mode.trace("w", lambda *_: self._sync_mode())

        # 底部：日志
        bottom = ttk.LabelFrame(self.root, text="日志")
        bottom.pack(fill="both", padx=10, pady=(4, 10))
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x", padx=8, pady=(6, 4))
        self.log = tk.Text(bottom, height=self._log_lines, wrap="none", font=("Consolas", 9),
                           bg="#fbfbfb", relief="flat", state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    # ---------------- 选项区滚动 ----------------
    def _on_opt_canvas_resize(self, event):
        self.opt_canvas.itemconfigure(self._opt_window, width=event.width)
        self._sync_opt_scroll()

    def _sync_opt_scroll(self):
        """内容比可视区高才挂滚动条，否则藏起来（平常看着和普通面板一样）。"""
        try:
            bbox = self.opt_canvas.bbox("all")
            if not bbox:
                return
            self.opt_canvas.configure(scrollregion=bbox)
            need = bbox[3] - bbox[1] > self.opt_canvas.winfo_height() + 1
            shown = bool(self.opt_vsb.winfo_ismapped())
            if need and not shown:
                self.opt_vsb.pack(side="right", fill="y")
            elif not need and shown:
                self.opt_vsb.pack_forget()
                self.opt_canvas.yview_moveto(0)
        except tk.TclError:
            pass

    def _on_opt_wheel(self, event):
        try:
            bbox = self.opt_canvas.bbox("all")
            if not bbox or bbox[3] - bbox[1] <= self.opt_canvas.winfo_height() + 1:
                return
            self.opt_canvas.yview_scroll(int(-event.delta / 120), "units")
        except tk.TclError:
            pass

    # ---------------- 窗口尺寸 ----------------
    def _work_area(self) -> tuple[int, int, int, int]:
        """屏幕可用工作区 (left, top, right, bottom)：扣掉任务栏。

        只减一个拍脑袋的常数，任务栏高一点就会把窗口底边压到任务栏下面。
        """
        try:
            import ctypes
            import ctypes.wintypes as wt

            rect = wt.RECT()
            # SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                if rect.right - rect.left > 200 and rect.bottom - rect.top > 200:
                    return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _size_dialog(self, dlg, want_w, want_h, min_w, min_h):
        """对话框同样不能写死像素：按屏幕可用范围收窄，并居中到主窗口。"""
        left, top, right, bottom = self._work_area()
        avail_w, avail_h = right - left, bottom - top
        w = min(want_w, max(min_w, avail_w - 40))
        h = min(want_h, max(min_h, avail_h - 60))
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - w) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - h) // 3)
        x = min(max(x, left), max(left, right - w))
        y = min(max(y, top), max(top, bottom - h))
        dlg.geometry("%dx%d+%d+%d" % (w, h, x, y))
        dlg.minsize(min(min_w, w), min(min_h, h))

    def _chrome_height(self) -> int:
        """窗口外框比客户区多出来的高度（标题栏 + 边框）。

        geometry 给的是客户区尺寸，标题栏是额外加上去的；
        不留出这部分，窗口外框就会压到任务栏上（实测 125% 缩放是 39px）。
        """
        try:
            import ctypes
            import ctypes.wintypes as wt

            u32 = ctypes.windll.user32
            hwnd = self.root.winfo_id()
            frame = u32.GetParent(hwnd) or hwnd
            rect = wt.RECT()
            if u32.GetWindowRect(frame, ctypes.byref(rect)):
                chrome = (rect.bottom - rect.top) - self.root.winfo_height()
                if 0 <= chrome < 200:
                    return chrome
        except Exception:
            pass
        return 40

    def _fit_window(self):
        """按内容实测尺寸决定窗口大小，而不是写死像素。

        Tk 的 px 是物理像素，而字体大小会随系统缩放（125%/150%）放大，
        所以同一个 "980x660" 在高缩放屏幕上装不下内容，底部按钮会被裁掉。
        这里先让 Tk 算出内容真正需要的尺寸，再钳制到工作区范围内（并扣掉标题栏）；
        真的放不下时优先牺牲最不关键的日志框高度，选项区还有滚动条兜底。
        """
        self.root.update_idletasks()
        # 画布先按选项区的真实尺寸撑开，窗口才能一次显示全；
        # 真要装不下时，再靠滚动条兜底（_sync_opt_scroll 里按需显示）。
        self.opt_canvas.configure(width=self.opt_frame.winfo_reqwidth(),
                                  height=self.opt_frame.winfo_reqheight())
        self.root.update_idletasks()

        left, top, right, bottom = self._work_area()
        avail_w, avail_h = right - left, bottom - top
        chrome = self._chrome_height()
        best = None

        # 跑两遍：第一遍之后才知道标题栏真实高度，第二遍用实测值重算
        for _ in range(2):
            limit_h = max(480, avail_h - chrome - 10)
            req_h = self.root.winfo_reqheight()
            # 日志框从 6 行往下退，退到 2 行还不够就交给滚动条
            while req_h > limit_h and self._log_lines > 2:
                self._log_lines -= 1
                self.log.configure(height=self._log_lines)
                self.root.update_idletasks()
                req_h = self.root.winfo_reqheight()

            w = min(max(self.root.winfo_reqwidth(), 880), max(720, avail_w - 20))
            h = min(req_h, limit_h)
            x = left + max(0, (avail_w - w) // 2)
            y = top + max(0, (avail_h - chrome - h) // 2)
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
            # 下限就用算出来的尺寸：再缩小一定会切掉控件
            self.root.minsize(w, h)
            self.root.update_idletasks()

            measured = self._chrome_height()
            if best is None or measured == chrome:
                best = (w, h, x, y)
            if abs(measured - chrome) <= 2:
                break
            chrome = measured

        if best:
            w, h, x, y = best
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
            self.root.minsize(w, h)
            self.root.update_idletasks()
        self._sync_opt_scroll()

    # ---------------- 文件管理 ----------------
    def _on_drop(self, event):
        paths = self.root.tk.splitlist(event.data)
        self._add(list(paths))

    def add_files(self):
        """按当前模式给不同分组：打包什么都收，还原只收容器/图片。"""
        if self.mode.get() == "pack":
            everything = OFFICE_EXTS + DOC_EXTS + PKG_EXTS + MEDIA_EXTS
            groups = [
                ("全部支持的文件", " ".join("*" + e for e in everything)),
                ("Office 文档", " ".join("*" + e for e in OFFICE_EXTS)),
                ("文本 / 源码 / PDF", " ".join("*" + e for e in DOC_EXTS)),
                ("压缩包", " ".join("*" + e for e in PKG_EXTS)),
                ("图片 / 音视频", " ".join("*" + e for e in MEDIA_EXTS)),
                ("所有文件（任意扩展名都能打包）", "*.*"),
            ]
        else:
            groups = [
                ("文本容器 / 二维码图片", " ".join("*" + e for e in TEXT_EXTS)),
                ("所有文件", "*.*"),
            ]
        paths = filedialog.askopenfilenames(title="选择文件", filetypes=groups)
        if paths:
            self._add(list(paths))

    def _add(self, paths: list[str]):
        for path in paths:
            if not os.path.isfile(path):
                continue
            if path in self.files:
                continue
            self.files.append(path)
            self._refresh_row(path)

    def _refresh_row(self, path: str):
        size = os.path.getsize(path)
        ext = os.path.splitext(path)[1].lower()
        if ext in qr.IMAGE_EXTS:
            info = "图片 / 二维码"
        elif ext in TEXT_EXTS:
            info = "文本容器"
        else:
            info = preview_mod.kind_of(ext)
        if self.tree.exists(path):
            self.tree.item(path, values=(os.path.basename(path), _human(size), info))
        else:
            self.tree.insert("", "end", iid=path, values=(os.path.basename(path), _human(size), info))

    def remove_selected(self):
        for iid in self.tree.selection():
            if iid in self.files:
                self.files.remove(iid)
            self.tree.delete(iid)

    def clear_files(self):
        self.files.clear()
        for iid in self.tree.get_children():
            self.tree.delete(iid)

    def pick_out_dir(self) -> str | None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.out_dir.set(path)
            return path
        return None

    def open_out_dir(self):
        """打开输出文件夹：优先用上一轮实际产出的目录。"""
        path = self.last_out_dir or self.out_dir.get().strip() or None
        if not path or not os.path.isdir(path):
            path = self.pick_out_dir()
            if not path:
                return
        try:
            os.startfile(path)  # noqa: S606 - 交给资源管理器打开
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, "无法打开文件夹：\n%s\n\n%s" % (path, exc))

    def paste_unpack(self):
        """粘贴识别面板：贴图（可多张、可连 Ditto）、贴文本，累积后一次还原。

        为什么是「面板」而不是「读一次剪贴板就还原」：多帧二维码常常分散在好几张
        截图里，而剪贴板一次只拿得到最上面那张。面板把每次识别到的帧累积起来，
        最后一次性还原 —— 顺序完全无所谓（分段自带序号，缺帧会明确告诉你缺第几帧）。
        """
        clip = clip_mod

        dlg = tk.Toplevel(self.root)
        dlg.title("粘贴识别面板")
        self._size_dialog(dlg, 880, 620, 660, 460)
        dlg.transient(self.root)
        try:
            dlg.grab_set()
        except tk.TclError:
            pass

        ttk.Label(
            dlg, justify="left", wraplength=820,
            text="① 复制一张二维码截图 → 在下面 Ctrl+V，可反复多次（帧会累积）\n"
                 "② 装了 Ditto：点「打开 Ditto 剪贴板」，在 Ditto 里双击要用的图，会自动贴进来\n"
                 "③ 也可以直接把容器文本 / 分片粘进来（顺序随意）",
        ).pack(anchor="w", padx=12, pady=(12, 4))

        wrap = ttk.Frame(dlg)
        wrap.pack(fill="both", expand=True, padx=12)
        box = tk.Text(wrap, wrap="none", font=("Consolas", 9), undo=True)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        box.pack(side="left", fill="both", expand=True)

        hint = ttk.Label(dlg, text="", justify="left", wraplength=820)
        hint.pack(anchor="w", padx=12, pady=(6, 0))

        bar = ttk.Frame(dlg)
        bar.pack(fill="x", padx=12, pady=10)

        known: set[str] = set()      # 已收帧，避免同一张图贴两次重复堆积

        def append_text(chunk: str) -> None:
            if box.get("1.0", "end-1c").strip():
                box.insert("end", "\n")
            box.insert("end", chunk)
            box.see("end")

        def refresh(*_args):
            """统计面板里现在攒了什么：共几帧、缺哪几帧。"""
            text = box.get("1.0", "end-1c")
            if not text.strip():
                hint.configure(text="", foreground="#666")
                return
            try:
                segs = ct.parse_segments(text)
            except Exception:  # noqa: BLE001
                segs = []
            if not segs:
                try:
                    info = ct.inspect_text(text)
                except ct.ContainerError:
                    hint.configure(text="暂时看不出是容器文本，点「还原」试试", foreground="#666")
                    return
                hint.configure(text="完整容器：%s（%s）"
                                    % (info.get("name"), _human(info.get("size") or 0)),
                               foreground="#0a7")
                return
            need, got, parts, complete = {}, {}, [], True
            for seg in segs:
                need[seg.doc_id] = seg.total
                got.setdefault(seg.doc_id, set()).add(seg.index)
            for doc_id, total in need.items():
                have = len(got.get(doc_id, ()))
                missing = [i for i in range(1, total + 1) if i not in got.get(doc_id, ())]
                complete = complete and not missing
                parts.append("共 %d 帧，已收 %d 帧%s"
                             % (total, have,
                                "" if not missing
                                else "，缺第 %s 帧" % "、".join(str(i) for i in missing)))
            hint.configure(
                text="；".join(parts) + ("　✔ 可以还原" if complete else "　继续贴图补帧"),
                foreground="#0a7" if complete else "#c60")

        def absorb(images, label: str) -> int:
            """识别一批图，把新帧追加进面板；返回新增帧数。"""
            added = 0
            deep = len(images) == 1
            for idx, img in enumerate(images, 1):
                try:
                    found = qr.decode_image(img, deep=deep)
                except Exception as exc:  # noqa: BLE001  单张坏了不影响其它
                    self._log("✗ %s 第 %d 张识别失败：%s" % (label, idx, exc))
                    continue
                fresh = [t for t in found if t not in known]
                if fresh:
                    known.update(fresh)
                    append_text("\n".join(fresh))
                    added += len(fresh)
                if len(images) > 1:
                    self._log("· %s第 %d 张：识别 %d 段，新增 %d 段"
                              % (label, idx, len(found), len(fresh)))
            return added

        def take_paths(paths) -> int:
            from PIL import Image

            images = []
            for path in paths:
                try:
                    with Image.open(path) as im:
                        images.append(im.convert("RGB"))
                except Exception as exc:  # noqa: BLE001
                    self._log("✗ 打不开 %s：%s" % (os.path.basename(path), exc))
            if not images:
                hint.configure(text="这些文件都打不开", foreground="#c00")
                return 0
            added = absorb(images, "%d 张图片" % len(images))
            refresh()
            return added

        def take_clipboard(verbose: bool = True) -> int:
            """读剪贴板：优先图，其次文件，最后才当文本。"""
            snap = clip.snapshot()
            images = list(snap["images"])
            paths = [p for p in snap["files"] if qr.is_image_path(p)]
            added = 0
            if images:
                added += absorb(images, "剪贴板截图")
            if paths:
                added += take_paths(paths)
            if not images and not paths:
                if (snap["text"] or "").strip():
                    append_text(snap["text"].strip())
                    added += 1
                elif verbose:
                    names = "、".join(snap["formats"][:6]) or "空"
                    hint.configure(
                        text="剪贴板里没有图（当前格式：%s）。"
                             "用 Win+Shift+S 或 Ditto 重新复制一张再试" % names,
                        foreground="#c60")
            refresh()
            return added

        def on_paste(_event=None):
            """Ctrl+V：剪贴板里是图就自己消化，是文本就交给默认粘贴。"""
            try:
                snap = clip.snapshot()
            except Exception:  # noqa: BLE001
                return None
            if snap["images"] or [p for p in snap["files"] if qr.is_image_path(p)]:
                take_clipboard()
                return "break"
            return None

        box.bind("<<Paste>>", on_paste)

        def pick_files():
            chosen = filedialog.askopenfilenames(
                parent=dlg, title="选择二维码截图（可多选，顺序随意）",
                filetypes=[("图片", " ".join("*" + e for e in qr.IMAGE_EXTS)),
                           ("所有文件", "*.*")],
            )
            if chosen:
                take_paths(list(chosen))

        def open_ditto():
            ok, msg = clip.open_ditto()
            hint.configure(text=msg, foreground="#0a7" if ok else "#c60")
            self._log(("· " if ok else "✗ ") + msg)

        def clear_all():
            box.delete("1.0", "end")
            known.clear()
            hint.configure(text="", foreground="#666")

        def do_unpack():
            text = box.get("1.0", "end-1c")
            if not text.strip():
                messagebox.showwarning("提示", "面板是空的：先贴一张二维码截图",
                                       parent=dlg)
                return
            password = self.password.get() or None
            if password is None:
                try:
                    cipher = ct.inspect_text(text).get("cipher") or {}
                except ct.ContainerError:
                    cipher = {}
                if cipher.get("alg", "none") != "none":
                    messagebox.showwarning(
                        "需要口令", "这份内容是加密的，请先在主界面的「口令」里填写",
                        parent=dlg)
                    return
            try:
                results = self._unpack_frames([text], "粘贴面板")
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("还原失败", str(exc), parent=dlg)
                return
            dlg.destroy()
            self._finish({"ok": len(results), "fail": 0,
                          "outputs": [p for p, _ in results], "errors": []})

        if not clip.ditto_path():
            self._log("· 没检测到 Ditto，用 Win+Shift+S 截图后 Ctrl+V 也一样")

        ttk.Button(bar, text="读剪贴板", command=take_clipboard).pack(side="left")
        ttk.Button(bar, text="打开 Ditto 剪贴板", command=open_ditto).pack(
            side="left", padx=6)
        ttk.Button(bar, text="选图片文件…", command=pick_files).pack(side="left")
        ttk.Button(bar, text="清空", command=clear_all).pack(side="left", padx=6)
        ttk.Button(bar, text="关闭", command=dlg.destroy).pack(side="right")
        ttk.Button(bar, text="还原", style="Go.TButton",
                   command=do_unpack).pack(side="right", padx=6)

        box.focus_set()
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        refresh()

    def _cursor_monitor(self) -> tuple[int, int, int, int]:
        """鼠标所在显示器的矩形（虚拟桌面坐标；主屏左侧的副屏会是负坐标）。

        Tk 的 winfo_screenwidth/height 只给主屏尺寸，拿它铺遮罩的话副屏就成了
        「点不到、也框不了」的死角。这里直接问系统要鼠标踩在哪块屏上。
        """
        fallback = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        try:
            import ctypes
            import ctypes.wintypes as wt

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                            ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

            point = wt.POINT()
            if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                return fallback
            handle = ctypes.windll.user32.MonitorFromPoint(point, 0)   # NEAREST
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if handle and ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                rect = info.rcMonitor
                if rect.right - rect.left > 100 and rect.bottom - rect.top > 100:
                    return rect.left, rect.top, rect.right, rect.bottom
        except Exception:  # noqa: BLE001  非 Windows / 老系统
            pass
        return fallback

    @staticmethod
    def _place_overlay(top, left: int, top_y: int, width: int, height: int) -> None:
        """把遮罩窗口精确摆到指定屏幕（支持负坐标，Tk 的 geometry 不一定认）。"""
        try:
            import ctypes
            import ctypes.wintypes as wt

            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            wt.UINT]
            hwnd = top.winfo_id()
            frame = user32.GetParent(hwnd) or hwnd
            # HWND_TOPMOST = -1, SWP_SHOWWINDOW = 0x0040
            if user32.SetWindowPos(frame, wt.HWND(-1), left, top_y,
                                   width, height, 0x0040):
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            top.geometry("%dx%d+%d+%d" % (width, height, left, top_y))
        except tk.TclError:
            pass

    def grab_region(self):
        """框选屏幕区域 → 截图 → 识别二维码 → 直接还原。

        比「先截图存文件再选文件」少两步：屏幕上有码，拖一个框就完事。
        **多屏**：遮罩铺在鼠标所在的那块屏幕上（主屏/副屏都行），
        抓图按虚拟桌面坐标取，副屏在左边导致的负坐标也照常工作。
        """
        try:
            from PIL import ImageGrab
        except Exception as exc:  # 没有 Pillow
            messagebox.showerror(APP_TITLE, "缺少 Pillow，无法截图：%s" % exc)
            return

        ml, mt, mr, mb = self._cursor_monitor()
        sw, sh = mr - ml, mb - mt

        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        try:
            top.attributes("-alpha", 0.30)      # 半透明遮罩，能看见底下的码
        except tk.TclError:
            pass
        top.geometry("%dx%d+0+0" % (sw, sh))
        top.configure(bg="black")

        canvas = tk.Canvas(top, bg="black", highlightthickness=0, cursor="cross")
        canvas.pack(fill="both", expand=True)
        canvas.create_text(sw // 2, 40, fill="white", font=("Microsoft YaHei UI", 14),
                           text="拖框圈住二维码 · 松开即识别 · Esc 取消")
        top.update_idletasks()
        self._place_overlay(top, ml, mt, sw, sh)

        box = {"x0": 0, "y0": 0, "rect": None}

        def on_press(e):
            box["x0"], box["y0"] = e.x, e.y
            box["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                  outline="#ff3b30", width=2)

        def on_drag(e):
            if box["rect"] is not None:
                canvas.coords(box["rect"], box["x0"], box["y0"], e.x, e.y)

        def on_release(e):
            x1, x2 = sorted((box["x0"], e.x))
            y1, y2 = sorted((box["y0"], e.y))
            top.destroy()
            if x2 - x1 < 24 or y2 - y1 < 24:      # 误点一下，当取消
                return
            self.root.configure(cursor="watch")
            self.root.update_idletasks()
            try:
                # 遮罩的客户区原点 = 该屏左上角，加上屏幕偏移就是虚拟桌面坐标
                img = ImageGrab.grab(bbox=(ml + x1, mt + y1, ml + x2, mt + y2),
                                     all_screens=True)
                self._unpack_qr_image(img, "屏幕框选")
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(APP_TITLE, "框选识别失败：\n%s" % exc)
                self._log("✗ 框选识别失败：%s" % exc)
            finally:
                self.root.configure(cursor="")
                self.root.update_idletasks()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        top.bind("<Escape>", lambda _e: top.destroy())
        top.focus_force()

    def _missing_frames(self, texts) -> str:
        """按段头算还缺哪些帧，返回一句人话（齐了就返回空串）。"""
        try:
            segs = ct.parse_segments("\n".join(texts))
        except Exception:  # noqa: BLE001
            return ""
        if not segs:
            return ""
        need, got = {}, {}
        for seg in segs:
            need[seg.doc_id] = seg.total
            got.setdefault(seg.doc_id, set()).add(seg.index)
        notes = []
        for doc_id, total in need.items():
            missing = [i for i in range(1, total + 1) if i not in got.get(doc_id, set())]
            if missing:
                notes.append("共 %d 帧，已收 %d，缺第 %s 帧"
                             % (total, total - len(missing),
                                "、".join(str(i) for i in missing)))
        return "；".join(notes)

    def _unpack_qr_image(self, images, source: str):
        """把一张（或一批）PIL 图里的二维码全部识别出来并还原。"""
        if not isinstance(images, (list, tuple)):
            images = [images]
        texts: list[str] = []
        seen: set[str] = set()
        deep = len(images) == 1       # 只有一张图时才值得深挖
        for idx, img in enumerate(images, 1):
            try:
                found = qr.decode_image(img, deep=deep)
            except Exception as exc:  # noqa: BLE001  一张坏了不该拖垮整批
                self._log("✗ 第 %d 张识别失败：%s" % (idx, exc))
                continue
            fresh = [t for t in found if t not in seen]
            seen.update(fresh)
            texts.extend(fresh)
            if len(images) > 1:
                self._log("· 第 %d/%d 张：识别 %d 段，其中新增 %d 段"
                          % (idx, len(images), len(found), len(fresh)))
        if not texts:
            raise qr.QRError("这块区域里没有识别到完整的二维码。\n"
                             "请把码整个圈进去，别切掉边角，也别缩得太小。")
        results = self._unpack_frames(texts, source)
        if messagebox.askyesno(
            APP_TITLE,
            "已从%s还原 %d 个文件\n\n%s\n\n打开所在文件夹？"
            % (source, len(results), self.last_out_dir),
        ):
            self.open_out_dir()

    def _unpack_frames(self, texts, source: str, parent=None):
        """帧文本 → 文件（顺序无关，缺帧会提示），返回 [(路径, meta)]。"""
        missing = self._missing_frames(texts)
        if missing:
            self._log("· %s：%s" % (source, missing))
            self._log("   补帧办法：把二维码放大显示（100% 或全屏）再截一张，"
                      "或用「粘贴识别面板」一张张累加，顺序随意")
        password = self.password.get() or None
        out_dir = self.out_dir.get().strip() or None
        results = ct.unpack_texts(texts, out_dir, password)
        self.last_out_dir = os.path.dirname(results[0][0])
        names = "、".join(os.path.basename(p) for p, _ in results[:5])
        self._log("✅ 从%s识别 %d 帧 → 还原 %d 个文件：%s"
                  % (source, len(texts), len(results), names))
        for path, meta in results:
            self._log("   %s  %s，SHA-256 %s…"
                      % (os.path.basename(path), _human(meta["size"]),
                         meta["sha256"][:16]))
        return results

    def qr_unpack(self, from_file: bool = False):
        """从截图还原：优先读剪贴板里的截图（含 Ditto 放进去的 CF_DIB），
        取不到再让人选图片文件。多图会**全部**识别后合并，不是只认最后一张。
        """
        clip = clip_mod

        source = ""
        images: list = []
        chosen: list[str] | None = None
        if not from_file:
            snap = clip.snapshot()
            images = list(snap["images"])
            file_paths = [p for p in snap["files"] if qr.is_image_path(p)]
            if images:
                source = "剪贴板截图"
            elif file_paths:
                chosen = file_paths
            else:
                names = "、".join(snap["formats"][:6]) or "空"
                self._log("· 剪贴板里没有图（当前格式：%s）" % names)

        if not images:
            if chosen is None:
                chosen = filedialog.askopenfilenames(
                    title="选择二维码截图（可多选，顺序随意）",
                    filetypes=[("图片", " ".join("*" + e for e in qr.IMAGE_EXTS)),
                               ("所有文件", "*.*")],
                )
            paths = list(chosen or ())
            if not paths:
                return
            from PIL import Image

            for path in paths:
                try:
                    with Image.open(path) as im:
                        images.append(im.convert("RGB"))
                except Exception as exc:  # noqa: BLE001
                    self._log("✗ 打不开 %s：%s" % (os.path.basename(path), exc))
            source = "%d 张图片" % len(paths)

        if not images:
            messagebox.showwarning(APP_TITLE,
                                   "没读到任何图片。可以试试「粘贴识别面板」，"
                                   "它能把多次截图累积到一起还原。")
            return

        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            self._unpack_qr_image(images, source)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, "截图还原失败：\n%s" % exc)
            self._log("✗ 截图还原失败：%s" % exc)
        finally:
            self.root.configure(cursor="")
            self.root.update_idletasks()

    def _on_preset(self, _event=None):
        label = self.preset.get()
        key = label.split("（")[0].strip()
        if key in ct.PRESETS:
            cfg = ct.PRESETS[key]
            self.codec.set(cfg["codec"])
            self.compress.set(cfg["compress"])

    def _on_split_toggle(self):
        """只有勾选「分片」时才允许填每段字符数。"""
        enabled = self.split_enabled.get() and self.mode.get() == "pack"
        try:
            self.seg_entry.configure(state="normal" if enabled else "disabled")
        except tk.TclError:
            pass

    def _on_qr_toggle(self):
        """二维码与分片互斥（二维码自带分帧）；编码器不锁，随便选。"""
        pack = self.mode.get() == "pack"
        on = self.qr_enabled.get() and pack
        # 分片开关让位（还原模式下本来就不可用）
        try:
            self.split_chk.configure(state="normal" if (pack and not on) else "disabled")
        except tk.TclError:
            pass
        if on:
            self.split_enabled.set(False)
        self._on_split_toggle()
        self._qr_hint()

    def _qr_hint(self):
        """勾了二维码时，在日志区提示当前编码器在二维码里多费多少空间。"""
        if not (self.qr_enabled.get() and self.mode.get() == "pack"):
            return
        name = (self.codec.get() or "").lower()
        ratio = qr.overhead_ratio(name)
        if ratio <= 1.0001:
            return
        self._log(
            "· 二维码通道：编码器 %s 不落在字母数字模式，每帧约少装 %.0f%% 的内容"
            "（多出约 %.1f 倍帧数）。想少几张码就选预设 qr（qr45 + max）。"
            % (name or "?", (1 - 1 / ratio) * 100, ratio))

    def _sync_mode(self):
        """切换打包/还原时联动分片、二维码开关的可用状态。"""
        pack = self.mode.get() == "pack"
        try:
            self.split_chk.configure(state="normal" if pack else "disabled")
            self.qr_chk.configure(state="normal" if pack else "disabled")
        except tk.TclError:
            pass
        if not pack:
            self.qr_enabled.set(False)
        self._on_split_toggle()
        self._on_qr_toggle()

    # ---------------- 参数说明 ----------------
    def _param_pair(self, parent, row, items):
        """一行两列的参数组：短下拉项并排，省下的高度留给日志。

        每项 = (标题, 帮助key, 构建函数)；构建函数负责把控件塞进自己的小格子里。
        """
        box = ttk.Frame(parent)
        box.grid(row=row, column=0, sticky="ew", padx=10, pady=(2, 4))
        box.columnconfigure(0, weight=1, uniform="pair")
        box.columnconfigure(1, weight=1, uniform="pair")
        for idx, (text, key, build) in enumerate(items):
            cell = ttk.Frame(box)
            cell.grid(row=0, column=idx, sticky="ew",
                      padx=(0, 5) if idx == 0 else (5, 0))
            head = ttk.Frame(cell)
            head.pack(fill="x")
            ttk.Label(head, text=text).pack(side="left")
            mark = ttk.Label(head, text="?", foreground="#0a58ca", cursor="hand2")
            mark.pack(side="left", padx=(3, 0))
            mark.bind("<Button-1>", lambda _e, k=key: self._show_help(k))
            build(cell)
        return box

    def _build_codec(self, cell):
        self.codec_box = ttk.Combobox(cell, textvariable=self.codec, width=9,
                                      state="readonly", values=list(codec_mod.CODECS))
        self.codec_box.pack(fill="x")

    def _build_compress(self, cell):
        self.compress_box = ttk.Combobox(cell, textvariable=self.compress, width=9,
                                         state="readonly",
                                         values=["auto", "lzma", "zlib", "bz2", "none"])
        self.compress_box.pack(fill="x")

    def _build_cipher(self, cell):
        self.cipher_box = ttk.Combobox(cell, textvariable=self.cipher, width=9,
                                       state="readonly",
                                       values=["none", "aes-256-gcm"])
        self.cipher_box.pack(fill="x")

    def _build_password(self, cell):
        row = ttk.Frame(cell)
        row.pack(fill="x")
        self.pwd_entry = ttk.Entry(row, textvariable=self.password, width=6, show="●")
        self.pwd_entry.pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(row, text="显示", variable=self.show_pwd,
                        command=lambda: self.pwd_entry.configure(
                            show="" if self.show_pwd.get() else "●")
                        ).pack(side="left", padx=(4, 0))

    def _param_label(self, parent, row, text, key):
        """参数名 + 可点击的 ? 说明入口。"""
        box = ttk.Frame(parent)
        box.grid(row=row, column=0, sticky="w", padx=10, pady=2)
        ttk.Label(box, text=text).pack(side="left")
        mark = ttk.Label(box, text="?", foreground="#0a58ca", cursor="hand2")
        mark.pack(side="left", padx=(4, 0))
        mark.bind("<Button-1>", lambda _e, k=key: self._show_help(k))
        return box

    def _current_value(self, key: str) -> str:
        if key == "preset":
            return self.preset.get().split("（")[0].strip()
        if key == "codec":
            return self.codec.get()
        if key == "compress":
            return self.compress.get()
        if key == "cipher":
            return self.cipher.get()
        if key == "outdir":
            return "set" if self.out_dir.get().strip() else ""
        if key == "split":
            return "on" if self.split_enabled.get() else "off"
        if key == "qr":
            return "on" if self.qr_enabled.get() else "off"
        return ""

    def _show_help(self, key: str | None = None):
        """弹出参数说明；key=None 时列出全部。当前选中的选项会高亮。"""
        dlg = tk.Toplevel(self.root)
        dlg.title("参数说明" if key is None else "参数说明 · " + HELP[key][0])
        self._size_dialog(dlg, 820, 600, 520, 360)
        dlg.transient(self.root)

        wrap = ttk.Frame(dlg)
        wrap.pack(fill="both", expand=True, padx=4, pady=(10, 4))
        txt = tk.Text(wrap, wrap="word", font=("Microsoft YaHei UI", 10),
                      padx=16, pady=12, relief="flat", bg="#fcfcfc")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        txt.tag_configure("title", font=("Microsoft YaHei UI", 12, "bold"), spacing3=4)
        txt.tag_configure("intro", foreground="#555", spacing3=10)
        txt.tag_configure("name", font=("Consolas", 10, "bold"), lmargin1=4, spacing1=7)
        txt.tag_configure("cur", font=("Consolas", 10, "bold"), foreground="#0a58ca",
                          lmargin1=4, spacing1=7)
        txt.tag_configure("desc", foreground="#333", lmargin1=24, lmargin2=24, spacing3=4)

        for k in ([key] if key else list(HELP)):
            title, intro, items = HELP[k]
            txt.insert("end", title + "\n", "title")
            txt.insert("end", intro + "\n", "intro")
            current = self._current_value(k)
            for name, desc in items:
                hit = bool(current) and bool(name) and (
                    name == current or current.startswith(name))
                txt.insert("end", ("▶ " if hit else "  ") + (name or "（留空）") + "\n",
                           "cur" if hit else "name")
                txt.insert("end", desc + "\n", "desc")
            txt.insert("end", "\n")

        txt.configure(state="disabled")

        bar = ttk.Frame(dlg)
        bar.pack(fill="x", padx=16, pady=(0, 12))
        ttk.Label(bar, text="▶ = 当前选中", foreground="#0a58ca").pack(side="left")
        ttk.Button(bar, text="关闭", command=dlg.destroy).pack(side="right")

        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.focus_set()

    # ---------------- 执行 ----------------
    def _log(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.files:
            messagebox.showinfo(APP_TITLE, "请先添加文件")
            return
        mode = self.mode.get()
        password = self.password.get() or None
        cipher = self.cipher.get()
        if cipher != "none" and not password:
            messagebox.showwarning(APP_TITLE, "已选择加密器，请填写口令")
            return
        out_dir = self.out_dir.get().strip() or None
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        segment = 0
        if mode == "pack" and self.split_enabled.get():
            seg_text = self.segment.get().strip()
            segment = (int(seg_text) if seg_text.isdigit() and int(seg_text) > 0
                       else ct.DEFAULT_SEGMENT_CHARS)

        self.go.configure(state="disabled", text="处理中…")
        self.progress.configure(maximum=len(self.files), value=0)
        self._log("── 开始%s，共 %d 个文件 ──" % ("打包" if mode == "pack" else "还原", len(self.files)))
        files = list(self.files)
        self.worker = threading.Thread(
            target=self._run,
            args=(mode, files, out_dir, password, cipher,
                  self.codec.get(), self.compress.get(), segment,
                  self.qr_enabled.get() and mode == "pack",
                  self.qr_format.get(), self.qr_fps.get()),
            daemon=True,
        )
        self.worker.start()

    def _run(self, mode, files, out_dir, password, cipher, codec_name, compress_mode,
             segment, as_qr=False, qr_format="PNG（单帧 + 总览）", qr_fps="3"):
        ok = 0
        outputs: list[str] = []
        errors: list[str] = []

        if mode == "unpack":
            # 分段必须整体合并还原，不能逐个文件处理；图片和文本混着给也能一起还原
            try:
                results, stats = qr.unpack_any(files, out_dir, password)
                if stats["images"]:
                    self.queue.put(("log",
                        "· %d 张图片扫回 %d 段 + %d 个文本容器 → 合并还原"
                        % (stats["images"], stats["blocks"] - stats["texts"],
                           stats["texts"])))
                for path, meta in results:
                    outputs.append(path)
                    ok += 1
                    self.queue.put(("log",
                        "✅ 还原 → %s\n   %d 字节，SHA-256 校验通过 %s…" % (
                            path, meta["size"], meta["sha256"][:16])))
            except Exception as exc:
                errors.append("还原失败：%s" % exc)
                self.queue.put(("log", "✗ 还原失败：%s" % exc))
            self.queue.put(("tick", len(files)))
            self.queue.put(("done", {"ok": ok, "fail": len(files) - ok,
                                     "outputs": outputs, "errors": errors}))
            return

        for src in files:
            name = os.path.basename(src)
            try:
                if as_qr:
                    paths = self._pack_one_qr(src, name, out_dir, password, cipher,
                                              compress_mode, codec_name, outputs,
                                              qr_format, qr_fps)
                    ok += 1
                    continue

                out_path = None
                if out_dir:
                    out_path = os.path.join(
                        out_dir, os.path.basename(ct.default_out_path(src, codec_name))
                    )
                paths, meta = ct.pack_file(
                    src, out_path, codec_name=codec_name,
                    cipher_name=cipher, password=password,
                    compress=compress_mode, segment=max(segment, 0),
                )
                outputs.extend(paths)
                ok += 1
                text_size = sum(os.path.getsize(p) for p in paths)
                seg_note = "，分成 %d 段" % len(paths) if len(paths) > 1 else ""
                self.queue.put(("log",
                    "✅ %s  %s → 文本 %s / %d 字符（%s/%s/%s）%s\n   → %s" % (
                        name, _human(meta["size"]), _human(text_size),
                        meta["text_chars"],
                        meta["codec"], (meta.get("compress") or {}).get("alg", "none"),
                        (meta.get("cipher") or {}).get("alg", "none"), seg_note,
                        paths[0] if len(paths) == 1
                        else "%s … %s" % (paths[0], os.path.basename(paths[-1])))))
            except Exception as exc:
                errors.append("%s：%s" % (name, exc))
                self.queue.put(("log", "✗ %s：%s" % (name, exc)))
            self.queue.put(("tick", 1))
        self.queue.put(("done", {"ok": ok, "fail": len(files) - ok,
                                 "outputs": outputs, "errors": errors}))

    def _pack_one_qr(self, src, name, out_dir, password, cipher, compress_mode,
                     codec_name, outputs, qr_format="PNG（单帧 + 总览）", qr_fps="3"):
        """单个文件走二维码通道；返回写出的图片路径。编码器沿用界面选择。"""
        with open(src, "rb") as fh:
            raw = fh.read()
        pack = ct.pack_bytes(
            raw, name, codec_name=codec_name,
            cipher_name=cipher, password=password,
            compress=compress_mode, preview=False, ascii_only=True,
        )
        frames = qr.build_frames(pack, ec=qr.DEFAULT_EC)
        target_dir = out_dir or os.path.dirname(os.path.abspath(src))
        want_png = qr_format.startswith("PNG") or "+" in qr_format
        want_single = "仅总览" not in qr_format
        want_gif = "GIF" in qr_format and len(frames) > 1

        res = {"images": [], "sheet": None, "sheets": [], "dir": target_dir}
        if want_png:
            res = qr.render_images(frames, target_dir, name, ec=qr.DEFAULT_EC,
                                   sheet=True, single=want_single)
            outputs.extend(res["images"])
            outputs.extend(res.get("sheets") or [])

        gif_path = None
        if want_gif:
            try:
                fps = float(qr_fps)
            except (TypeError, ValueError):
                fps = 3.0
            gif_path = os.path.join(target_dir, "%s.qr.gif" % name)
            qr.render_gif(frames, gif_path, ec=qr.DEFAULT_EC, fps=fps)
            outputs.append(gif_path)

        meta = pack.meta
        room = qr.single_frame_bytes(qr.DEFAULT_EC, meta["codec"])
        sheets = res.get("sheets") or []
        note = ""
        if len(sheets) == 1:
            note = "，另附总览图 %s" % os.path.basename(sheets[0])
        elif len(sheets) > 1:
            note = "，另附 %d 页总览图（%s 起）" % (len(sheets), os.path.basename(sheets[0]))
        if gif_path:
            note += "，动图 %s" % os.path.basename(gif_path)
        self.queue.put(("log",
            "✅ %s  %s → 压缩后 %s → %d 张二维码（%s，纠错 %s）%s\n   → 目录 %s" % (
                name, _human(meta["size"]), _human(len(pack.payload)), len(frames),
                meta["codec"], qr.DEFAULT_EC.upper(), note, target_dir)))
        if len(frames) == 1:
            self.queue.put(("log",
                "   ✔ 一张码搞定（当前组合单张上限约 %s 压缩后数据）" % _human(room)))
        else:
            self.queue.put(("log",
                "   单张上限约 %s，压缩后 %s → 拆成 %d 张；总览图或逐张拖入都能还原，顺序无关"
                % (_human(room), _human(len(pack.payload)), len(frames))))
        if gif_path:
            self.queue.put(("log",
                "   动图可循环播放，手机对着屏幕录一段或边看边扫就行；"
                "把 gif 丢进「还原二维码 → 选择图片文件」也能直接读全帧"))
        if not want_png:
            self.queue.put(("log", "   （本次只输出动图，未写单帧 PNG）"))
        return res["images"]

    def _finish(self, info: dict):
        """一轮处理结束：日志汇总 + 明确的弹窗提示 + 输出位置。"""
        ok, fail = info["ok"], info["fail"]
        outputs, errors = info["outputs"], info["errors"]

        self.go.configure(state="normal", text="开始处理")
        self.progress.configure(value=0)
        self._log("── 完成：成功 %d / 失败 %d ──" % (ok, fail))

        if outputs:
            self.last_out_dir = os.path.dirname(outputs[0])
            self._log("输出文件夹：%s" % self.last_out_dir)
            for path in outputs[:6]:
                self._log("   · %s" % os.path.basename(path))
            if len(outputs) > 6:
                self._log("   · …共 %d 个文件" % len(outputs))
        else:
            self.last_out_dir = self.out_dir.get().strip() or None

        if fail:
            detail = "\n".join(errors[:8]) or "未知原因"
            if len(errors) > 8:
                detail += "\n…"
            messagebox.showerror(
                APP_TITLE,
                "处理结束：成功 %d 个，失败 %d 个\n\n失败原因：\n%s"
                % (ok, fail, detail),
            )
            return

        if not outputs:
            messagebox.showwarning(APP_TITLE, "没有生成任何文件，请检查输入。")
            return

        where = self.last_out_dir or "(原文件所在目录)"
        ask = "处理完成：成功 %d 个文件。\n\n输出位置：\n%s\n" % (ok, where)
        if len(outputs) > 1:
            ask += "（共 %d 个文件）\n" % len(outputs)
        ask += "\n是否打开该文件夹？"
        if messagebox.askyesno(APP_TITLE, ask):
            self.open_out_dir()

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "tick":
                    self.progress.step(payload)
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self._poll_id = self.root.after(120, self._poll_queue)

    def _on_close(self):
        """窗口关闭时取消挂起的 after，否则 Tk 会抛 invalid command name。"""
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        self.root.destroy()


def _enable_dpi_awareness() -> None:
    """让进程感知 DPI：不设的话，150% 缩放下框选屏幕的坐标和截图会错位。"""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:  # 非 Windows / 老系统
        pass


def main() -> int:
    _enable_dpi_awareness()
    if _HAS_DND:
        try:
            root = TkinterDnD.Tk()
            has_dnd = True
        except Exception:  # pragma: no cover
            root = tk.Tk()
            has_dnd = False
    else:
        root = tk.Tk()
        has_dnd = False
    App(root, has_dnd)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
