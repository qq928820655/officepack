"""图形界面：拖拽或选择文件，一键打包 / 还原。

依赖 tkinter（Python 自带）。若安装了 tkinterdnd2 则支持拖拽，否则退回按钮选择。
    pip install tkinterdnd2     # 可选，仅用于拖拽
"""

from __future__ import annotations

import os
import queue
import threading
import time
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
            ("GIF 动图", "把全部帧合成一张循环播放的动图，适合手机扫或电脑框选。\n"
                        "首尾各有一张标识帧：框选时「见首帧开始收、见尾帧收工」，\n"
                        "所以边播边扫也能确认一轮到底有没有收全。\n"
                        "二维码只有黑白两色，GIF 对它是**完全无损**的，数据不会掉；\n"
                        "帧率别贪高：相机对焦+解码要 150ms 以上，2~4 帧/秒最稳。"),
            ("PNG + GIF", "两种都出。先拿总览图在电脑上还原；手机端用动图。" ),
            ("GIF + WebP", "两种动图都出。WebP 是**无损**的，体积约为 GIF 的 1/5。\n"
                          "代价：能播动图 WebP 的软件比 GIF 少（Windows「照片」、\n"
                          "微信可能只显示第一帧）。先确认你的播放器能循环播放它再选这项。" ),
            ("WebP 动图", "**只出**一张无损动图 WebP，不出 PNG、不出 GIF。\n"
                         "体积最小（约为 GIF 的 1/5），但播放兼容性最差；\n"
                         "播放器只显示第一帧的话，扫码就只会拿到那一帧。" ),
            ("帧/秒", "动图播放速度。太快手机跟不上会更漏帧，太慢要等太久；3 最好用。"),
            ("back", "还原：点「还原二维码 → 框选屏幕区域」，拖框圈住码，松开就连拍识别；\n"
                     "动图请让它一直在框里循环播放 —— 同一块区域会连拍到你收齐为止，\n"
                     "漏的帧再框一次会自动续上（累计不会重复）。\n"
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
        self._scan_texts: list[str] = []       # 框选连拍的累计帧（多次框选可续上）
        self._scan_cancelled = False
        self._scan_active = False              # 扫描进行中：主窗口要一直藏着
        self._last_phys = None                 # 上次框选的物理坐标：再来一轮不用重框
        self._hud = None                       # 扫描期间的小悬浮条
        self._log_lines = 6                    # 日志框行数，屏幕不够时会被自动压缩
        self._poll_id: str | None = None       # 队列轮询的 after id
        # 当前文件内部的进度（第几张码 / 第几张图）。大文件出码是几百帧的活，
        # 只看文件级进度条会以为程序卡死了，所以另开一条细分进度 + 文字。
        self._bar_t0 = 0.0
        self._bar_tick = 0.0
        self._bar_logged = -1                  # 已经打过的百分比刻度（每 10% 一行）
        self._bar_last = (0, 1, "")

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
                                               "GIF 动图", "WebP 动图", "GIF + WebP",
                                               "PNG + GIF", "PNG + GIF + WebP"])
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
        self.progress.pack(fill="x", padx=8, pady=(6, 2))
        # 二级进度：**当前文件内部**走到哪了（渲染第几张码 / 识别第几张图）。
        # 一条码要出几百张时，文件级进度一动不动 —— 有了这条才看得出还在干活。
        sub = ttk.Frame(bottom)
        sub.pack(fill="x", padx=8, pady=(0, 4))
        self.sub_progress = ttk.Progressbar(sub, mode="determinate")
        self.sub_progress.pack(side="left", fill="x", expand=True)
        self.prog_text = ttk.Label(sub, text="", foreground="#0a58ca", width=34, anchor="e")
        self.prog_text.pack(side="left", padx=(8, 0))
        # 缺帧时出现的「继续框选」：明确告诉用户这是**接着收同一份文件**，
        # 而不是又开一次不知底细的框选（两次框选是同一份还是另一份，以前只能猜）。
        # 缺帧时出现的两个按钮：一个**还是那块区域**再来一轮（不用重新拖框，
        # 框歪了才会想重框），一个重新框。以前只有后者，补录几个帧也要重新拖一次，
        # 拖歪一点又是白扫一轮。
        self.replay_btn = ttk.Button(bottom, text="再来一轮（还是那块区域，不用重框）",
                                     command=self._replay_scan)
        self.rescan_btn = ttk.Button(bottom, text="重新框选（接着收同一份文件）",
                                     command=self.grab_region)
        self._rescan_shown = False
        self.log = tk.Text(bottom, height=self._log_lines, wrap="none", font=("Consolas", 9),
                           bg="#fbfbfb", relief="flat", state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _show_rescan(self, show: bool):
        """缺帧才露出补录按钮；不占常驻空间，也不会把底部按钮挤掉。"""
        if show == self._rescan_shown:
            return
        self._rescan_shown = show
        try:
            if show:
                if self._last_phys:
                    self.replay_btn.pack(fill="x", padx=8, pady=(0, 2),
                                         before=self.log)
                self.rescan_btn.pack(fill="x", padx=8, pady=(0, 4),
                                     before=self.log)
            else:
                self.replay_btn.pack_forget()
                self.rescan_btn.pack_forget()
        except tk.TclError:
            pass

    def _replay_scan(self):
        """拿上次那块区域再来一轮 —— 补录几十个帧，不用重新拖框。

        「漏了几帧 → 再框一次」这条路上，最多的摩擦是**重新拖框**：手一抖就
        框歪，于是又白扫一轮。同一块屏幕、同一段动图，区域根本没变，记住坐标
        直接重扫就行。
        """
        if not self._last_phys:
            return self.grab_region()
        try:
            from PIL import ImageGrab
        except Exception:  # noqa: BLE001
            return self.grab_region()
        bbox = self._last_phys
        self._show_rescan(False)
        self._log("· 再来一轮：还是上次那块区域 %d×%d @ (%d,%d)，不用重新框"
                  % (bbox[2] - bbox[0], bbox[3] - bbox[1], bbox[0], bbox[1]))
        self.root.withdraw()
        self.root.update_idletasks()
        time.sleep(0.05)
        try:
            self._run_scan(bbox, ImageGrab)
        except Exception as exc:  # noqa: BLE001
            self._restore_window()
            messagebox.showerror(APP_TITLE, "重扫失败：\n%s" % exc)

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

    @staticmethod
    def _monitors() -> list[tuple[int, int, int, int]]:
        """枚举所有显示器矩形（虚拟桌面坐标；主屏左侧的副屏是负坐标）。"""
        rects: list[tuple[int, int, int, int]] = []
        try:
            import ctypes
            import ctypes.wintypes as wt

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                            ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

            proto = ctypes.WINFUNCTYPE(ctypes.c_int, wt.HMONITOR, wt.HDC,
                                       ctypes.POINTER(wt.RECT), wt.LPARAM)

            def _cb(hmon, hdc, lprc, data):
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                    r = info.rcMonitor
                    if r.right - r.left > 100 and r.bottom - r.top > 100:
                        rects.append((r.left, r.top, r.right, r.bottom))
                return 1

            ctypes.windll.user32.EnumDisplayMonitors(None, None, proto(_cb), 0)
        except Exception:  # noqa: BLE001  非 Windows / 老系统
            pass
        return rects

    def _virtual_screen(self) -> tuple[int, int, int, int]:
        """整个虚拟桌面 (x, y, 宽, 高)：多屏时横跨所有显示器，左/上可能是负数。"""
        try:
            import ctypes

            u32 = ctypes.windll.user32
            x, y = u32.GetSystemMetrics(76), u32.GetSystemMetrics(77)   # SM_X/YVIRTUALSCREEN
            w, h = u32.GetSystemMetrics(78), u32.GetSystemMetrics(79)   # SM_CX/CYVIRTUALSCREEN
            if w > 100 and h > 100:
                return x, y, w, h
        except Exception:  # noqa: BLE001
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _build_overlay(self):
        """铺一层盖住**所有**显示器的半透明遮罩，返回 (窗口, 画布, x, y, 宽, 高)。

        关键是铺「整个虚拟桌面」而不是鼠标所在那一块：只铺一块时，另一块屏幕
        既不暗、点上去也没反应，双屏用户会以为功能坏了。铺满之后哪块屏都能框，
        甚至能拖一个横跨两块屏的框。
        """
        vx, vy, vw, vh = self._virtual_screen()
        mons = self._monitors() or [(vx, vy, vx + vw, vy + vh)]

        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        try:
            top.attributes("-alpha", 0.32)      # 半透明遮罩，能看见底下的码
        except tk.TclError:
            pass
        top.configure(bg="black")
        top.geometry("%dx%d+%d+%d" % (vw, vh, vx, vy))

        canvas = tk.Canvas(top, bg="black", highlightthickness=0, cursor="cross",
                           width=vw, height=vh)
        canvas.pack(fill="both", expand=True)
        # 每块屏幕顶部各放一条提示：双屏时不用来回找「提示在哪」
        font = ("Microsoft YaHei UI", 13)
        for ml, mt, mr, mb in mons:
            canvas.create_text((ml + mr) // 2 - vx, 46,
                               fill="#ffffff", font=font,
                               text="拖框圈住二维码 · 松开即识别 · Esc 或右键取消")
        top.update_idletasks()
        self._place_overlay(top, vx, vy, vw, vh)
        return top, canvas, vx, vy, vw, vh

    def grab_region(self):
        """框选屏幕区域 → 截图 → 识别二维码 → 直接还原。

        比「先截图存文件再选文件」少两步：屏幕上有码，拖一个框就完事。
        **多屏**：遮罩铺满整个虚拟桌面，主屏/副屏都能框，跨屏拖框也支持；
        抓图按虚拟桌面坐标取（副屏在左边导致的负坐标同样正确）。
        """
        try:
            from PIL import ImageGrab
        except Exception as exc:  # 没有 Pillow
            messagebox.showerror(APP_TITLE, "缺少 Pillow，无法截图：%s" % exc)
            return

        # 主窗口往往铺得很宽，留在屏幕上会正好压住要圈的码（用户实测：窗口太大，
        # 遮罩底下还是窗口自己）。框选期间先收起来，识别完再放回来。
        mons = self._monitors()
        vx0, vy0, vw0, vh0 = self._virtual_screen()
        if self._scan_texts:
            self._log("· 接着上次：已攒 %d 帧，这次框选会往上续（同一份文件才续得上）"
                      % len(self._scan_texts))
        self._log("· 准备框选：虚拟桌面 %d×%d @ (%d,%d)，%d 块屏 —— "
                  "主窗口已隐藏，识别完自动恢复"
                  % (vw0, vh0, vx0, vy0, len(mons) or 1))
        self.root.withdraw()
        self.root.update_idletasks()

        top, canvas, vx, vy, vw, vh = self._build_overlay()
        box = {"x0": 0, "y0": 0, "rect": None, "p0": None}

        def clamp_x(x):
            return max(0, min(int(x), vw))

        def clamp_y(y):
            return max(0, min(int(y), vh))

        def on_press(e):
            box["x0"], box["y0"] = clamp_x(e.x), clamp_y(e.y)
            box["p0"] = _cursor_pos()          # 按下点的物理坐标（截图用这套）
            box["rect"] = canvas.create_rectangle(box["x0"], box["y0"], box["x0"],
                                                  box["y0"], outline="#ff3b30", width=2)

        def on_drag(e):
            if box["rect"] is not None:
                canvas.coords(box["rect"], box["x0"], box["y0"],
                              clamp_x(e.x), clamp_y(e.y))

        def on_release(e):
            if box["rect"] is None:               # 右键取消时窗口已销毁
                return
            x1, x2 = sorted((box["x0"], clamp_x(e.x)))
            y1, y2 = sorted((box["y0"], clamp_y(e.y)))
            p0, p1 = box.get("p0"), _cursor_pos()
            box["rect"] = None
            top.destroy()
            if x2 - x1 < 24 or y2 - y1 < 24:      # 误点一下，当取消
                self._restore_window()
                return
            self.root.configure(cursor="watch")
            self.root.update()
            # 截图区域优先用**物理鼠标坐标**（跨屏不会错位）；拿不到才退回
            # 「遮罩原点 + 框内偏移」这套 Tk 坐标换算。
            if p0 and p1:
                bx1, bx2 = sorted((p0[0], p1[0]))
                by1, by2 = sorted((p0[1], p1[1]))
                if bx2 - bx1 < 24 or by2 - by1 < 24:      # 物理坐标异常，用 Tk 的
                    bx1, by1, bx2, by2 = vx + x1, vy + y1, vx + x2, vy + y2
            else:
                bx1, by1, bx2, by2 = vx + x1, vy + y1, vx + x2, vy + y2
            # 遮罩带 alpha，销毁后要等一帧才真正从屏幕上消失；抢着截图会把
            # 一层 30% 的灰也截进去，等于给自己加了一层水印。
            time.sleep(0.15)
            try:
                self._scan_region_unpack((bx1, by1, bx2, by2), ImageGrab)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(APP_TITLE, "框选识别失败：\n%s" % exc)
                self._log("✗ 框选识别失败：%s" % exc)
                self.root.configure(cursor="")

        def cancel(_e=None):
            """右键 / Esc 取消：关遮罩并把主窗口放回来。"""
            try:
                top.destroy()
            finally:
                self._restore_window()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        canvas.bind("<Button-3>", cancel)                      # 右键取消
        top.bind("<Escape>", cancel)
        top.focus_force()

    def _restore_window(self):
        """把主窗口放回来（框选期间会隐藏）。重复调用无害。"""
        self._scan_active = False
        self._drop_hud()
        try:
            if self.root.state() != "normal":
                self.root.deiconify()
            self.root.lift()
        except tk.TclError:
            pass

    # ---------------- 扫描期间的小悬浮条 ----------------

    def _make_hud(self, bbox):
        """扫描期间在框外角落放一条极小的状态条：进度 + 停止。

        整套扫描可能跑几分钟（978 帧的动图按 3 帧/秒播一轮就是 326 秒），
        这期间主窗口是藏起来的 —— 没有它就既看不到进度、也没法中止。
        位置自动躲开框选区域，免得它自己挡住要扫的码；实在躲不开就不显示。
        """
        try:
            vx, vy, vw, vh = self._virtual_screen()
            w, h = 340, 64
            spots = [(vx + 16, vy + 16), (vx + vw - w - 16, vy + 16),
                     (vx + 16, vy + vh - h - 16), (vx + vw - w - 16, vy + vh - h - 16)]
            place = None
            for sx, sy in spots:
                if sx + w <= bbox[0] or sx >= bbox[2] or sy + h <= bbox[1] or sy >= bbox[3]:
                    place = (sx, sy)
                    break
            if place is None:
                return None
            hud = tk.Toplevel(self.root)
            hud.overrideredirect(True)
            hud.attributes("-topmost", True)
            hud.geometry("%dx%d+%d+%d" % (w, h, place[0], place[1]))
            hud.configure(bg="#1f2430")
            tk.Label(hud, text="正在连拍收帧 · 别切窗口", bg="#1f2430", fg="#9fb3d1",
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=10, pady=(6, 0))
            row = tk.Frame(hud, bg="#1f2430")
            row.pack(fill="x", padx=10, pady=(2, 6))
            self._hud_text = tk.StringVar(value="已收 0 帧")
            tk.Label(row, textvariable=self._hud_text, bg="#1f2430", fg="#ffffff",
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            tk.Button(row, text="停止", command=self._stop_scan, relief="flat",
                      bg="#3b4356", fg="#ffffff", activebackground="#4a5468",
                      font=("Microsoft YaHei UI", 8), padx=8, cursor="hand2"
                      ).pack(side="right")
            return hud
        except tk.TclError:
            return None

    def _drop_hud(self):
        hud = getattr(self, "_hud", None)
        self._hud = None
        if hud is None:
            return
        try:
            hud.destroy()
        except tk.TclError:
            pass

    def _stop_scan(self):
        """点「停止」：让扫描线程收工，已收到的帧照常保留（下次接着收）。"""
        self._scan_cancelled = True
        try:
            self._hud_text.set("正在停止…")
        except (tk.TclError, AttributeError):
            pass

    def _save_scan_frame(self, img):
        """把最后一枪的画面存成 PNG，返回路径。

        「识别不了」光靠描述没法定位：是码太小、框错了地方、还是被别的窗口
        挡住，看一眼图就知道。失败时静默落盘 + 把路径打进日志。
        """
        if img is None:
            return None
        try:
            import tempfile

            name = "officepack_框选抓图_%s.png" % time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(tempfile.gettempdir(), name)
            img.save(path, "PNG")
            return path
        except Exception:  # noqa: BLE001  诊断信息存不下不该影响主流程
            return None

    @staticmethod
    def _scale_bbox(bbox, scale):
        """把逻辑坐标的框换算成物理像素的框；scale≈1 时原样返回。"""
        if abs(scale - 1.0) <= 0.01:
            return tuple(bbox)
        return tuple(int(round(v * scale)) for v in bbox)

    def _grab_scale(self, ImageGrab) -> float:
        """物理像素 ÷ 逻辑坐标 的比值，用来把框选矩形换算成截图坐标。

        本机实测踩到的坑：物理桌面 **3840×1200**，但 Tk 和 ``GetSystemMetrics``
        都只说 **3072×960**（125% 缩放）—— 进程拿到的是「逻辑坐标」，而
        ``ImageGrab`` 抓的是「物理像素」。不换算的话，框在副屏的矩形会整体
        缩水到 1/1.25 的位置，抓到的是一块空桌面，现象正是
        「同一块屏能识别、主屏放程序副屏放码就框不准」。

        比值直接由「整屏抓图宽度 ÷ 逻辑虚拟桌面宽度」反推：如果进程真的拿到了
        per-monitor DPI，这两个数本来就相等，比值为 1，这段换算自动失效。
        """
        try:
            vw = self._virtual_screen()[2]
            probe = ImageGrab.grab(all_screens=True)
            width = probe.width
            probe.close()
            if vw > 0 and width > 0:
                return width / float(vw)
        except Exception:  # noqa: BLE001  抓不到就按 1:1 处理
            pass
        return 1.0

    def _scan_region_unpack(self, bbox, ImageGrab):
        """框选后盯着这块区域连拍，把动图里一帧帧的码全收下来再还原。

        框选的真正用途就是动图：一张截图只有播放中的某一帧，收不全必然还原
        不了。连拍放后台线程（静态码第一枪就齐、立刻返回），主线程只刷日志，
        界面不会假死。
        """
        self._scan_cancelled = False
        self._scan_active = True
        scale = self._grab_scale(ImageGrab)
        if abs(scale - 1.0) > 0.01:
            self._log("· 屏幕缩放 %.0f%%：框选坐标已按物理像素换算"
                      "（这就是「一块屏能识别、换块屏框不准」的根因）"
                      % (scale * 100))
            bbox = self._scale_bbox(bbox, scale)
        self._run_scan(bbox, ImageGrab)

    def _scan_fps(self) -> float:
        """按界面上设的「帧/秒」去估收帧要多久。

        GIF 就是本程序按这个帧率生成的，屏幕上放的时候也是这个速度 —— 拿它
        算「播一轮要多久」才对得上。改成别的值也不会出事：扫描过程中还会拿
        实测收帧速度再校准一次。
        """
        try:
            return max(0.5, min(20.0, float(self.qr_fps.get())))
        except (ValueError, tk.TclError, AttributeError):
            return 3.0

    def _run_scan(self, bbox, ImageGrab):
        """真正开始连拍（``bbox`` 已经是物理像素坐标）。"""
        self._scan_cancelled = False
        self._scan_active = True
        self._scan_bbox = bbox
        self._last_phys = tuple(int(v) for v in bbox)
        self._hud = self._make_hud(bbox)
        # 兜底：万一后台线程被卡住（截屏 API 被杀软拦、桌面切换），窗口也得回来。
        # 用「绝对天花板」而不是默认 30 秒 —— 978 帧的动图本来就要跑好几分钟。
        self.root.after(int(qr.SCAN_CAP_MAX * 1000) + 15000, self._restore_window)
        fps = self._scan_fps()
        self._log("· 正在扫描框选区域 %d×%d @ (%d,%d) —— 动图请让它继续在框里循环播放"
                  "（首帧开始、尾帧收工，收齐自动停）"
                  % (bbox[2] - bbox[0], bbox[3] - bbox[1], bbox[0], bbox[1]))
        self._log("· 按 %.0f 帧/秒估算：收到第一帧读出总帧数后，连拍上限会放到"
                  "「够播 2.5 轮」，收齐前不会自己停；角落小条上有预计剩余时间"
                  % fps)
        # 只抓框内那块的 GDI 抓图：比 ImageGrab 整屏再裁快 4 倍（83ms → 20ms）。
        # 采样快了才追得上 3 帧/秒的播放速度，不然一帧丢一帧。
        last = {"t": 0.0}
        grab_fast = _make_grabber(bbox)
        if grab_fast is None:
            def grab():
                return ImageGrab.grab(bbox=bbox, all_screens=True)
        else:
            def grab():
                try:
                    return grab_fast()
                except Exception:  # noqa: BLE001  个别显卡/远控环境下 BitBlt 会失败
                    return ImageGrab.grab(bbox=bbox, all_screens=True)

        def report(st):
            # 日志别刷屏：3 秒一行，且只在真有新帧时才说。978 帧的动图要跑几分钟，
            # 原来 0.5 秒一行能刷出六十多行，把有用信息全冲没了。
            if st["frames"] and st["elapsed"] - last["t"] >= 3.0:
                last["t"] = st["elapsed"]
                need = st.get("need")
                text = ("已收 %d/%d 帧" % (st["frames"], need)) if need \
                    else ("已收 %d 帧" % st["frames"])
                eta = st.get("eta")
                tail = _fmt_eta(eta)          # 「还要 1 分 40 秒」/「快好了」
                self.queue.put(("hud", "%s · %s" % (text, tail)))
                self.queue.put(("log", "  %s（%d 枪 · 已用 %.0f 秒 · %s）"
                                % (text, st["shots"], st["elapsed"], tail)))

        def worker():
            try:
                texts, stats = qr.scan_region(
                    grab, on_progress=report,
                    should_stop=lambda: self._scan_cancelled,
                    frames_per_sec=fps)
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("call", lambda e=exc: self._scan_failed(e)))
                return
            if not texts:                      # 框内颗粒无收，整屏兜一次
                texts, stats = self._rescue_fullscreen(ImageGrab, stats)
            self.queue.put(("call", lambda: self._after_scan(texts, stats)))

        threading.Thread(target=worker, daemon=True).start()

    def _rescue_fullscreen(self, ImageGrab, stats):
        """框内一张码都没捞着时，整屏再找一遍。

        框错一点、DPI 缩放导致坐标偏移、或者码被切成两半（静区缺一角）都会
        让框内扫描一无所获，但码其实就在屏幕上。整屏抓一张再扫，能救回这类
        「明明看得见却识别不了」。只认 officepack 自己的分段（``#OPK1``/``#OPK2``），
        免得把屏幕上别处的二维码当数据。
        """
        try:
            full = ImageGrab.grab(all_screens=True)
        except Exception:  # noqa: BLE001
            return [], stats
        try:
            found = qr.decode_image(full, deep=True, keep_markers=True)
        except Exception:  # noqa: BLE001
            return [], stats
        try:
            keep = [t for t in found if not qr.is_marker(t)
                    and ct.parse_segments(t)]
        except Exception:  # noqa: BLE001
            keep = []
        if not keep:
            return [], stats
        stats = dict(stats)
        stats["rescue"] = "整屏 %d×%d" % (full.width, full.height)
        self.queue.put(("log", "· 框内没找到，但在**整屏**里扫到 %d 帧 —— "
                               "多半是框小了/框偏了，已按整屏结果继续"
                        % len(keep)))
        return keep, stats

    def _scan_failed(self, exc):
        self._restore_window()
        self.root.configure(cursor="")
        self._log("✗ 框选扫描失败：%s" % exc)
        messagebox.showerror(APP_TITLE, "框选扫描失败：\n%s" % exc)

    def _after_scan(self, texts, stats):
        """连拍结束：够帧就还原，不够就把这一轮收进累计区，等下一次框选续上。

        动图的播放速度、屏幕刷新和我们的抓图节奏不一定对得上，一轮漏掉一帧
        很正常。与其直接报「缺少第 7 段」让人从头再来，不如把已收到的攒起来，
        再框一次自动补齐 —— 用户不需要知道缺的是哪一帧。
        """
        self._restore_window()
        self.root.configure(cursor="")
        if not texts:
            self._show_rescan(bool(self._scan_texts))
            shot = self._save_scan_frame(stats.get("image"))
            self._log("✗ 框选区域没识别到二维码：%.1f 秒 / %d 枪"
                      % (stats["elapsed"], stats["shots"]))
            img = stats.get("image")
            want = getattr(self, "_scan_bbox", None)
            if img is not None and want:
                got_w, got_h = img.width, img.height
                ask_w, ask_h = want[2] - want[0], want[3] - want[1]
                self._log("· 抓到的画面 %d×%d（请求 %d×%d）%s"
                          % (got_w, got_h, ask_w, ask_h,
                             "" if (got_w, got_h) == (ask_w, ask_h)
                             else " ← 尺寸对不上，像是 DPI 缩放导致的错位"))
            extra = ""
            if shot:
                self._log("· 这次框到的画面已存成 %s（可以直接拖进文件列表试试，"
                          "或发我定位问题）" % shot)
                extra = ("\n\n这次框到的画面已存成：\n%s\n"
                         "可以直接把它拖进文件列表，看是不是图本身的问题。"
                         % shot)
            messagebox.showerror(
                APP_TITLE,
                "这块区域里没有识别到完整的二维码。\n\n"
                "· 动图：让它一直在框里循环播放，别停在中途某一帧\n"
                "· 码要整个进框，四周留一点白边（静区）\n"
                "· 显示得太小就放大到 100% 再框\n"
                "· 还不行就用「粘贴识别面板」，一张一张累加，顺序随意"
                + extra)
            return

        seen = set(self._scan_texts)
        fresh = [t for t in texts if t not in seen]
        # 换了一份文件就别再跟上一份攒的帧混在一起，否则永远「缺上一份的第 N 帧」
        if fresh and self._scan_texts and not (self._scan_docs(fresh)
                                               & self._scan_docs(self._scan_texts)):
            self._log("· 框到的是另一份文件，之前攒的帧作废")
            self._scan_texts = []
        self._scan_texts.extend(fresh)
        self._log("· 本轮扫描：%d 枪 / %.1f 秒 / %d 轮，新收 %d 帧（累计 %d 帧）"
                  % (stats["shots"], stats["elapsed"], stats.get("cycles", 0),
                     len(fresh), len(self._scan_texts)))

        missing = self._missing_frames(self._scan_texts)
        if missing:
            self._show_rescan(True)
            self._log("· %s —— 点「再来一轮」还是这块区域接着收，不必重框" % missing)
            # 帧数很多却只收到一两帧、又始终没见到首尾标识帧：框到的是**静态图**
            # （照片/截图/网页里的一张码）。静态图永远只有 1 帧，再怎么框也补不齐，
            # 得让用户换路子，而不是让他反复框同一张图。
            if not stats.get("cycles") and len(self._scan_texts) <= 4:
                messagebox.showwarning(
                    APP_TITLE,
                    "只收到 %d 帧，%s\n\n"
                    "这看起来是一张**静态图**（屏幕上的照片/截图里的一张码）。\n"
                    "静态图里永远只有 1 帧，再怎么框也补不齐整份文件。\n\n"
                    "· 要传整份文件：出码时选「PNG + GIF」，让 GIF 在屏幕上循环播放再框\n"
                    "· 手上只有 PNG：把**总览图**直接拖进文件列表识别，"
                    "总览里一格就是一帧" % (len(self._scan_texts), missing))
                return
            messagebox.showwarning(
                APP_TITLE,
                "还差几帧，已收的都替你先攒着了。\n\n%s\n\n"
                "让动图继续循环播放，点「再来一轮（还是那块区域）」即可自动补上 —— "
                "不用重新拖框（只有框歪了才需要「重新框选」）。"
                % missing)
            return

        texts, self._scan_texts = list(self._scan_texts), []
        self._show_rescan(False)
        try:
            results = self._unpack_frames(texts, "屏幕框选")
        except Exception as exc:  # noqa: BLE001
            self._log("✗ 还原失败：%s" % exc)
            messagebox.showerror(APP_TITLE, "还原失败：\n%s" % exc)
            return
        if messagebox.askyesno(
            APP_TITLE,
            "已从屏幕框选还原 %d 个文件\n\n%s\n\n打开所在文件夹？"
            % (len(results), self.last_out_dir),
        ):
            self.open_out_dir()

    @staticmethod
    def _scan_docs(texts) -> set:
        """这批帧属于哪些文档（按段头 doc_id），用来判断「是不是同一份文件」。"""
        try:
            segs = ct.parse_segments("\n".join(texts))
        except Exception:  # noqa: BLE001
            return set()
        return {seg.doc_id for seg in segs}

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
                notes.append("共 %d 帧，已收 %d，缺 %d 帧：第 %s 帧"
                             % (total, total - len(missing), len(missing),
                                _frame_ranges(missing)))
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

    def _start_bar(self):
        """换一个文件/换一个阶段：细分进度条归零，重新开始计时。"""
        self._bar_t0 = time.monotonic()
        self._bar_tick = self._bar_t0
        self._bar_logged = -1
        self._bar_last = (0, 1, "")
        try:
            if getattr(self, "_bar_writing", False):
                self.sub_progress.stop()
                self.sub_progress.configure(mode="determinate")
                self._bar_writing = False
            self.sub_progress.configure(maximum=1, value=0)
            self.prog_text.configure(text="")
        except tk.TclError:
            pass

    def _set_bar(self, done, total, phase=""):
        """刷新当前文件的帧级进度（工作线程经队列派过来，只在主线程执行）。"""
        total = max(1, int(total))
        done = max(0, min(int(done), total))
        self._bar_last = (done, total, phase)
        self._bar_tick = time.monotonic()
        # 「写入」是 PIL 内部一次性压完所有帧，没法按帧报进度 ——
        # 进度条切成来回滚动的动画，让人看出还在动，而不是停在 100% 假死。
        writing = "写入" in (phase or "")
        try:
            if writing and not getattr(self, "_bar_writing", False):
                self.sub_progress.configure(mode="indeterminate")
                self.sub_progress.start(120)
                self._bar_writing = True
            elif not writing and getattr(self, "_bar_writing", False):
                self.sub_progress.stop()
                self.sub_progress.configure(mode="determinate")
                self._bar_writing = False
            if not writing:
                self.sub_progress.configure(maximum=total, value=done)
        except tk.TclError:
            return
        if writing:
            self.prog_text.configure(
                text="%s · 大文件要几十秒，别关窗口" % phase)
            return
        pct = int(done * 100 / total)
        text = "%s %d%% (%d/%d)" % (phase or "处理", pct, done, total)
        elapsed = self._bar_tick - self._bar_t0 if self._bar_t0 else 0.0
        if elapsed >= 1.0:          # 刚起步那一瞬间显示「· 0.0s」没有意义
            text += " · %.1fs" % elapsed
        self.prog_text.configure(text=text)
        # 日志每 10% 留一行：事后翻日志能看出「哪一步花了多久」，也能确认没卡死
        if pct // 10 > self._bar_logged:
            self._bar_logged = pct // 10
            self._log("   %s %d%%（%d/%d）" % (phase or "处理", pct, done, total))

    def _bar_heartbeat(self):
        """一秒一跳的心跳：长任务里进度不动时，至少让人看出还在跑。"""
        if self._scan_active:
            # 框选扫描期间主窗口必须是藏起来的：它一露头就压在要扫的码上，
            # 每枪抓到的画面里都会多出一块窗口 —— 用户从任务栏把它点回来也一样。
            try:
                if self.root.state() != "withdrawn":
                    self.root.withdraw()
            except tk.TclError:
                pass
        if not self._bar_t0 or not (self.worker and self.worker.is_alive()):
            return
        now = time.monotonic()
        if now - self._bar_tick < 1.0:
            return
        self._bar_tick = now
        done, total, phase = self._bar_last
        pct = int(done * 100 / max(1, total))
        try:
            self.prog_text.configure(
                text="%s %d%% (%d/%d) · %.0fs"
                % (phase or "处理中", pct, done, total, now - self._bar_t0))
        except tk.TclError:
            pass

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self._show_rescan(False)
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
        self._start_bar()
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
                results, stats = qr.unpack_any(files, out_dir, password,
                                               progress=self._qr_progress())
                self.queue.put(("bar", (1, 1, "识别完成")))
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
            self._start_bar()
            try:
                if as_qr:
                    paths = self._pack_one_qr(src, name, out_dir, password, cipher,
                                              compress_mode, codec_name, outputs,
                                              qr_format, qr_fps)
                    ok += 1
                    continue

                out_path = None
                self.queue.put(("bar", (0, 1, "压缩加密")))
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

    def _qr_progress(self, phase: str | None = None):
        """给 qr 模块用的进度回调：工作线程 → 队列 → 主线程刷进度条。

        ``phase`` 给了就锁死阶段名（比如「合成动图」），没给就用 qr 自己报的。
        """
        def report(done, total, step=""):
            name = phase or "处理"
            if step and step != "渲染":
                name = "%s·%s" % (name, step)      # 合成动图·写入 WebP
            self.queue.put(("bar", (done, total, name)))
        return report

    def _pack_one_qr(self, src, name, out_dir, password, cipher, compress_mode,
                     codec_name, outputs, qr_format="PNG（单帧 + 总览）", qr_fps="3"):
        """单个文件走二维码通道；返回写出的图片路径。编码器沿用界面选择。"""
        with open(src, "rb") as fh:
            raw = fh.read()
        self.queue.put(("bar", (0, 1, "压缩加密")))
        self.queue.put(("log", "· %s 正在压缩/加密 %s…" % (name, _human(len(raw)))))
        pack = ct.pack_bytes(
            raw, name, codec_name=codec_name,
            cipher_name=cipher, password=password,
            compress=compress_mode, preview=False, ascii_only=True,
        )
        frames = qr.build_frames(pack, ec=qr.DEFAULT_EC)
        target_dir = out_dir or os.path.dirname(os.path.abspath(src))
        want_png = qr_format.startswith("PNG")
        want_single = "仅总览" not in qr_format
        want_gif = "GIF" in qr_format and len(frames) > 1
        want_webp = "WebP" in qr_format and len(frames) > 1
        if not (want_png or want_gif or want_webp):
            # 只选了动图格式，但这份文件一帧就装下了 —— 没有「动」可言。
            # 不能就这么啥也不写，老老实实出一张单帧 PNG。
            want_png = True
            want_single = True
        self.queue.put(("log", "   拆成 %d 张码，开始出图（%s）…"
                        % (len(frames), "渲染 PNG" if want_png else "合成动图")))

        res = {"images": [], "sheet": None, "sheets": [], "dir": target_dir}
        if want_png:
            res = qr.render_images(frames, target_dir, name, ec=qr.DEFAULT_EC,
                                   sheet=True, single=want_single,
                                   progress=self._qr_progress("渲染 PNG"))
            outputs.extend(res["images"])
            outputs.extend(res.get("sheets") or [])

        fps = 3.0
        try:
            fps = float(qr_fps)
        except (TypeError, ValueError):
            pass

        gif_path = None
        if want_gif:
            gif_path = os.path.join(target_dir, "%s.qr.gif" % name)
            qr.render_gif(frames, gif_path, ec=qr.DEFAULT_EC, fps=fps,
                          progress=self._qr_progress("合成动图"))
            outputs.append(gif_path)

        webp_path = None
        if want_webp:
            webp_path = os.path.join(target_dir, "%s.qr.webp" % name)
            qr.render_webp(frames, webp_path, ec=qr.DEFAULT_EC, fps=fps,
                           progress=self._qr_progress("合成 WebP"))
            outputs.append(webp_path)

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
        if webp_path:
            note += "，WebP %s（约 %s）" % (
                os.path.basename(webp_path),
                _human(os.path.getsize(webp_path)))
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
                "   动图可循环播放（首尾带标识帧，框选时能判断一轮起止）；"
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
        self._bar_t0 = 0.0
        try:
            self.sub_progress.configure(value=0)
            self.prog_text.configure(text="")
        except tk.TclError:
            pass
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
        bars = None
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "tick":
                    self.progress.step(payload)
                elif kind == "bar":
                    bars = payload      # 积压时只认最后一条，免得主线程被刷爆
                elif kind == "done":
                    self._finish(payload)
                elif kind == "hud":
                    try:
                        self._hud_text.set(payload)
                    except (tk.TclError, AttributeError):
                        pass
                elif kind == "call":
                    payload()          # 工作线程派回主线程的回调（弹窗必须在主线程）
        except queue.Empty:
            pass
        if bars is not None:
            self._set_bar(*bars)
        self._bar_heartbeat()
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


def _cursor_pos():
    """当前鼠标的**物理虚拟桌面坐标**；拿不到返回 None（非 Windows）。

    为什么不用 Tk 的 ``e.x`` / ``e.y`` 去算截图区域：那是窗口客户区坐标，
    跨屏（尤其两块屏缩放不同）时，Tk 和 Windows 会各自按自己的理解换算一遍，
    算出来的矩形就和 ``ImageGrab`` 的坐标系对不上 —— 现象正是「同一块屏能
    识别，主屏放程序、副屏放二维码就框不准」。``GetCursorPos`` 返回的是物理
    虚拟桌面坐标，和 ``ImageGrab.grab(all_screens=True)`` 用的是同一套，天然对齐。
    """
    try:
        import ctypes
        from ctypes import wintypes

        pt = wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
    except Exception:  # noqa: BLE001  非 Windows / 调用失败就走回退路径
        pass
    return None


def _fmt_eta(eta) -> str:
    """把「还要多少秒」说成人话：「还要 1 分 40 秒」/「就快好了」。

    ETA 是件很重要的事：978 帧要连拍好几分钟，光看帧数一点点涨，谁都会觉得
    「这得等到什么时候」。给出预计剩余时间，才知道是可以去泡杯茶，还是马上
    就完事了。
    """
    if eta is None:
        return "正在估时"
    if eta <= 3:
        return "就快好了"
    if eta < 60:
        return "还要 %d 秒" % int(round(eta))
    minutes, seconds = divmod(int(round(eta)), 60)
    if minutes < 10:
        return "还要 %d 分 %02d 秒" % (minutes, seconds)
    return "还要约 %d 分钟" % minutes


def _frame_ranges(nums: list[int], max_parts: int = 8) -> str:
    """把帧号压成区间：「1–118、209–547、……」。

    978 帧缺 700 个时，逐个列出来是一千多个字符的流水账，日志直接没法看；
    压成区间后既知道缺多少，也一眼看出缺的是哪几段连续的。
    """
    if not nums:
        return ""
    spans: list[tuple[int, int]] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        spans.append((start, prev))
        start = prev = n
    spans.append((start, prev))
    parts = [("%d" % a) if a == b else ("%d–%d" % (a, b)) for a, b in spans]
    if len(parts) > max_parts:
        return "、".join(parts[:max_parts]) + "…等 %d 段" % len(parts)
    return "、".join(parts)


def _make_grabber(bbox):
    """做一个「只抓框内区域」的抓图函数（Windows GDI BitBlt）；失败返回 None。

    为什么不用 ``ImageGrab``：它在 Windows 上**每次都把整个虚拟桌面抓下来再裁**，
    双屏 3840×1200 就是 18MB 的拷贝。实测抓同一块 971×948：

    * ``ImageGrab.grab(bbox=…, all_screens=True)``：**83ms/枪**
    * GDI ``BitBlt`` 只抓框内那块：**20ms/枪**（4 倍）

    动图 3 帧/秒、每帧停留 333ms，一枪 83ms 时采样点会漂，实测 142 枪只收到
    90 帧（漏 37%）；降到 20ms 后每帧能采 4~5 次，基本不丢。

    GDI 用的是**物理像素坐标**（和 ``ImageGrab`` 一致，已实测逐像素相同），
    所以传进来的 bbox 必须是换算过 scale 的物理坐标。
    """
    try:
        import ctypes
        from ctypes import wintypes

        from PIL import Image

        x1, y1, x2, y2 = [int(v) for v in bbox]
        width, height = x2 - x1, y2 - y1
        if width <= 0 or height <= 0:
            return None

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        hdc_screen = user32.GetDC(0)
        if not hdc_screen:
            return None
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
        gdi32.SelectObject(hdc_mem, hbmp)

        class _BitmapInfoHeader(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long),
                        ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        info = _BitmapInfoHeader()
        info.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.biWidth, info.biHeight = width, -height      # 负高度 = 自上而下
        info.biPlanes, info.biBitCount = 1, 32
        info.biCompression = 0
        buf = ctypes.create_string_buffer(width * height * 4)
        src_copy = 0x00CC0020                              # SRCCOPY

        def grab():
            if not gdi32.BitBlt(hdc_mem, 0, 0, width, height,
                                hdc_screen, x1, y1, src_copy):
                raise OSError("BitBlt 失败")
            if not gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf,
                                   ctypes.byref(info), 0):
                raise OSError("GetDIBits 失败")
            # copy() 必须留：buf 会被下一枪覆写，不拷出来所有帧会变成同一张
            return Image.frombuffer("RGB", (width, height), buf,
                                    "raw", "BGRX", 0, 1).copy()

        return grab
    except Exception:  # noqa: BLE001  非 Windows / API 不可用就走 ImageGrab
        return None


def _enable_dpi_awareness() -> None:
    """让进程感知 DPI：不设的话，150% 缩放下框选屏幕的坐标和截图会错位。

    双屏且缩放不同（本机主屏 125%、副屏 100%）时，必须用
    ``PER_MONITOR_AWARE_V2``：V1 下面 Windows 会把跨屏窗口按主屏缩放虚拟化，
    遮罩和截图就会错位。V2 拿不到（Win7/8）再退回 shcore 的 V1。
    """
    try:
        import ctypes

        u32 = ctypes.windll.user32
        if hasattr(u32, "SetProcessDpiAwarenessContext"):
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            if u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
    except Exception:  # 非 Windows / 老系统
        pass
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
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
