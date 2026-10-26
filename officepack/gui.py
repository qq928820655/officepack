"""图形界面：拖拽或选择文件，一键打包 / 还原。

依赖 tkinter（Python 自带）。若安装了 tkinterdnd2 则支持拖拽，否则退回按钮选择。
    pip install tkinterdnd2     # 可选，仅用于拖拽
"""

from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
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
from . import screengrab

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
            ("帧/秒", "动图播放速度，最高 20 帧/秒。\n"
                     "电脑框选（本程序自己抓屏）：10~15 最快，一帧还能被抓到两次以上。\n"
                     "手机扫：相机对焦 + 解码要 150ms 以上，用 3~6，太高反而漏帧。\n"
                     "换算：743 帧的 2MB 文件，3 帧/秒要播 4 分钟，10 帧/秒 75 秒，\n"
                     "15 帧/秒 50 秒 —— 「整份要几分钟」几乎全花在播放上。"),
            ("webp", "只影响 WebP 动图的编码档位（GIF 没有这个旋钮）。\n"
                     "速度优先：每帧约 25ms，780 帧约 20 秒，体积大约四成。\n"
                     "体积优先：每帧约 45ms，780 帧约 35 秒，体积最小。\n"
                     "两种都是**无损**，扫出来的数据完全一样，只差等待时间和文件大小。"),
            ("矩阵 1×1 / 2×2 / 3×3",
             "一帧里拼 N×N 张码再合成动图。动图帧数直接降到 1/N²，播放时长同样砍到 1/N² ——\n"
             "743 帧的文件按 10 帧/秒，1×1 要 74 秒，2×2 降到 19 秒，3×3 只要 8 秒。\n"
             "\n"
             "**限制在于「每张码还剩多少像素」，而且矩阵是正方形，卡的是屏幕高度。**\n"
             "每张码按 720px 生成，3×3 拼出来是 2244×2244 —— 要 1:1 不缩放地显示，\n"
             "屏幕上就得有一块 2244×2244 的可视区域：\n"
             "· 1920×1080：工作区高约 1040px，只有宽没有高 → 每张码被压到 347px\n"
             "· 1920×1200：约 1130px → 每张码 377px\n"
             "· 2560×1440：约 1360px → 每张码 453px\n"
             "· 3840×2160：约 2080px → 每张码 693px（最接近原图）\n"
             "二维码单帧约 2900 字节时是 version 40，横竖各 177 个模块：\n"
             "每模块 ≥2.5px 稳，2.0px 勉强能用，低于 1.7px 开始掉帧。\n"
             "这就是为什么一般显示器上 3×3「看着能播、却总收不全」：\n"
             "一是被压小，二是**只要正方形被切掉一条边（任务栏、窗口标题栏、播放器边距），\n"
             "就是整整一行码收不到**。要 3×3 就用浏览器 F11 全屏、关掉任务栏遮挡。\n"
             "**笔记本/1080p 优先 2×2**（每码 520px，2.9 像素/模块，正好够），\n"
             "1440p 以上再上 3×3，4K 才是 3×3 的舒适区。\n"
             "点「开始处理」时程序会把这两笔账算给你看，像素不够会直接提示该用几×几。\n"
             "\n"
             "识别端不用配：一张图里有几个码就收几个，按白缝自动切块、多线程解码，\n"
             "单码和矩阵通用；识别端帧率跟着播放器走，不需要另外设置。"),
            ("back", "还原：点「还原二维码 → 框选屏幕区域」，拖框圈住码，松开就连拍识别；\n"
                     "动图请让它一直在框里循环播放 —— 同一块区域会连拍到你收齐为止，\n"
                     "漏的帧再框一次会自动续上（累计不会重复）。\n"
                     "也可以读剪贴板截图、选图片文件，或直接把 PNG/GIF 拖进列表。\n"
                     "顺序无关：每帧都带序号和校验，扫到哪帧收哪帧。"),
        ],
    ),
    # 独立一项：原来它藏在「输出二维码」底下，点自己的 ? 会 KeyError，弹出来是空的
    "webp": (
        "WebP 设置",
        "只影响 WebP 动图的编码档位（GIF 没有这个旋钮）。两种档位都是无损，"
        "扫出来的数据完全一样，只差等待时间和文件大小。",
        [
            ("速度", "每帧约 25ms：780 帧约 20 秒，体积大约四成。帧多的动图选它。"),
            ("体积", "每帧约 45ms：780 帧约 35 秒，体积最小。只在文件大小敏感时选。"),
        ],
    ),
}



def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%.1f GiB" % n


#: 识别失败时留在 %TEMP% 的诊断抓图前缀（``_save_scan_frame`` 写的）。
SCAN_SHOT_PREFIX = "officepack_框选抓图_"


def asset_path(name: str) -> str | None:
    """找打包进来的资源（图标之类）。

    源码运行：``<项目根>/assets/<name>``
    PyInstaller：``<_MEIPASS>/assets/<name>``（spec 里把 assets 打进了 datas）
    找不到就返回 None —— 图标缺了顶多没图标，不能因此启动失败。
    """
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "assets", name)
    return path if os.path.exists(path) else None


def scan_shots(max_age: float | None = None) -> list[str]:
    """本程序留在临时目录里的框选抓图。

    ``max_age`` 给了就只返回**比它旧**的（秒）；启动时用它只清隔夜的，
    免得把用户刚生成、还没看完的那张删掉。
    """
    import tempfile

    root = tempfile.gettempdir()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    now = time.time()
    out = []
    for name in names:
        if not (name.startswith(SCAN_SHOT_PREFIX) and name.endswith(".png")):
            continue
        path = os.path.join(root, name)
        if max_age is not None:
            try:
                if now - os.path.getmtime(path) < max_age:
                    continue
            except OSError:
                continue
        out.append(path)
    return out


def clean_scan_shots(max_age: float | None = None) -> tuple[int, int]:
    """删掉诊断抓图，返回（删了几个, 释放多少字节）。

    这些图只在「识别失败」时有用（让用户看一眼是码太小还是框错了），
    一张 925×927 的 PNG 约 200KB。还原成功之后它们就没用了，一直堆在
    %TEMP% 里白占磁盘 —— 用户提过「还原完这些图可以删掉」。
    """
    removed = freed = 0
    for path in scan_shots(max_age):
        try:
            size = os.path.getsize(path)
            os.remove(path)
        except OSError:
            continue
        removed += 1
        freed += size
    return removed, freed


class ThinBar(tk.Canvas):
    """6 像素高的自绘进度条。

    为什么不用 ttk.Progressbar：Windows 的 vista 主题把高度写死成 22px，
    ``thickness`` / ``troughthickness`` 全被主题忽略（实测四种写法都是 22px）。
    两条 22px 的进度条就是 44px，全是从日志区抢走的 —— 用户直接说「进度条太粗，
    日志框能展示的信息太少」。自己画一条 6px 的，高度和颜色都说了算。

    接口和 ttk.Progressbar 常用的那几个方法对齐（``configure(maximum/value/mode)``
    / ``start()`` / ``stop()``），调用方不用改。
    """

    HEIGHT = 6

    def __init__(self, master, **kw):
        kw.setdefault("height", self.HEIGHT)
        super().__init__(master, highlightthickness=0, bd=0, bg="#e6eaf0", **kw)
        self._value = 0.0
        self._max = 1.0
        self._indeterminate = False
        self._phase = 0.0
        self._job = None
        self._bar = self.create_rectangle(0, 0, 0, self.HEIGHT,
                                          fill="#0a58ca", width=0)
        self.bind("<Configure>", lambda _e: self._redraw())

    # ---- 与 ttk 对齐的接口 ----
    def configure(self, cnf=None, **kw):  # noqa: D401  ttk 兼容层
        mode = kw.pop("mode", None)
        if "maximum" in kw:
            try:
                self._max = max(1e-9, float(kw.pop("maximum")))
            except (TypeError, ValueError):
                self._max = 1.0
        if "value" in kw:
            try:
                self._value = float(kw.pop("value"))
            except (TypeError, ValueError):
                self._value = 0.0
        if mode is not None:
            self._indeterminate = str(mode) == "indeterminate"
            if not self._indeterminate:
                self._stop_job()
        if kw or cnf:
            super().configure(cnf, **kw)
        self._redraw()

    config = configure

    def start(self, interval: int = 120):
        """来回滚动：PIL 一次性压完所有帧，没法按帧报进度，得让人看出还在动。"""
        self._indeterminate = True
        self._stop_job()
        self._tick(max(30, int(interval)))

    def stop(self):
        self._stop_job()
        self._indeterminate = False
        self._redraw()

    # ---- 绘制 ----
    def _tick(self, interval):
        self._phase = (self._phase + 0.06) % 1.0
        self._redraw()
        self._job = self.after(interval, lambda: self._tick(interval))

    def _stop_job(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _redraw(self):
        try:
            width = max(1, self.winfo_width())
            height = int(self.cget("height"))
            if self._indeterminate:
                seg = max(24, int(width * 0.28))
                x = int((width + seg) * self._phase) - seg
                self.coords(self._bar, max(0, x), 0, min(width, x + seg), height)
            else:
                frac = 0.0 if self._max <= 0 else min(1.0, self._value / self._max)
                self.coords(self._bar, 0, 0, int(width * frac), height)
        except tk.TclError:
            pass


class App:
    #: 底部进度行固定占位（像素）。一行说明字 + 6px 进度条，见布局里那段注释。
    #: 24 是默认字体在 125% 缩放下的行高；固定住就不会因为「有没有活」而变。
    SUB_ROW_H = 24

    def __init__(self, root: tk.Tk, has_dnd: bool):
        self.root = root
        self.has_dnd = has_dnd
        self.files: list[str] = []
        self.queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.last_out_dir: str | None = None   # 上一轮实际写出文件的目录
        self.last_out_file: str | None = None  # 上一轮第一个产出，用来在资源管理器里选中
        self._scan_texts: list[str] = []       # 框选连拍的累计帧（多次框选可续上）
        self._scan_rate = 0.0                  # 上一轮实测收帧速度（帧/秒）：估时用它才准
        self._webp_rate = 0.0                  # 上一轮实测「写动图」秒/帧：一次性写入的估时靠它
        self._scan_phase = 0.0                 # 下一轮采样相位（0~1 个采样周期，见 qr.plan_jitter）
        self._scan_plan = None                  # 上一轮算出的抖动/提速建议（见 qr.plan_jitter）
        self._scan_cancelled = False
        # 打包/还原的「中止」信号。以前只有框选扫描能中止，出码和还原一旦开跑
        # 就只能等 —— 957 帧的动图要跑一分钟，中途发现选错格式根本停不下来。
        # 用 Event 而不是 bool：工作线程只读，主线程置位，不需要加锁。
        self._cancel = threading.Event()
        self._run_t0 = 0.0                     # 本轮开始时刻（算「本次总用时」）
        self._scan_active = False              # 扫描进行中：主窗口保持最小化
        self._last_phys = None                 # 上次框选的物理坐标：再来一轮不用重框
        self._scan_bbox = None                 # 本次扫描区域（物理像素）
        self._scan_scale = 1.0                 # 物理 ÷ 逻辑，挪窗口时要把框换回逻辑坐标
        self._win_home = None                  # 扫描前窗口位置：避让挪开后要还回去
        self._win_moved = False                # 是否为了避让而挪过窗口
        self._win_zoomed = False               # 扫描前是最大化的话，完事要还原
        self._avoid_at = 0.0                   # 上次检查窗口是否挡住码的时间
        self._hud = None                       # 扫描期间的小悬浮条
        self._log_lines = 14                   # 日志框行数，屏幕不够时会被自动压缩
        self._poll_id: str | None = None       # 队列轮询的 after id
        # 当前文件内部的进度（第几张码 / 第几张图）。大文件出码是几百帧的活，
        # 只看文件级进度条会以为程序卡死了，所以另开一条细分进度 + 文字。
        self._bar_t0 = 0.0
        self._bar_tick = 0.0
        self._bar_logged = -1                  # 已经打过的百分比刻度（每 10% 一行）
        self._bar_last = (0, 1, "", False)

        root.title(APP_TITLE)

        self._build_style()
        self._set_window_icon()
        self._build_ui()
        # 布局建完才知道内容真实需要多大：不能写死 geometry，
        # 否则在 125%/150% 缩放的屏幕上底部按钮会被裁掉。
        self._fit_window()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_queue()
        # 隔夜的诊断抓图顺手清掉：一张约 200KB，攒几次就上百兆。
        # 只删本程序自己写的（前缀匹配），且只删超过 1 天的。
        clean_scan_shots(max_age=24 * 3600)

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

    def _set_window_icon(self):
        """窗口 / 任务栏图标。

        优先用 ``iconphoto`` 喂多个尺寸的 PNG（16/32/48/256）—— Tk 会把它们
        一起交给 Windows，标题栏和任务栏各挑合适的；ICO 只作兜底，因为 Tk
        自带的 ico 解析器只读文件里的第一条，而第一条是 256 的 PNG，读不动。
        图标缺失不该影响启动，所以全程静默。
        """
        photos = []
        for name in ("officepack-48.png", "officepack-32.png", "officepack-16.png",
                     "officepack.png"):
            path = asset_path(name)
            if not path:
                continue
            try:
                photos.append(tk.PhotoImage(file=path))
            except tk.TclError:
                pass
        if photos:
            self._icon_photo = photos        # 必须留住引用，否则被回收后图标变空
            try:
                self.root.iconphoto(True, *photos)
                return
            except tk.TclError:
                pass
        ico = asset_path("officepack.ico")
        if ico and os.name == "nt":
            try:
                self.root.iconbitmap(default=ico)
            except tk.TclError:
                pass

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

        # 日志区**先** pack（side="bottom"）：Tk 按 pack 顺序分空间，先 pack 的
        # 拿够自己需要的高度。以前它在 body 之后 pack，而上方的选项区太高，
        # 日志被压成一行、底部进度条直接掉到窗口外面 —— 就是「执行信息展示不全」。
        bottom = ttk.LabelFrame(self.root, text="日志")
        bottom.pack(side="bottom", fill="x", padx=10, pady=(4, 10))

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
        # 历史残留清理：扫过的诊断抓图、中止/强退留下的半成品记录都堆在 %TEMP%
        ttk.Button(btns, text="清理临时文件", command=self.clean_temp).pack(side="right")

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

        right = ttk.LabelFrame(self.opt_canvas)
        # 「全部参数说明」按钮太占地方，改成标题右侧一个 ? —— 点开还是同一份说明。
        # 顺带把这一行的高度省下来给日志。
        title_bar = ttk.Frame(right)
        ttk.Label(title_bar, text="选项").pack(side="left")
        self.help_btn = tk.Label(title_bar, text=" ? ", cursor="hand2",
                                 bg="#eef3ff", fg="#0a58ca", relief="ridge", bd=1)
        self.help_btn.pack(side="left", padx=(6, 0))
        self.help_btn.bind("<Button-1>", lambda _e: self._show_help(None))
        right.configure(labelwidget=title_bar)
        self.opt_frame = right
        self._opt_window = self.opt_canvas.create_window((0, 0), window=right, anchor="nw")
        right.bind("<Configure>", lambda _e: self._sync_opt_scroll())
        self.opt_canvas.bind("<Configure>", self._on_opt_canvas_resize)
        self.opt_canvas.bind("<Enter>", lambda _e: self.opt_canvas.bind_all(
            "<MouseWheel>", self._on_opt_wheel))
        self.opt_canvas.bind("<Leave>", lambda _e: self.opt_canvas.unbind_all("<MouseWheel>"))

        self.mode = tk.StringVar(value="pack")
        ttk.Label(right, text="操作模式").grid(row=0, column=0, sticky="w", **pad)
        # 「打包 / 还原」并排一行：上下两行纯属浪费高度，箭头两侧也不留空格
        mode_row = ttk.Frame(right)
        mode_row.grid(row=1, column=0, sticky="w", padx=10)
        ttk.Radiobutton(mode_row, text="打包（文件→文本）", variable=self.mode,
                        value="pack").pack(side="left")
        ttk.Radiobutton(mode_row, text="还原（文本→文件）", variable=self.mode,
                        value="unpack").pack(side="left", padx=(8, 0))

        self.preset = tk.StringVar(value="chars")
        self.codec = tk.StringVar(value=ct.PRESETS["chars"]["codec"])
        self.compress = tk.StringVar(value=ct.PRESETS["chars"]["compress"])
        self.cipher = tk.StringVar(value="none")
        self.password = tk.StringVar()
        self.show_pwd = tk.BooleanVar(value=False)

        # 预设 与 分片输出 并排一行（两个都是窄控件，各占半行）
        self.split_enabled = tk.BooleanVar(value=False)
        self.segment = tk.StringVar(value=str(ct.DEFAULT_SEGMENT_CHARS))
        preset_split = ttk.Frame(right)
        preset_split.grid(row=2, column=0, sticky="ew", padx=10, pady=(2, 4))
        preset_split.columnconfigure(0, weight=1, uniform="ps")
        preset_split.columnconfigure(1, weight=1, uniform="ps")

        cell = ttk.Frame(preset_split)
        cell.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        head = ttk.Frame(cell)
        head.pack(fill="x")
        ttk.Label(head, text="预设").pack(side="left")
        mark = ttk.Label(head, text="?", foreground="#0a58ca", cursor="hand2")
        mark.pack(side="left", padx=(3, 0))
        mark.bind("<Button-1>", lambda _e: self._show_help("preset"))
        self.preset_box = ttk.Combobox(cell, textvariable=self.preset, width=10,
                                       state="readonly",
                                       values=["qr（单张码装最多）", "chars（字数最少）",
                                               "compact（ASCII 体积最小）",
                                               "safe（纯大写+数字，耐复制）",
                                               "fast（速度快）", "plain（不压缩）", "自定义"])
        self.preset_box.pack(fill="x")
        self.preset_box.set("chars（字数最少）")
        self.preset_box.bind("<<ComboboxSelected>>", self._on_preset)

        cell = ttk.Frame(preset_split)
        cell.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        head = ttk.Frame(cell)
        head.pack(fill="x")
        ttk.Label(head, text="分片输出").pack(side="left")
        mark = ttk.Label(head, text="?", foreground="#0a58ca", cursor="hand2")
        mark.pack(side="left", padx=(3, 0))
        mark.bind("<Button-1>", lambda _e: self._show_help("split"))
        seg_row = ttk.Frame(cell)
        seg_row.pack(fill="x")
        self.split_chk = ttk.Checkbutton(seg_row, text="启用", variable=self.split_enabled,
                                         command=self._on_split_toggle)
        self.split_chk.pack(side="left")
        self.seg_entry = ttk.Entry(seg_row, textvariable=self.segment, width=6, state="disabled")
        self.seg_entry.pack(side="left", padx=(4, 3))
        ttk.Label(seg_row, text="字/段").pack(side="left")

        # 编码器 / 压缩器的下拉项都很短，并成一行两列，省下的高度留给日志
        self._param_pair(right, 3, [
            ("编码器", "codec", self._build_codec),
            ("压缩器", "compress", self._build_compress),
        ])
        self._param_pair(right, 4, [
            ("加密器", "cipher", self._build_cipher),
            ("口令", "password", self._build_password),
        ])

        self._param_label(right, 5, "输出目录", "outdir")
        self.out_dir = tk.StringVar(value="")
        out_row = ttk.Frame(right)
        out_row.grid(row=6, column=0, sticky="ew", padx=10)
        ttk.Entry(out_row, textvariable=self.out_dir, width=16).pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="…", width=3, command=self.pick_out_dir).pack(side="left", padx=(4, 0))

        # 二维码输出（与分片互斥：二维码有自己的分帧规则）
        self._param_label(right, 7, "输出二维码", "qr")
        self.qr_enabled = tk.BooleanVar(value=False)
        self.qr_format = tk.StringVar(value="PNG（单帧 + 总览）")
        self.qr_fps = tk.StringVar(value="10")
        self.qr_grid = tk.StringVar(value="1×1")
        self.webp_mode = tk.StringVar(value="速度优先")
        qr_row = ttk.Frame(right)
        qr_row.grid(row=8, column=0, sticky="ew", padx=10)
        self.qr_chk = ttk.Checkbutton(qr_row, text="生成", variable=self.qr_enabled,
                                      command=self._on_qr_click)
        self.qr_chk.pack(side="left")
        self.qr_fmt_box = ttk.Combobox(qr_row, textvariable=self.qr_format, width=17,
                                       state="readonly",
                                       values=["PNG（单帧 + 总览）", "PNG（仅总览）",
                                               "GIF 动图", "WebP 动图", "GIF + WebP", "PNG + GIF",
                                               "PNG + GIF + WebP"])
        self.qr_fmt_box.pack(side="left", padx=(6, 0))
        ttk.Entry(qr_row, textvariable=self.qr_fps, width=3).pack(side="left", padx=(5, 0))
        ttk.Label(qr_row, text="帧/秒", style="Hint.TLabel").pack(side="left", padx=(2, 0))
        # 一帧放 N×N 张码：播放时长直接砍到 1/N²，代价是屏幕上每张码要占 1/N 宽。
        # 默认 1×1；2×2 / 3×3 只对动图有意义（单帧 PNG 照旧一张一码）。
        self.qr_grid_box = ttk.Combobox(qr_row, textvariable=self.qr_grid, width=4,
                                        state="readonly", values=["1×1", "2×2", "3×3"])
        self.qr_grid_box.pack(side="left", padx=(8, 0))

        # WebP 编码档位：标题 + 两个选项挤在同一行，差异说明交给 ? （原来括号里
        # 那半句「快 1.8 倍 / 小四成」放不下还容易看错）
        webp_row = ttk.Frame(right)
        webp_row.grid(row=9, column=0, sticky="ew", padx=10, pady=2)
        ttk.Label(webp_row, text="WebP 设置").pack(side="left")
        mark = ttk.Label(webp_row, text="?", foreground="#0a58ca", cursor="hand2")
        mark.pack(side="left", padx=(4, 10))
        mark.bind("<Button-1>", lambda _e: self._show_help("webp"))
        ttk.Radiobutton(webp_row, text="速度", value="速度优先",
                        variable=self.webp_mode).pack(side="left")
        ttk.Radiobutton(webp_row, text="体积", value="体积优先",
                        variable=self.webp_mode).pack(side="left", padx=(8, 0))

        # 开始 / 中止同一行：开跑之后「开始」变灰，右边这个才亮 ——
        # 用户不用去别处找停止入口，视线就在原地。
        go_row = ttk.Frame(right)
        go_row.grid(row=10, column=0, sticky="ew", padx=10, pady=(12, 3))
        go_row.columnconfigure(0, weight=1)
        self.go = ttk.Button(go_row, text="开始处理", style="Go.TButton", command=self.start)
        self.go.grid(row=0, column=0, sticky="ew")
        self.stop_btn = ttk.Button(go_row, text="中止", width=6,
                                   command=self.cancel_run, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=(6, 0))

        self.open_btn = ttk.Button(right, text="打开输出文件夹", command=self.open_out_dir)
        self.open_btn.grid(row=11, column=0, sticky="ew", padx=10, pady=(0, 3))

        self.paste_btn = ttk.Button(right, text="粘贴文本还原…", command=self.paste_unpack)
        self.paste_btn.grid(row=12, column=0, sticky="ew", padx=10, pady=(0, 3))

        self.shot_btn = ttk.Menubutton(right, text="还原二维码 ▾")
        shot_menu = tk.Menu(self.shot_btn, tearoff=False)
        shot_menu.add_command(label="粘贴识别面板…（贴图/连 Ditto，推荐）",
                              command=self.paste_unpack)
        # 「框选屏幕区域」= 重新开始：上一次攒的帧会先作废，不跟新文件混
        shot_menu.add_command(label="框选屏幕区域…（支持多屏，从零开始）",
                              command=lambda: self.grab_region())
        shot_menu.add_command(label="读取剪贴板截图", command=self.qr_unpack)
        shot_menu.add_command(label="选择图片文件…（可多选）",
                              command=lambda: self.qr_unpack(from_file=True))
        self.shot_btn["menu"] = shot_menu
        self.shot_btn.grid(row=13, column=0, sticky="ew", padx=10, pady=(0, 8))

        self._on_split_toggle()
        try:
            self.mode.trace_add("write", lambda *_: self._sync_mode())
        except AttributeError:  # Tk < 8.6
            self.mode.trace("w", lambda *_: self._sync_mode())

        # 底部：日志（bottom 已在 body 之前 pack 好，这里只填内容）
        # 底部固定区（进度条 + 补录按钮）必须**先** pack：Tk 按 pack 顺序分空间，
        # 先 pack 的拿够自己需要的高度，剩下的才留给日志。反过来（日志先 pack 且
        # expand=True）日志会把空间吃干，按钮被压成 0 高 —— pack 了却点不到，
        # 用户看到的就是「缺帧了但根本没有补录按钮」。
        self._foot = ttk.Frame(bottom)
        self._foot.pack(side="bottom", fill="x")
        self.log = tk.Text(bottom, height=self._log_lines, wrap="none", font=("Consolas", 9),
                           bg="#fbfbfb", relief="flat", state="disabled")
        # 下边距交给下面那条进度行去撑：它**常年占位**，多留 pady 就白花了
        self.log.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        # 只留一条进度条：上面那条「第几个文件」在处理单个文件时永远是 0 或 100%，
        # 纯占高度（用户直接问「这道横杠有什么用」）。文件序号改由右侧文字带出来。
        #
        # 这一行**固定占位**（不管有没有活干都留着），原因是以前「用的时候才 pack」
        # 会让整窗需要的高度在合成阶段突然多出 28px：窗口已经顶到工作区下沿，长不高，
        # Tk 只能去压别人 —— 右侧选项区被压出滚动条，底部进度行贴着窗口下沿被裁掉半截。
        # 占位后，从启动到收尾的布局完全一样，谁都不会再被临时挤一下。
        self._sub_row = ttk.Frame(self._foot, height=self.SUB_ROW_H)
        self._sub_row.pack(fill="x", padx=8, pady=(0, 2))
        self._sub_row.pack_propagate(False)
        self.sub_progress = ThinBar(self._sub_row)
        # 文字必须**先** pack 在右侧：pack 按顺序分空间，反过来时进度条
        # fill+expand 会把它挤出窗口，只剩半截字（用户截图里正是这样）。
        # 不给 width（那是「字符数」，长一点的阶段名会被硬裁掉末尾）——
        # 改成按文字自适应 + 超长自己省略，见 _elide_bar_text()。
        self.prog_text = ttk.Label(self._sub_row, text="", foreground="#0a58ca",
                                   anchor="e")
        # 行高按**真实字体**定：150% 缩放或用户换了更大的系统字体时，24px 装不下
        # 一行字，文字会被这层「写死高度」的壳裁掉半截。
        try:
            self._sub_row.configure(
                height=max(self.SUB_ROW_H, self.prog_text.winfo_reqheight()))
        except tk.TclError:
            pass
        self._bar_w = 480            # 进度条最长多少像素，再长就只是空跑
        # 窗口一拉伸，进度条跟着重算长度（文字优先，条子占剩下的，封顶 480）
        self._sub_row.bind("<Configure>", self._fit_bar_width)
        self._file_pos = (0, 0)        # (处理到第几个, 一共几个)
        self._bars_shown = False        # 空闲时不占位置：空进度条只会让人以为界面坏了
        # 缺帧时出现的三个按钮，并排一行：一个**还是那块区域**再来一轮（不用重新
        # 拖框），一个重新框，一个放弃。前两个都**只补缺的帧**，已收的不会重数。
        self._rescan_row = ttk.Frame(self._foot)
        self.replay_btn = ttk.Button(self._rescan_row, text="再来一轮（同一区域）",
                                     command=self._replay_scan)
        self.rescan_btn = ttk.Button(self._rescan_row, text="重新框选（接着收）",
                                     command=lambda: self.grab_region(resume=True))
        # 差一两帧又补不上时的体面出口：清掉攒下的帧和临时抓图，下次从零开始。
        # 没有它，用户只能关掉程序重开（帧在内存里、抓图在 %TEMP% 里都还在）。
        self.giveup_btn = ttk.Button(self._rescan_row, text="放弃（清掉已收）",
                                     command=self._give_up_scan)
        self._rescan_shown = False

    def _show_bars(self, show: bool):
        """干活时才把进度条露出来；空闲时只留那行**空白占位**（高度不变）。

        行本身一直在（高度固定），所以切进切出不会改变窗口需要的高度 —— 这正是
        「合成阶段底部被裁、右侧冒出滚动条」的根因，见布局里那段注释。
        """
        if show == self._bars_shown:
            return
        self._bars_shown = show
        try:
            if show:
                self.prog_text.pack(side="right", padx=(8, 6))
                self.sub_progress.pack(side="left", padx=(0, 2))
                self._fit_bar_width()
                self._pack_rescan_buttons()      # 按钮永远排在进度条下面
            else:
                self.prog_text.pack_forget()
                self.sub_progress.pack_forget()
        except tk.TclError:
            pass

    def _fit_bar_width(self, _event=None):
        """进度条按剩余宽度伸缩，但**最多 _bar_w 像素**。

        用户的原话是「进度条可以适当缩短一点」：一条横贯整个窗口的进度条看着像
        分隔线，而且它一长，右边的说明字就被挤到窗口边缘、末尾直接被窗口裁掉。
        这里把条子封顶，余量全部让给文字。
        """
        try:
            row = self._sub_row.winfo_width()
            need = self.prog_text.winfo_reqwidth()
        except tk.TclError:
            return
        if row <= 1:
            return
        avail = row - need - 16
        self.sub_progress.configure(width=max(120, min(self._bar_w, max(120, avail))))

    @staticmethod
    def _elide_bar_text(text: str, limit: int = 42) -> str:
        """进度文字太长就自己省略，别指望窗口替我们裁。

        曾经用 ``width=31``（单位是字符）把它框住，结果阶段名一长，末尾的
        「100% (3825/3825)」就被硬生生裁掉 —— 那才是最要紧的数字。
        """
        if len(text) <= limit:
            return text
        return text[:max(1, limit - 1)] + "…"

    def _pack_rescan_buttons(self):
        """把补录按钮排成**一行**（横向并列）。

        三个按钮各占一行要吃掉 96px —— 实测能把日志从 14 行压到 8 行，
        窗口长不高的时候更是直接把选项区挤歪。并列一行只花 32px。
        """
        if not self._rescan_shown:
            return
        row = self._rescan_row
        row.pack(fill="x", padx=8, pady=(0, 4))
        if self._last_phys:
            self.replay_btn.pack(side="left", fill="x", expand=True,
                                 padx=(0, 4))
        self.rescan_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.giveup_btn.pack(side="left", fill="x", expand=True)

    def _show_rescan(self, show: bool, missing: int = 0):
        """缺帧才露出补录按钮；不占常驻空间，也不会把底部按钮挤掉。

        ``missing`` 是还差几帧 —— 直接写进按钮文字里，一眼就知道点它要补多少。
        字要短：三个按钮挤在一行里，写长了就互相压。
        """
        if show:
            try:
                self.replay_btn.configure(
                    text="再来一轮（只补 %d 帧）" % missing if missing
                    else "再来一轮（同一区域）")
                self.rescan_btn.configure(
                    text="重新框选（只补 %d 帧）" % missing if missing
                    else "重新框选（接着收）")
            except tk.TclError:
                pass
        if show == self._rescan_shown:
            return
        self._rescan_shown = show
        try:
            if show:
                self._pack_rescan_buttons()
            else:
                self.replay_btn.pack_forget()
                self.rescan_btn.pack_forget()
                self.giveup_btn.pack_forget()
                self._rescan_row.pack_forget()
        except tk.TclError:
            pass
        # 补录按钮一出现，底部就长高一大截 —— 跟进度行一样会挤到别人。
        # 交给同一套「先长高、再少两行日志、最后才滚动」的收尾逻辑处理。
        self._schedule_settle()

    def _schedule_settle(self):
        """布局收尾要跑**两次**：80ms 那次经常量到旧尺寸。

        pack 之后 Tk 要等下一轮布局才更新 reqsize，80ms 时 `opt_canvas`
        报的还是旧高度，`_settle_opt_height` 会判成「已经装得下」直接返回，
        之后再没人复查 —— 表现就是补录按钮出现时右侧突然冒滚动条、
        选项区被压掉几十像素（用户报的就是这个「显示不全」）。
        300ms 那次兜住，函数本身幂等，多跑一次只是几次查询。
        """
        for delay in (80, 300):
            try:
                self.root.after(delay, self._settle_opt_height)
            except tk.TclError:
                return

    def _drop_scan_accum(self, why: str = "") -> int:
        """清掉上一次攒的帧和临时抓图；返回清掉多少帧。

        「框选屏幕区域」的语义是**重新开始**：用户拿一份新文件来框时，上一份的帧
        必须作废，否则两份文件混在一起（老文件碰巧已经齐了才没出事）。
        顺手把 %TEMP% 里的诊断抓图删掉 —— 不然每扫一次就多攒一批。
        """
        n = len(self._scan_texts)
        if not n:
            self._scan_phase = 0.0
            self._scan_plan = None
            return 0
        self._scan_texts = []
        self._scan_rate = 0.0
        self._scan_phase = 0.0
        self._scan_plan = None
        shots, freed = clean_scan_shots()
        self._show_rescan(False)
        self._log("· %s：上一次攒的 %d 帧作废%s，这次从零开始"
                  % (why or "重新开始", n,
                     ("（顺带删掉 %d 张临时抓图，释放 %s）" % (shots, _human(freed)))
                     if shots else ""))
        return n

    def _give_up_scan(self):
        """放弃这次识别：攒下的帧和临时抓图一起清掉，回到「从零开始」。

        差一两帧又怎么都补不上时，堆着既占内存（一帧几百字节，几千帧就是几 MB）
        又占磁盘（%TEMP% 里的诊断抓图），而且下一次「接着上次」还会背着这个包袱。
        用户明确要一个能自己退出的开关，清了就能重新扫。
        """
        n = len(self._scan_texts)
        try:
            ok = messagebox.askyesno(
                APP_TITLE,
                "放弃这次识别？\n\n"
                "会清掉已经收下的 %d 帧（内存）和临时抓图，下次框选从零开始。\n\n"
                "如果只差几帧，建议先点「再来一轮」—— 已收的帧不会重收。" % n)
        except tk.TclError:
            ok = True
        if not ok:
            return
        self._drop_scan_accum("已放弃本次识别")

    def _tune_fps_for_speed(self):
        """补录前自动把「动图帧率」调快 —— 前提是算得出来会更快。

        每一轮都必须让动图走完一整圈（缺的帧可能在任何位置），所以补 R 轮的
        耗时就是 R 圈。只要枪速超过播放帧率，一轮之内就能扫完 —— 把帧率从
        10 提到 12，比"再补 3 轮"快三倍。
        """
        plan = getattr(self, "_scan_plan", None) or {}
        if plan.get("fastest") != "raise_fps":
            return
        target = plan.get("target_fps")
        if not target:
            return
        cur = self._scan_fps()
        want = min(20, int(math.ceil(float(target))))
        if want <= cur:
            return
        try:
            self.qr_fps.set(str(want))
        except tk.TclError:
            return
        self._log("· 已把「动图帧率」%d → %d（枪速超过播放率）：一轮就能扫完，"
                  "比按原设置补 %s 轮快"
                  % (cur, want, plan.get("rounds_left") or "?"))

    def _replay_scan(self):
        """拿上次那块区域再来一轮 —— 补录几十个帧，不用重新拖框。

        「漏了几帧 → 再框一次」这条路上，最多的摩擦是**重新拖框**：手一抖就
        框歪，于是又白扫一轮。同一块屏幕、同一段动图，区域根本没变，记住坐标
        直接重扫就行。
        """
        if not self._last_phys:
            return self.grab_region(resume=True)
        try:
            from PIL import ImageGrab
        except Exception:  # noqa: BLE001
            return self.grab_region(resume=True)
        bbox = self._last_phys
        self._show_rescan(False)
        gap = self._missing_total(self._scan_texts)
        self._log("· 再来一轮：还是上次那块区域 %d×%d @ (%d,%d)，不用重新框"
                  % (bbox[2] - bbox[0], bbox[3] - bbox[1], bbox[0], bbox[1]))
        if self._scan_texts:
            eta = self._scan_eta(gap)
            self._log("  已收 %d 帧，这轮只补缺的 %d 帧；缺的可能在任何位置，"
                      "所以要完整播一轮%s，见尾帧就停"
                      % (len(self._scan_texts), gap,
                         ("（约 %s）" % _fmt_eta(eta)) if eta else ""))
            self._tune_fps_for_speed()
            ph = float(getattr(self, "_scan_phase", 0.0) or 0.0)
            if ph:
                # 相位不再是随机的：上一轮结束就算好了该挪多少（二分序列），
                # 播到首帧标识那一刻开始按新相位采样，缺口正好被劈成两半。
                self._log("  采样相位挪 %.2f 个周期（缺口二分序列的 1/%d）"
                          % (ph, int(round(1.0 / ph))))
        self._win_home = "+%d+%d" % (self.root.winfo_x(), self.root.winfo_y())
        self._win_moved = False
        try:
            self.root.iconify()            # 保留任务栏图标，中途能点回来
        except tk.TclError:
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

        # 先把两遍算出来的几何尺寸落定，再进去兜底；
        # 顺序反了的话这一步会把兜底刚收掉的高度又顶回去。
        if best:
            w, h, x, y = best
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
            self.root.minsize(w, h)
            self.root.update_idletasks()

        # 兜底：直接用 Win32 的外框矩形和 Win32 的工作区比。
        # 为什么还要这一步：打包成 exe 后进程的 DPI 感知和源码运行时不一定相同，
        # Tk 报的逻辑像素与 SPI_GETWORKAREA 给的数可能差着一层缩放 —— 两边都是
        # Windows 直接给的数，只有它们之间的比较一定同坐标。实测 exe 底边压到
        # 任务栏下面 89px，就是这么来的。
        for _ in range(14):
            # 必须 update() 而不是 update_idletasks()：改窗口大小是异步的，
            # 只跑 idle 任务时 GetWindowRect 还是旧尺寸，会误判成「没超出」。
            self.root.update()
            frame = self._frame_rect()
            if not frame or frame[3] <= bottom:
                break
            # minsize 会挡住收缩，兜底前先放开
            self.root.minsize(640, 480)
            if self._log_lines <= 2:
                # 日志已经退到底了还不下，只能整体收高度
                h = max(480, h - (frame[3] - bottom))
                self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
                self.root.update()
                break
            self._log_lines -= 1
            self.log.configure(height=self._log_lines)
            self.root.update()
            h = min(self.root.winfo_reqheight(), limit_h)
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
            self.root.update()
        self._sync_opt_scroll()
        # 窗口真正显示出来以后再校一次：ttk 主题切换、DnD 包装、字体回退都
        # 可能让选项区比刚才量到的再高几个像素 —— 那就会莫名其妙冒出滚动条。
        try:
            self.root.after(120, self._settle_opt_height)
        except tk.TclError:
            pass

    def _settle_opt_height(self):
        """右边选项区宁可让窗口长高一点，也不要冒出滚动条。

        用户要的是「把空间还给日志」，不是「多一个滚动条」：日志行数已经按
        屏幕算好了，这里只处理「显示出来发现差几个像素」的收尾。
        顺序是：先让窗口长高（工作区还够的话）→ 再少几行日志 → 实在不行才滚动。
        """
        try:
            bbox = self.opt_canvas.bbox("all")
            if not bbox:
                return
            need = bbox[3] - bbox[1]
            if need <= self.opt_canvas.winfo_height() + 1:
                self._sync_opt_scroll()
                return
            short = need - self.opt_canvas.winfo_height() + 2
            left, top, right, bottom = self._work_area()
            frame = self._frame_rect()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            x = self.root.winfo_rootx()
            y = self.root.winfo_rooty()
            if frame and (bottom - frame[3]) >= short:
                # 工作区还有余量：直接把窗口加高，选项区就完整了
                h += short
                self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
                self.root.minsize(w, h)
                self.root.update_idletasks()
                bbox = self.opt_canvas.bbox("all")
                if bbox and (bbox[3] - bbox[1]) <= self.opt_canvas.winfo_height() + 1:
                    self._sync_opt_scroll()
                    return
            # 已经贴到工作区底边：从日志里扣，最多扣到 4 行（再少就没意义了）。
            # 窗口高度不动 —— 日志少占几行，pack 自然把空出来的高度还给上面。
            while self._log_lines > 4:
                self._log_lines -= 1
                self.log.configure(height=self._log_lines)
                self.root.update_idletasks()
                bbox = self.opt_canvas.bbox("all")
                if bbox and (bbox[3] - bbox[1]) <= self.opt_canvas.winfo_height() + 1:
                    break
            self._sync_opt_scroll()
        except tk.TclError:
            pass

    def _frame_rect(self):
        """窗口外框的矩形 (left, top, right, bottom)；取不到返回 None。"""
        try:
            import ctypes
            import ctypes.wintypes as wt

            u32 = ctypes.windll.user32
            hwnd = self.root.winfo_id()
            frame = u32.GetParent(hwnd) or hwnd
            rect = wt.RECT()
            if u32.GetWindowRect(frame, ctypes.byref(rect)):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:  # noqa: BLE001
            pass
        return None

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
        """打开输出文件夹，并**选中**刚刚生成的那个文件。

        只用 os.startfile 打开目录的话，资源管理器会沿用上一次的选中项 ——
        用户看到高亮的不是刚生成的文件，以为生成错了。这里走
        ``explorer /select,`` 精确选中；文件已经不在了就退回只开目录。
        """
        path = self.last_out_dir or self.out_dir.get().strip() or None
        if not path or not os.path.isdir(path):
            path = self.pick_out_dir()
            if not path:
                return
        target = self.last_out_file
        if target and os.path.isfile(target):
            try:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
                return
            except Exception:  # noqa: BLE001 - 退回到「只开目录」
                pass
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

    def grab_region(self, resume: bool = False):
        """框选屏幕区域 → 截图 → 识别二维码 → 直接还原。

        比「先截图存文件再选文件」少两步：屏幕上有码，拖一个框就完事。
        **多屏**：遮罩铺满整个虚拟桌面，主屏/副屏都能框，跨屏拖框也支持；
        抓图按虚拟桌面坐标取（副屏在左边导致的负坐标同样正确）。

        ``resume=False``（默认，菜单里的「框选屏幕区域」）= **重新开始**：上一次
        攒下的帧和临时抓图先清掉。以前这里也是「接着上次」，用户拿一份**新文件**
        来框，上一份的帧会一起拼进去 —— 老文件碰巧已经齐了才没出事，换个文件就
        直接混在一起。``resume=True`` 只给「再来一轮 / 重新框选（接着收同一份）」
        用：那才是真的要续扫。
        """
        try:
            from PIL import ImageGrab
        except Exception as exc:  # 没有 Pillow
            messagebox.showerror(APP_TITLE, "缺少 Pillow，无法截图：%s" % exc)
            return

        # 主窗口往往铺得很宽，留在屏幕上会正好压住要圈的码（用户实测：窗口太大，
        # 遮罩底下还是窗口自己）。框选期间先**最小化**，识别完再放回来。
        # 用 iconify 而不是 withdraw：withdraw 之后任务栏里连影子都没有，用户
        # 中途想回来点「停止」都无从下手；最小化则一直留着任务栏图标。
        mons = self._monitors()
        vx0, vy0, vw0, vh0 = self._virtual_screen()
        if self._scan_texts and not resume:
            self._drop_scan_accum("重新框选")
        if self._scan_texts:
            # 把「复用」这件事说清楚：用户最怕的是又从头数一遍。这里明说已收多少、
            # 只补多少，以及为什么必须再看完整一轮（缺的那帧可能在任何位置）。
            gap = self._missing_total(self._scan_texts)
            eta = self._scan_eta(gap)
            self._log("· 接着上次：已收 %d 帧，%s。这次框选只补缺的 %d 帧 —— "
                      "已收的一帧都不会重收；缺的那几帧可能在任何位置，"
                      "所以还得让动图完整播一轮%s，看到尾帧就停"
                      % (len(self._scan_texts),
                         self._missing_frames(self._scan_texts) or "已经齐了",
                         gap, ("（约 %s）" % _fmt_eta(eta)) if eta else ""))
        self._log("· 准备框选：虚拟桌面 %d×%d @ (%d,%d)，%d 块屏 —— "
                  "主窗口已最小化到任务栏（点图标可随时回来），识别完自动恢复"
                  % (vw0, vh0, vx0, vy0, len(mons) or 1))
        self._win_home = "+%d+%d" % (self.root.winfo_x(), self.root.winfo_y())
        self._win_moved = False

        top, canvas, vx, vy, vw, vh = self._build_overlay()
        # 遮罩铺好之后再最小化主窗口：顺序反过来（先 iconify 再建 Toplevel）在
        # 某些 Windows 主题下会让遮罩跟着父窗口一起被收起。
        try:
            self.root.iconify()
        except tk.TclError:
            self.root.withdraw()
        self.root.update_idletasks()
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
        """把主窗口放回来（扫描期间是最小化状态）。重复调用无害。"""
        self._scan_active = False
        self._drop_hud()
        try:
            if self._win_moved and self._win_home:
                self.root.geometry(self._win_home)     # 回到用户原来的位置
            self._win_moved = False
            self._win_home = None
            try:
                self.root.attributes("-alpha", 1.0)    # 避让时可能压过透明度
            except tk.TclError:
                pass
            if self.root.state() != "normal":
                self.root.deiconify()
            if self._win_zoomed:                       # 扫描前是最大化，还原回去
                self.root.state("zoomed")
            self._win_zoomed = False
            self.root.lift()
        except tk.TclError:
            pass

    def _avoid_scan_bbox(self):
        """扫描期间用户把窗口点回来了 —— 挪开，而不是再把它藏起来。

        以前是「一发现窗口露头就 withdraw」，用户从任务栏点回来的窗口不到一秒
        又消失，等于点了没反应：既看不了日志，也点不到停止。现在改成挪到框外，
        扫描继续跑；实在挤不开才提示用户自己拖开。
        """
        if not getattr(self, "_scan_active", False):
            return
        box = getattr(self, "_scan_bbox", None)
        if not box:
            return
        now = time.monotonic()
        if now - getattr(self, "_avoid_at", 0.0) < 0.4:   # 120ms 轮询一次，别查太勤
            return
        self._avoid_at = now
        try:
            if self.root.state() == "iconic":            # 还最小化着，什么都不用做
                return
            if self.root.state() == "zoomed":            # 最大化了就挪不动，先还原
                self._win_zoomed = True
                self.root.state("normal")
                self.root.update_idletasks()
            if self.root.state() != "normal":
                return
            self.root.update_idletasks()
            w = max(240, self.root.winfo_width())
            h = max(180, self.root.winfo_height())
            scale = getattr(self, "_scan_scale", 1.0) or 1.0
            # 扫描区域换成逻辑坐标：窗口位置用的是这一套单位
            sx1, sy1, sx2, sy2 = (int(round(v / scale)) for v in box)
            x, y = self.root.winfo_x(), self.root.winfo_y()
            if not _overlaps(x, y, w, h, (sx1, sy1, sx2, sy2)):
                return                                   # 已经在框外，别动它
            spot = _free_spot(w, h, (sx1, sy1, sx2, sy2), self._monitors())
            if spot is None:
                if not getattr(self, "_avoid_warned", False):
                    self._avoid_warned = True
                    self._log("· 窗口正压在框选区域上，可能会挡住二维码 —— "
                              "请把它拖开，或点窗口右上角「—」再最小化")
                return
            self.root.geometry("+%d+%d" % spot)
            self._win_moved = True
            self._log("· 窗口已挪到 (%d,%d) 避开二维码 —— 扫描照常进行，"
                      "要停就点角落小条上的「停止」" % spot)
        except tk.TclError:
            pass

    # ---------------- 扫描期间的小悬浮条 ----------------

    def _make_hud(self, bbox):
        """扫描期间在框外角落放一条极小的状态条：进度 + 透明度 + 回主窗口 + 停止。

        整套扫描可能跑几分钟（978 帧的动图按 3 帧/秒播一轮就是 326 秒），
        这期间主窗口最小化在任务栏 —— 没有它就既看不到进度、也没法中止。

        **可以拖**（按住文字拖走），**透明度可调**（右上角滑杆，35%~100%），
        位置和透明度会记住，下次扫描沿用。自动躲开框选区域，免得它自己挡码；
        记住的位置如果正好压在码上，也自动换到没被框住的角。
        """
        try:
            vx, vy, vw, vh = self._virtual_screen()
            w, h = 440, 70
            alpha, saved = _load_hud_cfg()
            place = None
            if saved:
                px, py = int(saved[0]), int(saved[1])
                inside = vx - w < px < vx + vw and vy - h < py < vy + vh
                free = not (px + w > bbox[0] and px < bbox[2]
                            and py + h > bbox[1] and py < bbox[3])
                if inside and free:
                    place = (px, py)
            if place is None:
                spots = [(vx + 16, vy + 16), (vx + vw - w - 16, vy + 16),
                         (vx + 16, vy + vh - h - 16),
                         (vx + vw - w - 16, vy + vh - h - 16)]
                for sx, sy in spots:
                    if sx + w <= bbox[0] or sx >= bbox[2] or sy + h <= bbox[1] or sy >= bbox[3]:
                        place = (sx, sy)
                        break
            if place is None:
                return None
            hud = tk.Toplevel(self.root)
            hud.overrideredirect(True)
            hud.attributes("-topmost", True)
            try:
                hud.attributes("-alpha", alpha)
            except tk.TclError:
                pass
            hud.geometry("%dx%d+%d+%d" % (w, h, place[0], place[1]))
            hud.configure(bg="#1f2430")

            # ---- 拖动：按住标题/状态文字拖走（不绑在按钮上，免得点停止时误移）----
            drag = {"x": 0, "y": 0, "moved": False}

            def start_drag(e):
                drag["x"], drag["y"] = e.x_root - hud.winfo_x(), e.y_root - hud.winfo_y()
                drag["moved"] = False

            def do_drag(e):
                drag["moved"] = True
                hud.geometry("+%d+%d" % (e.x_root - drag["x"], e.y_root - drag["y"]))

            def end_drag(_e=None):
                if drag["moved"]:
                    _save_hud_cfg(alpha_var.get(), (hud.winfo_x(), hud.winfo_y()))

            top = tk.Frame(hud, bg="#1f2430")
            top.pack(fill="x", padx=10, pady=(6, 0))
            title = tk.Label(top, text="正在连拍收帧 · 按住这里可拖动",
                             bg="#1f2430", fg="#9fb3d1", font=("Microsoft YaHei UI", 8))
            title.pack(side="left")

            # ---- 透明度：右上角小滑杆，35% ~ 100% ----
            alpha_var = tk.DoubleVar(value=alpha)
            tk.Label(top, text="透明", bg="#1f2430", fg="#6b7c96",
                     font=("Microsoft YaHei UI", 8)).pack(side="right")
            scale = tk.Scale(top, from_=1.0, to=0.35, resolution=0.05,
                             orient="horizontal", variable=alpha_var, showvalue=0,
                             length=76, width=8, sliderlength=12, bd=0,
                             highlightthickness=0, bg="#1f2430", fg="#9fb3d1",
                             troughcolor="#2c3444", activebackground="#4a5468",
                             command=lambda v: hud.attributes("-alpha", float(v)))
            scale.pack(side="right", padx=(0, 4))

            row = tk.Frame(hud, bg="#1f2430")
            row.pack(fill="x", padx=10, pady=(2, 6))
            self._hud_text = tk.StringVar(value="已收 0 帧")
            # 按钮先 pack：pack 按顺序分配空间，状态文字一长就会把后 pack 的控件
            # 挤成 0 宽 —— 「停止」按钮要是被挤没了，这个条就白放了。
            tk.Button(row, text="停止", command=self._stop_scan, relief="flat",
                      bg="#3b4356", fg="#ffffff", activebackground="#4a5468",
                      font=("Microsoft YaHei UI", 8), padx=8, cursor="hand2"
                      ).pack(side="right")
            tk.Button(row, text="主窗口", command=self._show_main_window, relief="flat",
                      bg="#2c3444", fg="#ffffff", activebackground="#3b4356",
                      font=("Microsoft YaHei UI", 8), padx=8, cursor="hand2"
                      ).pack(side="right", padx=(0, 6))
            status = tk.Label(row, textvariable=self._hud_text, bg="#1f2430",
                              fg="#ffffff", font=("Microsoft YaHei UI", 9), anchor="w")
            status.pack(side="left", fill="x", expand=True)

            # 只给「标题 / 状态 / 空背景」绑拖动，**不绑 hud 本身**。
            # Tk 的 bindtags 里 Toplevel 是每个子控件的祖先标签，绑在 hud 上等于
            # 把滑杆、按钮的按下也当成拖动 —— 拖透明度时窗口跟着鼠标乱跑，
            # 看起来就是「调透明度会反复弹出窗口」。
            for wid in (title, status, top, row):
                wid.bind("<ButtonPress-1>", start_drag)
                wid.bind("<B1-Motion>", do_drag)
                wid.bind("<ButtonRelease-1>", end_drag)
            # 滑杆松手时把透明度一起记住（拖动过程中不写盘，省得每秒写几十次）
            scale.bind("<ButtonRelease-1>", end_drag)
            self._hud_alpha = alpha_var
            self._hud_place = place
            # 宽度按内容自己定：见过「已收 3803/3823 帧（本轮 +3803）· 还要 1 分钟」
            # 被 440px 的固定宽度裁掉尾巴。overrideredirect 的窗口没有边框可拖，
            # 那就让它随文字长 —— 这也算「能调整大小」。
            # 先按**最长可能出现的文字**量一次，之后只增不减，免得每次刷新都抖一下。
            sample = "已收 9999/9999 帧（本轮 +9999）· 还要 99 分 59 秒"
            keep = self._hud_text.get()
            self._hud_text.set(sample)
            self._fit_hud(hud)
            self._hud_text.set(keep)
            return hud
        except tk.TclError:
            return None

    def _fit_hud(self, hud=None, grow_only: bool = False):
        """让悬浮条按内容自动定宽定高（上限是虚拟桌面宽 - 40，绝不跑出屏幕）。

        ``grow_only``：只许变宽不许变窄。文字每次刷新长短不一（「还要 1 分 05 秒」
        vs 「就快好了」），跟着缩会让条子左右抖。
        """
        hud = hud or getattr(self, "_hud", None)
        if hud is None:
            return
        try:
            hud.update_idletasks()
            vx, vy, vw, vh = self._virtual_screen()
            w = int(min(max(360, hud.winfo_reqwidth() + 6), max(360, vw - 40)))
            h = int(min(max(62, hud.winfo_reqheight() + 4), max(62, vh - 40)))
            if grow_only:
                w = max(w, getattr(self, "_hud_w", 0) or 0)
                h = max(h, getattr(self, "_hud_h", 0) or 0)
            self._hud_w, self._hud_h = w, h
            if hud.winfo_width() != w or hud.winfo_height() != h:
                hud.geometry("%dx%d+%d+%d" % (w, h, hud.winfo_x(), hud.winfo_y()))
        except tk.TclError:
            pass

    def _show_main_window(self):
        """从悬浮条把主窗口叫回来（会自动挪到框外，不挡码）。"""
        try:
            self.root.deiconify()
            self.root.lift()
            self._avoid_at = 0.0            # 立刻避让一次，别等下一拍
            self._avoid_scan_bbox()
        except tk.TclError:
            pass

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
        self._scan_scale = scale       # 挪窗口时要把物理框换回逻辑坐标
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
            return 10.0

    def _run_scan(self, bbox, ImageGrab):
        """真正开始连拍（``bbox`` 已经是物理像素坐标）。"""
        self._scan_cancelled = False
        self._scan_active = True
        self._scan_bbox = bbox
        self._avoid_at = 0.0
        self._avoid_warned = False
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
        self._log("· 主窗口已最小化到任务栏 —— 中途想看日志点任务栏图标就行，"
                  "窗口会自动挪开避开二维码，不影响扫描")
        # 抓屏走 ``screengrab``：有独显的机器自动用 DXGI 桌面复制（帧本来就在显存
        # 里，只裁框内那块），否则退回 GDI 区域 BitBlt。两条路都比 ``ImageGrab``
        # 快得多 —— 后者每次都把整个虚拟桌面抓下来再裁。
        # 抓屏再套一层 Prefetcher 挪到后台线程：主循环只等解码，枪数上限从
        # 「1/(抓屏+解码)」变成「1/解码」。
        last = {"t": 0.0}

        def _prefetch(fn):
            """抓屏进后台线程；建不出来就原样返回（功能不受影响，只是慢点）。"""
            try:
                return screengrab.Prefetcher(fn)
            except Exception:  # noqa: BLE001
                return fn

        def _close(p):
            close = getattr(p, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

        def report(st):
            # 日志别刷屏：3 秒一行，且只在真有新帧时才说。978 帧的动图要跑几分钟，
            # 原来 0.5 秒一行能刷出六十多行，把有用信息全冲没了。
            if st["frames"] and st["elapsed"] - last["t"] >= 3.0:
                last["t"] = st["elapsed"]
                need = st.get("need")
                # 收多了（屏幕上同一帧被认成两个不同文本）就别显示「986/983」，
                # 那种数字只会让人怀疑程序算错了。
                got = st["frames"]
                if need and got > need:
                    got = need
                text = ("已收 %d/%d 帧" % (got, need)) if need \
                    else ("已收 %d 帧" % got)
                if st.get("fresh"):
                    text += "（本轮 +%d）" % st["fresh"]
                eta = st.get("eta")
                tail = _fmt_eta(eta)          # 「还要 1 分 40 秒」/「快好了」
                self.queue.put(("hud", "%s · %s" % (text, tail)))
                self.queue.put(("log", "  %s（%d 枪 · 已用 %.0f 秒 · %s）"
                                % (text, st["shots"], st["elapsed"], tail)))

        def worker():
            # 选路 + 计时放在后台做：第一次要起子进程自检 DXGI，约 1 秒，
            # 在界面上卡一下还不如让它悄悄在后台完成。
            grabber = None
            try:
                grabber = screengrab.pick_grabber(bbox)
            except Exception:  # noqa: BLE001
                grabber = None
            base = grabber.grab if grabber is not None else None
            if base is None:
                self.queue.put(("log", "· 抓屏：整屏截图（较慢，可能是远程桌面或权限受限）"))
                base = lambda: ImageGrab.grab(bbox=bbox, all_screens=True)
            else:
                self.queue.put(("log", "· 抓屏：%s —— 采样够快才追得上动图播放"
                                % grabber.info))
            prefetch = _prefetch(base)

            def grab():
                img = prefetch()
                if img is None:
                    try:
                        img = ImageGrab.grab(bbox=bbox, all_screens=True)
                    except Exception:  # noqa: BLE001
                        img = None
                return img

            try:
                texts, stats = qr.scan_region(
                    grab, on_progress=report,
                    should_stop=lambda: self._scan_cancelled,
                    frames_per_sec=fps,
                    # 上一轮算出来的最优相位（见 qr.plan_jitter）：每补一轮都把
                    # 采样网格平移这么多，缺口才对半劈而不是原地打转。
                    phase_ratio=float(getattr(self, "_scan_phase", 0.0) or 0.0),
                    # 之前几轮收到的帧一起带进去：续扫只补缺的，收齐立刻停
                    known=list(self._scan_texts))
            except Exception as exc:  # noqa: BLE001
                _close(prefetch)
                self.queue.put(("call", lambda e=exc: self._scan_failed(e)))
                return
            _close(prefetch)
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
            self._scan_rate = 0.0
            self._scan_phase = 0.0
            self._scan_plan = None
        self._scan_texts.extend(fresh)
        self._log("· 本轮扫描：%d 枪 / %.1f 秒 / %d 轮，新收 %d 帧（累计 %d 帧）"
                  % (stats["shots"], stats["elapsed"], stats.get("cycles", 0),
                     len(fresh), len(self._scan_texts)))
        # 实测收帧速度：这是判断「到底卡在哪」的唯一硬指标。
        # 程序这边抓图 + 解码只要 20ms 一枪（约 45 枪/秒），收得慢基本是
        # **播放器**没跑到设定的帧率（看图软件缩放、GIF 解码慢、窗口被挡）。
        span = stats["elapsed"]
        if span >= 1.0:
            rate = len(fresh) / span
            want = self._scan_fps()
            if fresh:
                # 只记「真有新帧」那几轮的速度：补录时一帧没补到的轮次会把均值拉成 0
                self._scan_rate = rate
            if not fresh and len(self._scan_texts) > 1:
                # 补录时一帧都没补到，跟「播放器跟不上」完全是两回事：说明这轮刚好
                # 没转到缺的那几帧。以前一律报「播放器跟不上」，用户就去改帧率、
                # 换播放器，白折腾 —— 其实再点一次「再来一轮」就行。
                why = "这轮没转到缺的那几帧（缺帧可能在任何位置），再点一次「再来一轮」"
            elif rate >= want * 0.7:
                why = "正常"
            else:
                why = "播放器跟不上，改小帧率或换个播放器（浏览器最稳）"
            self._log("· 实测收帧 %.1f 帧/秒（设定 %.0f）：%s" % (rate, want, why))

        missing = self._missing_frames(self._scan_texts)
        if missing:
            gap = self._missing_total(self._scan_texts)
            self._show_rescan(True, gap)
            self._log("· %s —— 点「再来一轮」只补这 %d 帧，已收的不会重收"
                      % (missing, gap))
            # 抖动建议：把「下一轮挪多少相位、一共还要几轮」直接算给用户看。
            # 以前只说「缺 12 帧」，用户只能一轮一轮盲试，帧数越多越试不出来。
            plan = stats.get("jitter") or qr.plan_jitter(
                self._scan_texts,
                sample_fps=(stats["shots"] / stats["elapsed"]
                            if stats.get("elapsed") else 0.0),
                player_fps=stats.get("player_fps"),
                rounds_done=max(1, stats.get("cycles", 0) or 1))
            if plan:
                self._scan_phase = float(plan.get("next_ratio") or 0.0)
                self._scan_plan = plan
                if plan.get("advice"):
                    self._log("· 抖动建议：%s" % plan["advice"])
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
                "不用重新拖框（只有框歪了才需要「重新框选」）。\n\n"
                "实在补不上又不想留着，点「放弃本次识别」会把已收的帧和临时抓图"
                "一起清掉，下次从零开始。"
                % missing)
            return

        texts, self._scan_texts = list(self._scan_texts), []
        self._scan_rate = 0.0
        self._show_rescan(False)
        # 还原成功 = 这些帧再也用不上了：内存里的帧清空，临时目录里的诊断抓图删掉
        # （用户提过「还原完那些图可以删」）。只删本程序自己写的那批，别的文件不动。
        shots, freed = clean_scan_shots()
        if shots:
            self._log("· 已清理 %d 张框选抓图，释放 %s（%s）"
                      % (shots, _human(freed), "还原成功后这些就没用了"))
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

    def _missing_total(self, texts) -> int:
        """还差几帧（数字），给按钮文案用。"""
        try:
            segs = ct.parse_segments("\n".join(texts))
        except Exception:  # noqa: BLE001
            return 0
        if not segs:
            return 0
        need, got = {}, {}
        for seg in segs:
            need[seg.doc_id] = seg.total
            got.setdefault(seg.doc_id, set()).add(seg.index)
        return sum(max(0, total - len(got.get(doc_id, ())))
                   for doc_id, total in need.items())

    def _scan_eta(self, missing: int) -> float | None:
        """估算「再来一轮」还要多久：整段动图播一轮 ≈ 总帧数 ÷ 实测收帧速度。

        耗时由**总帧数**决定，不是缺几帧 —— 缺的那帧可能在第 1 帧也可能在
        最后一帧，只能让动图从头播到尾、见尾帧才敢停。所以「只缺 1 帧」照样
        要等一整轮，这点必须提前说明，否则用户会以为程序在偷懒。

        速度优先用**上一轮实测**的：屏幕上那段动图是播放器在放，实际可能比
        设定值快好几倍（实测设定 10，真跑出 18~58 帧/秒 —— 有些播放器根本
        不理 GIF 的每帧延时）。拿设定值算出来的「还要 6 分 22 秒」纯属吓人。
        """
        total = len(self._scan_texts) + max(0, missing)
        if total < 2:
            return None
        rate = max(0.5, float(getattr(self, "_scan_rate", 0.0) or 0.0)) \
            if getattr(self, "_scan_rate", 0.0) else self._scan_fps()
        return total / rate

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
        self.last_out_file = results[0][0]
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

    def _on_qr_click(self):
        """用户点了「生成」复选框：勾上切预设 qr，取消切回 chars。

        单独一个回调、不塞进 ``_on_qr_toggle``：那个会被 ``_sync_mode`` 反复调用
        （切打包/还原时），放里面会把用户手选的预设冲掉。
        """
        self._apply_preset_key("qr" if self.qr_enabled.get() else "chars")
        self._on_qr_toggle()

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

    def _apply_preset_key(self, key: str):
        """按预设 key 同步下拉框和编码器/压缩器（不触发 _on_preset 之外的分支）。"""
        if key not in ct.PRESETS:
            return
        label = self._preset_label(key)
        self.preset.set(label)
        try:
            self.preset_box.set(label)
        except (AttributeError, tk.TclError):
            pass
        cfg = ct.PRESETS[key]
        self.codec.set(cfg["codec"])
        self.compress.set(cfg["compress"])

    @staticmethod
    def _preset_label(key: str) -> str:
        for label in ("qr（单张码装最多）", "chars（字数最少）",
                      "compact（ASCII 体积最小）", "safe（纯大写+数字，耐复制）",
                      "fast（速度快）", "plain（不压缩）", "自定义"):
            if label.split("（")[0].strip() == key:
                return label
        return key

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

    @staticmethod
    def _short_phase(phase):
        """阶段名只取最后一段：``合成动图·写入 WebP`` → ``写入 WebP``。

        右侧那点宽度放不下长名字，写全了就被窗口裁掉半截；真正想看的是
        「现在在干哪一步」，前面的「合成动图」日志里已经说过了。
        """
        return (phase or "").split("·")[-1].strip() or "处理"

    def _with_file_pos(self, text):
        """多文件时把「第几个」塞进文字里，省掉一整条进度条。"""
        idx, total = getattr(self, "_file_pos", (0, 0))
        if total > 1 and idx:
            return "%d/%d · %s" % (idx, total, text)
        return text

    def _start_bar(self):
        """换一个文件/换一个阶段：细分进度条归零，重新开始计时。"""
        self._bar_t0 = time.monotonic()
        self._bar_tick = self._bar_t0
        self._bar_logged = -1
        self._bar_phase = None
        self._bar_last = (0, 1, "", False)
        try:
            if getattr(self, "_bar_writing", False):
                self.sub_progress.stop()
                self.sub_progress.configure(mode="determinate")
                self._bar_writing = False
            self.sub_progress.configure(maximum=1, value=0)
            self._set_bar_text("")
        except tk.TclError:
            pass

    #: 这些阶段是「PIL 内部一次性写完」的：拿不到中途进度，只能走动画条 + 估时
    ONE_SHOT_PHASES = ("写入 GIF",)

    def _set_bar(self, done, total, phase="", hide_count=False):
        """刷新当前文件的帧级进度（工作线程经队列派过来，只在主线程执行）。

        ``hide_count``：进度刻度和真实帧数无关（打包流水线用 0~1000 的合成刻度），
        这时只显示百分比，别把「(350/1000)」这种内部数字摆给用户看。
        """
        total = max(1, int(total))
        done = max(0, min(int(done), total))
        self._show_bars(True)          # 有活干了才把进度条露出来
        self._bar_last = (done, total, phase, hide_count)
        self._bar_tick = time.monotonic()
        if phase != getattr(self, "_bar_phase", None):
            # 换阶段（排版 → 渲染 → 写入）时把「每 10% 记一行」清零，
            # 否则后一个阶段的日志一行都留不下来
            self._bar_phase = phase
            self._bar_logged = -1
        # 「写入 GIF」是 PIL 内部一次性压完所有帧，没法按帧报进度 ——
        # 进度条切成来回滚动的动画，让人看出还在动，而不是停在 100% 假死。
        writing = any(tag in (phase or "") for tag in self.ONE_SHOT_PHASES)
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
            self._set_bar_text(self._writing_text(phase, total, 0.0))
            return
        pct = int(done * 100 / total)
        if hide_count:
            text = "%s %d%%" % (self._short_phase(phase), pct)
        else:
            text = "%s %d%% (%d/%d)" % (self._short_phase(phase), pct, done, total)
        elapsed = self._bar_tick - self._bar_t0 if self._bar_t0 else 0.0
        if elapsed >= 1.0:          # 刚起步那一瞬间显示「· 0.0s」没有意义
            text += " %.1fs" % elapsed
        self._set_bar_text(self._with_file_pos(text))
        # 日志每 10% 留一行：事后翻日志能看出「哪一步花了多久」，也能确认没卡死
        if pct // 10 > self._bar_logged:
            self._bar_logged = pct // 10
            self._log("   %s %d%%（%d/%d）" % (phase or "处理", pct, done, total))

    def _writing_text(self, phase: str, total: int, elapsed: float = 0.0) -> str:
        """一次性写入类阶段的文案：给「已用多久」和「大约还要多久」。

        957 帧的 WebP 要写三十多秒，光停在 100% 不动，用户只能猜是不是死了。
        ``_webp_rate`` 是上一次实测的「秒/帧」，跑过一次就准了。
        """
        n = max(1, int(total or 1))
        rate = float(getattr(self, "_webp_rate", 0.0) or 0.0) or 0.012
        est = n * rate
        used = ("已 %.0fs，" % elapsed) if elapsed >= 1 else ""
        if est >= 15:
            left = max(1.0, est - elapsed)
            return "%s…%s%d 帧，大约还要 %.0f 秒" % (
                self._short_phase(phase), used, n, left)
        return "%s…%s%d 帧" % (self._short_phase(phase), used, n)


    def _set_bar_text(self, text: str):
        """写进度文字：太长自动省略，顺手把进度条宽度重算一遍。

        重算是必须的 —— 文字变宽了，条子得让位；不然文字又被挤出窗口。
        """
        try:
            self.prog_text.configure(text=self._elide_bar_text(text))
            self._fit_bar_width()
        except tk.TclError:
            pass

    def _bar_heartbeat(self):
        """一秒一跳的心跳：长任务里进度不动时，至少让人看出还在跑。"""
        if self._scan_active:
            # 扫描期间主窗口保持最小化；用户从任务栏点回来时**不**强行再藏起来
            # ——以前那样做，点回来的窗口不到一秒又消失，等于点不动。现在改成
            # 把它挪到框外，扫描照常继续。
            self._avoid_scan_bbox()
        if not self._bar_t0 or not (self.worker and self.worker.is_alive()):
            return
        now = time.monotonic()
        if now - self._bar_tick < 1.0:
            return
        self._bar_tick = now
        done, total, phase, hide_count = self._bar_last
        elapsed = now - self._bar_t0
        # 一次性写入阶段没有百分比可言，心跳只负责把「已用多久」走起来 ——
        # 以前这里会拿 done/total 覆盖成「100%」，正好把「还要几十秒」盖掉，
        # 用户看到的就是「100% 卡住 30 秒」。
        if getattr(self, "_bar_writing", False):
            self._set_bar_text(self._writing_text(phase, total, elapsed))
            return
        pct = int(done * 100 / max(1, total))
        # 心跳也要走同一套「省略 + 让位」，否则一秒一次的刷新会把条子长度来回跳
        head = ("%s %d%%" % (self._short_phase(phase), pct) if hide_count
                else "%s %d%% (%d/%d)" % (self._short_phase(phase), pct, done, total))
        self._set_bar_text("%s · %.0fs" % (head, elapsed))

    def cancel_run(self):
        """点「中止」：给工作线程发信号，各阶段在帧边界抛 ``qr.Cancelled``。

        为什么要每个阶段都设检查点：出码链路里最长的是 zxing 预排（3825 帧
        约一分钟）和 WebP 逐帧编码，只在入口判一次等于没判 —— 用户点了中止
        还得干等一分钟，感受和「按钮没反应」一样。
        """
        if not (self.worker and self.worker.is_alive()):
            return
        self._cancel.set()
        try:
            self.stop_btn.configure(state="disabled", text="正在中止…")
        except tk.TclError:
            pass
        self._log("· 收到中止请求 —— 把手上这一帧收尾就停，"
                  "随后清掉本次的中间文件")

    def clean_temp(self, silent: bool = False) -> tuple[int, int]:
        """清掉本程序留在 %TEMP% 里的中间文件。

        「中间文件」到底是些什么：框选识别失败时存下的诊断抓图
        （``officepack_框选抓图_*.png``，一张约 200KB）。它们只在排查
        「为什么扫不出」时有用，问题解决了就是纯垃圾 —— 攒几十次能到上百兆。

        为什么要用户主动点一次：程序被强杀（任务管理器结束进程、断电）时，
        收尾代码根本没机会跑，这些图就永久留在临时目录里。给一个按钮，
        用户才能把历史残留一次清干净。

        返回（删了几个, 释放多少字节）。
        """
        removed, freed = clean_scan_shots(None)
        if not silent:
            if removed:
                self._log("🧹 已清理 %d 个历史中间文件，释放 %s"
                          % (removed, _human(freed)))
                messagebox.showinfo(
                    APP_TITLE,
                    "已清理 %d 个中间文件，释放 %s。\n\n"
                    "（都是框选识别的诊断抓图，删掉不影响任何已完成的结果）"
                    % (removed, _human(freed)))
            else:
                self._log("🧹 临时目录里没有本程序留下的中间文件")
                messagebox.showinfo(APP_TITLE, "临时目录很干净，没有需要清理的文件。")
        return removed, freed

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
        self._cancel.clear()
        self._run_t0 = time.monotonic()
        try:
            self.stop_btn.configure(state="normal", text="中止")
        except tk.TclError:
            pass
        self._file_pos = (0, len(self.files))
        self._start_bar()
        self._log("── 开始%s，共 %d 个文件 ──" % ("打包" if mode == "pack" else "还原", len(self.files)))
        files = list(self.files)
        self.worker = threading.Thread(
            target=self._run,
            args=(mode, files, out_dir, password, cipher,
                  self.codec.get(), self.compress.get(), segment,
                  self.qr_enabled.get() and mode == "pack",
                  self.qr_format.get(), self.qr_fps.get(),
                  # Tk 变量只能在主线程读：工作线程里 .get() 会走一次 Tcl 调用，
                  # 主线程不在 mainloop 时直接抛 RuntimeError（实测踩到了）
                  self.webp_mode.get(), self._qr_grid_n()),
            daemon=True,
        )
        self.worker.start()

    def _run(self, mode, files, out_dir, password, cipher, codec_name, compress_mode,
             segment, as_qr=False, qr_format="PNG（单帧 + 总览）", qr_fps="3",
             webp_mode="速度优先", matrix=1):
        t0 = time.monotonic()
        ok = 0
        outputs: list[str] = []
        errors: list[str] = []
        cancelled = False
        # 传给 qr 各阶段的取消回调。``Event.is_set`` 是 C 实现，每帧查一次
        # 约 0.2µs，相对单帧 10ms 的渲染开销可以忽略。
        stop = self._cancel.is_set

        if mode == "unpack":
            # 分段必须整体合并还原，不能逐个文件处理；图片和文本混着给也能一起还原
            try:
                results, stats = qr.unpack_any(files, out_dir, password,
                                               progress=self._qr_progress(),
                                               should_stop=stop)
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
            except qr.Cancelled:
                cancelled = True
                self.queue.put(("log", "· 已中止还原 —— 还没走到写盘，"
                                       "输出目录里不会留下半个文件"))
            except Exception as exc:
                errors.append("还原失败：%s" % exc)
                self.queue.put(("log", "✗ 还原失败：%s" % exc))
            self.queue.put(("tick", len(files)))
            self.queue.put(("done", {"ok": ok, "fail": len(files) - ok,
                                     "outputs": outputs, "errors": errors,
                                     "cancelled": cancelled,
                                     "elapsed": time.monotonic() - t0}))
            return

        for src in files:
            if self._cancel.is_set():
                cancelled = True
                self.queue.put(("log", "· 已中止，剩下 %d 个文件没有处理"
                                % (len(files) - ok - len(errors))))
                break
            name = os.path.basename(src)
            t_file = time.monotonic()
            # 归零要碰 Tk 控件，必须回主线程做（工作线程直接 configure 会抛
            # "main thread is not in main loop"）
            self.queue.put(("call", self._start_bar))
            try:
                if as_qr:
                    self._pack_one_qr(src, name, out_dir, password, cipher,
                                      compress_mode, codec_name, outputs,
                                      qr_format, qr_fps, webp_mode,
                                      matrix=matrix, should_stop=stop)
                    ok += 1
                    self.queue.put(("log", "   本文件用时 %.1f 秒"
                                    % (time.monotonic() - t_file)))
                    continue

                out_path = None
                self.queue.put(("bar", (0, 1000, "压缩加密", True)))
                if out_dir:
                    out_path = os.path.join(
                        out_dir, os.path.basename(ct.default_out_path(src, codec_name))
                    )
                paths, meta = ct.pack_file(
                    src, out_path, codec_name=codec_name,
                    cipher_name=cipher, password=password,
                    compress=compress_mode, segment=max(segment, 0),
                    progress=self._pack_progress(),
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
            except qr.Cancelled as exc:
                cancelled = True
                # 中止前已经落盘的图要算进「已写出」：逐张写 PNG 时异常是从
                # 写盘循环里抛的，光看返回值会误报「没有写出任何文件」
                for path in exc.partial:
                    if path not in outputs:
                        outputs.append(path)
                if exc.partial:
                    self.queue.put(("log",
                        "· 已中止（%s 没做完）—— 已写出的 %d 个文件保留在原处，"
                        "剩下的不会再动" % (name, len(exc.partial))))
                else:
                    self.queue.put(("log",
                        "· 已中止（%s 没做完）—— 这次还没写出文件"
                        % name))
                break
            except Exception as exc:
                errors.append("%s：%s" % (name, exc))
                self.queue.put(("log", "✗ %s：%s" % (name, exc)))
            else:
                self.queue.put(("log", "   本文件用时 %.2f 秒"
                                % (time.monotonic() - t_file)))
            self.queue.put(("tick", 1))
        self.queue.put(("done", {"ok": ok,
                                 # 中止时没轮到的文件不算失败，别在总结里吓人
                                 "fail": 0 if cancelled else len(files) - ok,
                                 "outputs": outputs, "errors": errors,
                                 "cancelled": cancelled,
                                 "elapsed": time.monotonic() - t0}))

    def _pack_progress(self):
        """打包流水线（压缩 → 加密 → 编码）的进度回调。

        这三段是真正的耗时大头（16.5MB：压缩 8s、编码 5s），以前一次都不报，
        界面只能显示「压缩加密 0%」干等。刻度是内部合成的 0~1000，所以
        ``hide_count=True`` —— 别把 350/1000 这种内部量摆给用户看。
        """
        def report(done, total, step=""):
            self.queue.put(("bar", (done, total, step or "压缩加密", True)))
        return report

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

    def _qr_grid_n(self) -> int:
        """当前选的矩阵边长：1（一帧一码）/ 2（2×2）/ 3（3×3）。"""
        try:
            return max(1, min(3, int(self.qr_grid.get()[0])))
        except (TypeError, ValueError, IndexError):
            return 1

    #: 矩阵拼图的单码边长与缝宽，必须和 ``_pack_one_qr`` 渲染时一致
    QR_CODE_PX = 720
    QR_GAP_PX = 12

    def _matrix_display_fit(self, n: int):
        """n×n 矩阵摊到屏幕上，每张码还剩多少像素。

        返回 ``(需要像素, 可用像素, 每码像素, 每模块像素)``，拿不到屏幕尺寸返回
        ``None``。单帧 2945 字节时是 version 40（177 个模块），**每模块至少要
        3 像素**才认得住 —— 这就是「3×3 能播、却只能认出一半」的根因，光看
        「窗口放得下」是不够的。
        """
        try:
            import ctypes

            class _Rect(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            r = _Rect()
            # SPI_GETWORKAREA：去掉任务栏后的可用桌面；进程是 DPI 感知的，
            # 拿到的是物理像素，和抓屏坐标系一致
            if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
                return None
            avail = min(r.right - r.left, r.bottom - r.top)
            if avail <= 0:
                return None
            need = self.QR_CODE_PX * n + self.QR_GAP_PX * (n - 1)
            per = avail / float(n)
            return need, avail, per, per / 177.0
        except Exception:  # noqa: BLE001  非 Windows 就不提示
            return None

    def _warn_matrix_fit(self, n: int) -> None:
        """矩阵放不下/每模块像素不够时，把账算给用户看，并给出更优选择。

        门槛按实测定的（400KB 文件、version 40 的码，按比例缩到各种屏幕尺寸）：
        每模块 ≥2.5px 稳；1.9~2.5px 勉强能解，但**整块正方形必须完整显示**，
        少一行就整行码丢失；<1.7px 开始掉帧。
        """
        fit = self._matrix_display_fit(n)
        if fit is None:
            return
        need, avail, per, ppm = fit
        if ppm >= 2.5:
            self.queue.put(("log",
                            "   %d×%d 矩阵：原图 %dpx，这块屏工作区 %dpx → 每张码 %.0fpx"
                            "（%.1f 像素/模块，够用）"
                            % (n, n, need, avail, per, ppm)))
            return
        better = n - 1
        while better > 1:
            _, _a, p2, ppm2 = self._matrix_display_fit(better)
            if ppm2 >= 2.5:
                break
            better -= 1
        tip = ("建议改用 %d×%d（每码约 %.1f 像素/模块）" % (better, better,
                                                        self._matrix_display_fit(better)[3])
               if better > 1 else "建议改回 1×1")
        if ppm >= 1.9:
            self.queue.put(("log",
                            "   ⚠ %d×%d 原图 %dpx，工作区只有 %dpx：每张码 %.0fpx"
                            "（%.1f 像素/模块，勉强）。播放时**整个正方形必须完整显示**，"
                            "被任务栏或窗口边切掉一条就是整行码丢失 —— 用浏览器 F11 全屏最稳。"
                            "%s会更从容。" % (n, n, need, avail, per, ppm, tip)))
            return
        self.queue.put(("log",
                        "   ⚠ %d×%d 原图 %dpx，工作区只有 %dpx：每张码 %.0fpx"
                        "（%.1f 像素/模块，低于 1.7 会明显掉）。%s，并用浏览器 F11 全屏播放"
                        % (n, n, need, avail, per, ppm, tip)))

    def _pack_one_qr(self, src, name, out_dir, password, cipher, compress_mode,
                     codec_name, outputs, qr_format="PNG（单帧 + 总览）", qr_fps="3",
                     webp_mode="速度优先", matrix=1, should_stop=None):
        """单个文件走二维码通道；返回写出的图片路径。编码器沿用界面选择。"""
        with open(src, "rb") as fh:
            raw = fh.read()
        self.queue.put(("bar", (0, 1000, "压缩加密", True)))
        self.queue.put(("log", "· %s 正在压缩/加密 %s…" % (name, _human(len(raw)))))
        t_pack = time.monotonic()
        pack = ct.pack_bytes(
            raw, name, codec_name=codec_name,
            cipher_name=cipher, password=password,
            compress=compress_mode, preview=False, ascii_only=True,
            progress=self._pack_progress(),
        )
        pack_sec = time.monotonic() - t_pack
        cinfo = pack.meta.get("compress") or {}
        if cinfo.get("probe_skipped"):
            self.queue.put(("log",
                "   已压缩格式（rar/zip/jpg 之类），再压也省不下来 → 跳过压缩"
                "（max 档全量试参数要几十秒，这里只花 0.2 秒试算）"))
        elif cinfo.get("auto_skipped"):
            self.queue.put(("log", "   压缩没有收益，按原样打包"))
        elif cinfo.get("tuned"):
            self.queue.put(("log",
                "   压缩完成（lzma · 参数组 %s）" % cinfo["tuned"]))
        self.queue.put(("log", "   打包 %.1f 秒：%s → %s → %d 字符"
                        % (pack_sec, _human(len(raw)), _human(len(pack.payload)),
                           len(pack.encoded or ""))))
        # 打包（压缩→加密→编码）几百毫秒到几十秒不等，提前中止就别往下出码了：
        # 这时候一张图都还没写，停在这里是「零副作用」的
        qr._check_stop(should_stop)
        frames = qr.build_frames(pack, ec=qr.DEFAULT_EC)
        if matrix > 1 and len(frames) > 1:
            # 矩阵不是越多越好：帧数降到 1/N²，但每张码在屏幕上只剩 1/N 宽。
            # 放不下就先说清楚，别等用户在还原端一头雾水地反复框选。
            self._warn_matrix_fit(matrix)
        target_dir = out_dir or os.path.dirname(os.path.abspath(src))
        want_png = qr_format.startswith("PNG")
        want_single = "仅总览" not in qr_format
        want_gif = "GIF" in qr_format and len(frames) > 1
        want_webp = "WebP" in qr_format          # 一帧也能出静态 WebP
        if not (want_png or want_gif or want_webp):
            # 只选了动图格式，但这份文件一帧就装下了 —— 没有「动」可言。
            # 不能就这么啥也不写，老老实实出一张单帧 PNG。
            want_png = True
            want_single = True
        self.queue.put(("log", "   拆成 %d 张码，输出格式「%s」%s…"
                        % (len(frames), qr_format,
                           "" if want_png else "（只写动图文件，不写单帧 PNG）")))

        res = {"images": [], "sheet": None, "sheets": [], "dir": target_dir}
        if want_png:
            res = qr.render_images(frames, target_dir, name, ec=qr.DEFAULT_EC,
                                   sheet=True, single=want_single,
                                   progress=self._qr_progress("渲染 PNG"),
                                   should_stop=should_stop)
            outputs.extend(res["images"])
            outputs.extend(res.get("sheets") or [])

        fps = 10.0
        try:
            fps = float(qr_fps)
        except (TypeError, ValueError):
            pass
        fps = max(0.5, min(20.0, fps))

        # 矩阵模式下动图帧数变成 1/N²（每帧放 N² 张码），播一轮的时间跟着砍
        anim_n = len(frames) + 2 if len(frames) > 1 else len(frames)
        per_frame = max(1, matrix * matrix)
        if matrix > 1 and len(frames) > 1:
            anim_n = (anim_n + per_frame - 1) // per_frame
        if len(frames) > 1:
            # 把「播一轮要多久」直接算给用户看：这份文件慢在哪，一眼就知道是
            # 帧率拖的，还是生成拖的。
            self.queue.put(("log",
                "   动图 %.0f 帧/秒：%d 帧%s播一轮约 %.0f 秒"
                "（电脑框选建议 10~15；手机扫用 3~6）"
                % (fps, anim_n,
                   "（矩阵 %d×%d，每帧 %d 张码）" % (matrix, matrix, per_frame)
                   if matrix > 1 else "",
                   anim_n / fps)))
        elif matrix > 1:
            self.queue.put(("log", "   只有一帧，矩阵没有意义，按单码输出"))

        # 同时要 GIF + WebP 时只渲染一遍：几百帧的二维码重画两遍纯属浪费
        # （渲染约 10ms/帧，983 帧就是白扔 10 秒）。
        anim_imgs = None
        if matrix > 1 and len(frames) > 1 and (want_gif or want_webp):
            # 3×3 的合成图约 2200px，显示到 1200px 以下每张码就不到 2px/模块了
            need_px = 700 if matrix == 2 else 1250
            self.queue.put(("log",
                            "   矩阵 %d×%d：%d 张码拼成 %d 张大图再出动图"
                            "（每张码 %s，屏幕上要显示到约 %dpx 才认得出）"
                            % (matrix, matrix, len(frames), anim_n,
                               "约一半宽" if matrix == 2 else "约三分之一宽", need_px)))
            if want_gif and want_webp:
                # 两种动图要共用同一批帧，PIL 的 GIF 又必须拿到完整列表，
                # 只能全量留住（内存换时间，用户明确要两份才走这里）
                anim_imgs = list(qr.anim_images(frames, qr.DEFAULT_EC, 720, True,
                                                self._qr_progress("渲染矩阵"),
                                                matrix=matrix,
                                                should_stop=should_stop))
            # 只出一种时**不预渲染列表**：交给 qr 那边流式出帧 + 编码，
            # 3800 帧全铺在内存里会直接卡死（实测「写入 77% 之后再不动」）
        elif want_gif and want_webp:
            self.queue.put(("log", "   GIF 和 WebP 要出两份：帧只渲染一遍，两边共用"))
            anim_imgs = list(qr._anim_source(frames, qr.DEFAULT_EC, 720, True,
                                             self._qr_progress("渲染动图"),
                                             should_stop=should_stop))

        gif_path = None
        if want_gif:
            gif_path = os.path.join(target_dir, "%s.qr.gif" % name)
            qr.render_gif(frames, gif_path, ec=qr.DEFAULT_EC, fps=fps,
                          progress=self._qr_progress("合成动图"), images=anim_imgs,
                          matrix=matrix, should_stop=should_stop)
            outputs.append(gif_path)

        webp_path = None
        if want_webp:
            webp_path = os.path.join(target_dir, "%s.qr.webp" % name)
            if len(frames) > 1:
                # 几百帧的动图 WebP 要压几十秒，这段时间界面上只有一条来回滚的
                # 进度条 —— 把实测耗时打进日志，事后能看出到底慢在哪一步。
                method = qr.webp_method_for(len(frames), webp_mode)
                t_webp = time.monotonic()
                qr.render_webp(frames, webp_path, ec=qr.DEFAULT_EC, fps=fps,
                               progress=self._qr_progress("合成 WebP"), images=anim_imgs,
                               method=method, matrix=matrix, should_stop=should_stop)
                spent = time.monotonic() - t_webp
                # 记住实测速度：下次写动图就能把「大约还要 xx 秒」算准
                self._webp_rate = spent / max(1, anim_n)
                self.queue.put(("log",
                    "   合成 WebP 用了 %.1f 秒（%d 帧 · 无损 · %s · method=%d，约 %.1f ms/帧）"
                    % (spent, anim_n, webp_mode, method,
                       spent * 1000 / max(1, anim_n))))
            else:
                # 一帧就装下：写一张**静态 WebP**。选了 WebP 就给 WebP，
                # 别偷偷换成 PNG —— 用户会以为「根本没有单 WebP 导出」。
                img = qr._render_uniform(frames, qr.DEFAULT_EC, 720)[0]
                img.save(webp_path, "WEBP", lossless=True, quality=100,
                         method=qr.webp_method_for(1, webp_mode))
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
        cancelled = bool(info.get("cancelled"))
        elapsed = float(info.get("elapsed") or 0.0)
        spent = _fmt_spent(elapsed)

        self.go.configure(state="normal", text="开始处理")
        try:
            self.stop_btn.configure(state="disabled", text="中止")
        except tk.TclError:
            pass
        self._cancel.clear()
        self._run_t0 = 0.0
        self._file_pos = (0, 0)
        self._bar_t0 = 0.0
        try:
            self.sub_progress.stop()          # 停掉「来回滚动」的动画，否则一直在重绘
            self.sub_progress.configure(value=0)
            self.prog_text.configure(text="")
        except tk.TclError:
            pass
        self._show_bars(False)         # 收工：进度条收起来（占位高度不变）
        if cancelled:
            self._log("── 已中止：完成 %d 个 · 本次用时 %s ──" % (ok, spent))
        else:
            self._log("── 完成：成功 %d / 失败 %d · 本次用时 %s ──"
                      % (ok, fail, spent))

        if outputs:
            self.last_out_dir = os.path.dirname(outputs[0])
            self.last_out_file = outputs[0]
            self._log("输出文件夹：%s" % self.last_out_dir)
            for path in outputs[:6]:
                self._log("   · %s" % os.path.basename(path))
            if len(outputs) > 6:
                self._log("   · …共 %d 个文件" % len(outputs))
        else:
            self.last_out_dir = self.out_dir.get().strip() or None

        if cancelled:
            # 中止后的收尾：把本次与历史留下的中间文件清掉（抓图每张约 200KB）。
            # 已写出的输出**不删** —— 那是用户能直接用的东西，删了才是损失。
            removed, freed = self.clean_temp(silent=True)
            if removed:
                self._log("· 中间文件已清理：%d 个，释放 %s"
                          % (removed, _human(freed)))
            else:
                self._log("· 没有需要清理的中间文件")
            if outputs:
                ask = ("已中止。\n\n本次用时 %s，已写出 %d 个文件（保留在原处）。\n\n"
                       "中间文件已清理：%d 个 / %s\n\n是否打开输出文件夹？"
                       % (spent, len(outputs), removed, _human(freed)))
                if messagebox.askyesno(APP_TITLE, ask):
                    self.open_out_dir()
            else:
                messagebox.showinfo(
                    APP_TITLE,
                    "已中止。\n\n本次用时 %s，没有写出任何文件。\n"
                    "中间文件已清理：%d 个 / %s" % (spent, removed, _human(freed)))
            return

        if fail:
            detail = "\n".join(errors[:8]) or "未知原因"
            if len(errors) > 8:
                detail += "\n…"
            messagebox.showerror(
                APP_TITLE,
                "处理结束：成功 %d 个，失败 %d 个\n本次用时 %s\n\n失败原因：\n%s"
                % (ok, fail, spent, detail),
            )
            return

        if not outputs:
            messagebox.showwarning(
                APP_TITLE, "没有生成任何文件，请检查输入。\n\n本次用时 %s" % spent)
            return

        where = self.last_out_dir or "(原文件所在目录)"
        ask = ("处理完成：成功 %d 个文件。\n\n本次用时：%s\n\n输出位置：\n%s\n"
               % (ok, spent, where))
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
                    idx, total = self._file_pos
                    self._file_pos = (idx + int(payload), total)
                elif kind == "bar":
                    bars = payload      # 积压时只认最后一条，免得主线程被刷爆
                elif kind == "done":
                    # 收工时把本轮积压的进度消息作废：否则 _finish 打印完
                    # 「── 已中止 ──」之后，循环末尾还会用它再刷一帧
                    # 「渲染 PNG 12%（19/152）」，看着像中止没生效
                    bars = None
                    self._finish(payload)
                elif kind == "hud":
                    try:
                        self._hud_text.set(payload)
                        # 只许变宽：文字长短不一，跟着缩会让条子左右抖
                        self._fit_hud(grow_only=True)
                    except (tk.TclError, AttributeError):
                        pass
                elif kind == "call":
                    payload()          # 工作线程派回主线程的回调（弹窗必须在主线程）
        except queue.Empty:
            pass
        try:
            if bars is not None:
                self._set_bar(*bars)
            self._bar_heartbeat()
        except Exception as exc:  # noqa: BLE001
            # 刷新界面时的一次异常绝不能把轮询打死：一旦 after 不再续期，
            # 队列里后面的日志和进度就全部石沉大海，界面看着就像卡死。
            try:
                self._log("· 界面刷新出错（%s），已跳过这一帧" % exc)
            except Exception:  # noqa: BLE001
                pass
        finally:
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


def _fmt_spent(sec: float) -> str:
    """本次实际用时，说成人话：``8.4 秒`` / ``1 分 23 秒`` / ``1 小时 05 分``。

    用户提的「生成和还原都该显示本次总用时」—— 同一份文件换压缩档、换矩阵、
    换 WebP 档位到底快了多少，只有把总耗时摆在收尾摘要里才比得出来。
    """
    sec = max(0.0, float(sec or 0.0))
    if sec < 60:
        return "%.1f 秒" % sec
    total = int(round(sec))
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return "%d 分 %02d 秒" % (minutes, seconds)
    hours, minutes = divmod(minutes, 60)
    return "%d 小时 %02d 分" % (hours, minutes)


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


#: 悬浮条的位置 / 透明度存这里，下次扫描沿用（坏了就回默认，不影响识别）
HUD_CFG = os.path.join(os.path.expanduser("~"), ".officepack_hud.json")


def _load_hud_cfg():
    """读悬浮条的透明度和位置：返回 ``(alpha, (x, y) 或 None)``。"""
    try:
        with open(HUD_CFG, encoding="utf-8") as fh:
            data = json.load(fh)
        alpha = max(0.35, min(1.0, float(data.get("alpha", 0.92))))
        pos = data.get("pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            return alpha, (int(pos[0]), int(pos[1]))
        return alpha, None
    except Exception:  # noqa: BLE001  配置坏了不该影响扫描
        return 0.92, None


def _save_hud_cfg(alpha, pos):
    """记住悬浮条的透明度 / 位置（拖动或调滑杆后调用）。"""
    try:
        with open(HUD_CFG, "w", encoding="utf-8") as fh:
            json.dump({"alpha": round(float(alpha), 2),
                       "pos": [int(pos[0]), int(pos[1])] if pos else None}, fh)
    except Exception:  # noqa: BLE001
        pass


def _overlaps(x, y, w, h, bbox) -> bool:
    """窗口矩形 (x,y,w,h) 和 bbox 是否有重叠（同一套坐标系）。"""
    bx1, by1, bx2, by2 = bbox
    return not (x + w <= bx1 or x >= bx2 or y + h <= by1 or y >= by2)


def _free_spot(w, h, bbox, monitors, pad: int = 8):
    """在显示器里找一块**不和 bbox 重叠**的地方放窗口；找不到返回 None。

    扫描期间用户从任务栏把主窗口点回来时，不能把窗口强行再藏起来（那等于点了
    没反应），只能把它挪开。每块屏试四个角，先到先得；``bbox`` 与 ``monitors``
    必须用同一套坐标（Tk 的窗口位置是逻辑坐标，物理像素的框要先除缩放比）。
    """
    bx1, by1, bx2, by2 = bbox
    for ml, mt, mr, mb in monitors or []:
        for px, py in ((ml + pad, mt + pad), (mr - w - pad, mt + pad),
                       (ml + pad, mb - h - pad), (mr - w - pad, mb - h - pad)):
            if not _overlaps(px, py, w, h, (bx1, by1, bx2, by2)):
                return px, py
    return None


def _make_grabber(bbox):
    """（保留旧入口）只抓框内区域的 GDI 抓图函数；失败返回 None。

    实现已经搬到 ``screengrab.gdi_grabber``：现在按机器自适应，有独显的走
    DXGI 桌面复制、没有的走 GDI BitBlt。这里留薄壳是为了老脚本和测试不受影响。
    """
    return screengrab.gdi_grabber(bbox)


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
