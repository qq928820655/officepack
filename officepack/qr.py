"""二维码通道：把容器文本装进 QR 码，也能从截图 / 图片里扫回来。

分工很明确：
    qrcode     负责生成 —— 能显式指定纠错级别，输出标准 PNG，任何扫码工具都认
    zxing-cpp  负责识别 —— 对截图、缩放、轻微倾斜、多码同图都比较稳

一帧 = 一个自包含的 ``#OPK1`` 分段，格式与文本分片**完全一致**，
所以二维码还原直接复用 ``container.unpack_texts()``，不存在第二套合并逻辑。

容量（QR 40，字母数字模式）：
    L 4296 / M 3391 / Q 2420 / H 1852 字符

编码器不限定。base32 / hex 的字符集是 A-Z 0-9，正好落在二维码的「字母数字模式」
（5.5 bit/字符）里最省地方；base91 / base64 / cjk 等会掉进字节模式（8 bit/字符）
甚至更高，能用，只是同样内容要多占一些帧。用哪个由容器 meta 记录，
扫码后按 meta 里的 codec 原样解码 —— 所以任意编码器生成、任意编码器还原都成立。
"""

from __future__ import annotations

import math
import os
from typing import Iterable, Sequence

from . import codecs as codec_mod
from . import container as ct

#: QR 通道推荐的编码器（纯大写字母+数字，命中二维码的字母数字模式）
QR_CODEC = "base32"

#: QR version 40 在「字母数字模式」下的字符容量（实测 qrcode 库的真实上限）
QR_ALNUM_CAPACITY = {"L": 4296, "M": 3391, "Q": 2420, "H": 1852}
DEFAULT_EC = "L"

# 分帧按「比特」算而不是按「字符」算：
# 载荷里的字符落在哪种模式，决定了每个字符要花多少 bit，
# 只数字符的话，首帧带了 Meta 就会悄悄超容量（qrcode 会报 version 41）。
_BITS_ALNUM = 5.5    # 字母数字模式：A-Z 0-9 和 $%*+-./: 空格
_BITS_BYTE = 8.0     # 字节模式：其余 ASCII
_BITS_CJK = 24.0     # cjk 编码器输出汉字，UTF-8 下 3 字节/字
_SEGMENT_OVERHEAD_BITS = 40   # 模式指示符 + 字符计数 + 终止符的余量

#: 落在二维码「字母数字模式」里的编码器（同样字符数能装更多）
ALNUM_CODECS = ("qr45", "base32", "hex")

#: 总览图最多拼多少帧。再多图幅会大到几万像素，识别率反而下降
MAX_SHEET_TILES = 12

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff")


class QRError(ValueError):
    pass


# --------------------------------------------------------------------------
# 分帧
# --------------------------------------------------------------------------
def bits_per_char(codec_name: str) -> float:
    """该编码器产出的文本，在二维码里平均每个字符要占多少 bit。"""
    name = (codec_name or "").lower()
    if name in ALNUM_CODECS:
        return _BITS_ALNUM
    if name == "cjk":
        return _BITS_CJK
    return _BITS_BYTE


def overhead_ratio(codec_name: str) -> float:
    """相对 base32，这个编码器要多占多少二维码空间（1.0 = 一样，>1 = 更费）。"""
    return bits_per_char(codec_name) / _BITS_ALNUM


def single_frame_bytes(ec: str = DEFAULT_EC, codec_name: str = QR_CODEC,
                       meta_chars: int = 360) -> int:
    """单张二维码最多能装多少**压缩后字节**（首帧要带 Meta，按典型长度扣减）。

    物理上限：QR 40 版字母数字模式 4296 字符 × 5.5 bit ÷ 8 = 2953 字节。
    qr45 是 1.5 字符/字节，再扣掉段头和 meta（压缩后约 360 字符，走字节模式 8 bit/字符），
    实测不加密约 2.5KB、加密约 2.35KB —— 这就是「一张码搞定」的天花板。
    ``meta_chars=0`` 可取不含元信息的上限（后续帧的真实容量）。
    """
    head = len("#OPK1 abcdef 001/001 AAAAA 0000000")
    chars = _payload_budget(ec, head + 1 + meta_chars, codec_name)
    return int(chars / codec_mod.get_codec(codec_name).expansion)


def capacity_report(pack: ct.PackResult, ec: str = DEFAULT_EC) -> dict:
    """给界面用的一句话容量体检：装得下几张码、卡在哪。"""
    codec_name = (pack.meta or {}).get("codec", QR_CODEC)
    frames = build_frames(pack, ec)
    room = single_frame_bytes(ec, codec_name)
    payload = len(pack.payload)
    return {
        "frames": len(frames),
        "payload_bytes": payload,
        "single_frame_bytes": room,
        "codec": codec_name,
        "ec": ec,
        "fits_single": len(frames) == 1,
        "over_ratio": round(payload / room, 2) if room else 0.0,
    }


def _payload_budget(ec: str, byte_overhead_chars: int, codec_name: str) -> int:
    """给定纠错级别与编码器，扣掉头部/Meta 的字节开销后，载荷最多能放几个字符。"""
    cap = QR_ALNUM_CAPACITY.get((ec or DEFAULT_EC).upper())
    if cap is None:
        raise QRError("未知纠错级别 %r，可用：L / M / Q / H" % ec)
    bits = (cap * _BITS_ALNUM
            - byte_overhead_chars * _BITS_BYTE
            - _SEGMENT_OVERHEAD_BITS)
    return int(max(0.0, bits) // bits_per_char(codec_name))


def build_frames(
    pack: ct.PackResult,
    ec: str = DEFAULT_EC,
    chars_per_frame: int = 0,
) -> list[str]:
    """把打包结果切成一批「每张二维码装得下」的分段文本。

    第 1 帧额外带 Meta 行（Meta 只在首帧出现一次，省容量）。
    ``chars_per_frame`` 给了就再压一道，用于「宁可多几帧、每帧更好扫」的场合。

    编码器不限定：base32 / hex 落在二维码的字母数字模式最省，
    base91 / base64 / cjk 等也能用，只是每字符占的 bit 更多、帧数更多。
    """
    meta = pack.meta
    codec_name = meta.get("codec", "")
    ec = (ec or DEFAULT_EC).upper()
    if ec not in QR_ALNUM_CAPACITY:
        raise QRError("未知纠错级别 %r，可用：L / M / Q / H" % ec)

    codec = codec_mod.get_codec(codec_name, meta.get("alphabet"))
    encoded = codec.encode(pack.payload)
    meta["text_chars"] = len(encoded)

    meta_line = ct.META_PREFIX + ct._meta_b64(meta)
    doc_id = meta["sha256"][:6]
    header = "#OPK1 %s 000/000 %s 0000000" % (doc_id, "AAAAA")

    first_cap = _payload_budget(ec, len(header) + 1 + len(meta_line) + 1, codec_name)
    rest_cap = _payload_budget(ec, len(header) + 1, codec_name)
    if chars_per_frame > 0:
        first_cap = min(first_cap, chars_per_frame)
        rest_cap = min(rest_cap, chars_per_frame)
    if first_cap <= 16:
        raise QRError(
            "二维码容量不足以放下元信息（纠错 %s / 编码器 %s 只剩 %d 字符），"
            "请改用更低的纠错级别（L）或 base32 编码器" % (ec, codec_name, first_cap)
        )

    chunks: list[str] = []
    rest = encoded
    take = first_cap
    while rest:
        chunks.append(rest[:take])
        rest = rest[take:]
        take = rest_cap
    if not chunks:
        chunks = [""]

    total = len(chunks)
    frames: list[str] = []
    for idx, chunk in enumerate(chunks, 1):
        head = "%s %s %03d/%03d %s %d" % (
            ct.SEG_PREFIX, doc_id, idx, total, ct._crc91(chunk), len(chunk)
        )
        parts = [head]
        if idx == 1:
            parts.append(meta_line)
        if chunk:
            parts.append(chunk)
        frames.append("\n".join(parts))
    return frames


# --------------------------------------------------------------------------
# 生成图片
# --------------------------------------------------------------------------
#: 写盘 / 读图用几条线程。zxing-cpp 的**编码器**全程持 GIL，实测 1/2/4/8 线程
#: 都是 ~14ms/帧，多线程对它没有收益，所以渲染老老实实串行；能并行的只有
#: 「PNG 编码」和「zxing 解码」—— 这两处 PIL 和 zxing 都会放 GIL，
#: 4 线程实测分别快 3.7 倍和 2.4 倍，白捡的。
RENDER_THREADS = 4
DECODE_THREADS = 4

#: 动图逐帧识别时，并行批次大小与「回头深挖」的帧数上限。
#: 上千帧的 GIF 不能一次性把几百兆像素全拽进内存，分批走。
DECODE_BATCH = 48
DECODE_RETRY = 8

_UNSET = object()
_ZX_WRITER = _UNSET


class _BackendMissing(Exception):
    """zxing-cpp 不具备写码能力 —— 只有这时才退回 qrcode。"""


def _zx_writer():
    """zxing-cpp 的写码后端；没装 / 版本太老就返回 None。

    为什么换：``qrcode`` 是纯 Python，生成一张 QR 40 版矩阵要 275~350ms，
    再放大成图，单帧 720ms —— 1MB 文件 451 帧要 **5 分钟**。zxing-cpp 是 C++，
    同一帧 15ms（含转成 PIL 图），快 40 倍；而且它本来就是识别端的依赖，
    打包里已经有了，不新增任何东西。
    """
    global _ZX_WRITER
    if _ZX_WRITER is _UNSET:
        try:
            import zxingcpp

            _ZX_WRITER = zxingcpp if hasattr(zxingcpp, "create_barcode") else None
        except Exception:  # noqa: BLE001  没装 zxing / 导入失败
            _ZX_WRITER = None
    return _ZX_WRITER


def _tick(progress, done: int, total: int, phase: str = "") -> None:
    """进度回调统一入口 —— 回调自己出错绝不能把出码/识别搞崩。"""
    if progress is None:
        return
    try:
        progress(int(done), int(total), phase)
    except Exception:  # noqa: BLE001
        pass


def _ec_level(ec: str) -> str:
    key = (ec or DEFAULT_EC).upper()
    if key not in QR_ALNUM_CAPACITY:
        raise QRError("未知纠错级别 %r，可用：L / M / Q / H" % ec)
    return key


def _ec_const(ec: str):
    import qrcode.constants as C

    table = {
        "L": C.ERROR_CORRECT_L,
        "M": C.ERROR_CORRECT_M,
        "Q": C.ERROR_CORRECT_Q,
        "H": C.ERROR_CORRECT_H,
    }
    return table[_ec_level(ec)]


def _make_qr_image(text: str, ec: str, target_px: int = 960):
    """生成一张二维码 PIL 图；模块大小按目标像素反推，保证每帧清晰度一致。"""
    return list(_iter_uniform([text], ec, target_px))[0]


def _iter_uniform(texts: Sequence[str], ec: str, target_px: int, progress=None,
                  base: int = 0, grand_total: int | None = None):
    """把一批文本渲染成**同一像素尺寸**的二维码图（生成器，出一张丢一张）。

    为什么必须统一：每帧的数据量不同，QR 版本（模块数）就不同，按
    ``target_px / 模块数`` 反推的模块大小一取整，产出的图片尺寸就参差不齐
    （实测 555 和 665 混在一起）。GIF 的逻辑画布取的是首帧尺寸，比画布大的帧
    会被**裁掉右/下边**，播放器里显示的就是个残缺码，怎么扫都扫不出来 ——
    「框选认不出动图」的根因就在这里。这里统一按最大那帧算模块大小，再把每张
    居中补白到同一画布，静区照样是白的，不影响识别。

    返回的图是 **L 模式**（1 字节/像素）：二维码只有黑白两色，用 RGB 存盘
    既慢又大三倍，没必要。

    ``base`` / ``grand_total`` 给分批调用用：一次只渲染一页时，进度条要按
    「整份文件第几张」报，而不是「本页第几张」。
    """
    texts = list(texts)
    total = grand_total or len(texts)
    if not texts:
        return
    level = _ec_level(ec)
    writer = _zx_writer()
    plan = None
    if writer is not None:
        try:
            plan = _plan_zx(writer, texts, level, target_px)
        except _BackendMissing:
            plan = None
    source = _emit_zx(plan) if plan else _iter_uniform_py(texts, level, target_px)
    for idx, im in enumerate(source, 1):
        yield im
        _tick(progress, base + idx, total, "渲染")


def _plan_zx(zx, texts: list[str], level: str, target_px: int):
    """zxing-cpp 预排：把每帧先编码成矩阵，量出统一倍率，返回 ``(bars, box, side)``。

    这一步是**立即执行**的（不是生成器），所以「zxing 没有写码能力」和
    「这一帧太大装不下」这两种错误都能在出第一张图之前就报出来。
    """
    fmt = getattr(zx.BarcodeFormat, "QRCode")
    bars, counts = [], []
    for text in texts:
        try:
            bar = zx.create_barcode(text, fmt, ec_level=level)
            # scale=1 → 每模块 1 像素，只用来量「含静区一共多少个模块」
            counts.append(int(bar.to_image(scale=1, add_quiet_zones=True).shape[0]))
        except (ImportError, AttributeError) as exc:  # 绑定缺能力，退回 qrcode
            raise _BackendMissing(str(exc)) from None
        except Exception as exc:  # noqa: BLE001  段比这个纠错级别装得下的还大
            raise QRError("二维码内容超出容量（%s）：请降低纠错级别，"
                          "或调小每帧字符数/改用 qr45 编码" % exc) from None
        bars.append(bar)
    box = max(3, int(round(float(target_px) / max(counts))))
    return bars, box, max(counts) * box


def _emit_zx(plan):
    """按预排好的统一倍率逐帧出图（L 模式）。"""
    from PIL import Image

    bars, box, side = plan
    for bar in bars:
        view = memoryview(bar.to_image(scale=box, add_quiet_zones=True))
        height, width = view.shape[0], view.shape[1]
        im = Image.frombytes("L", (width, height), view.tobytes())
        if (width, height) != (side, side):
            canvas = Image.new("L", (side, side), 255)
            canvas.paste(im, ((side - width) // 2, (side - height) // 2))
            im = canvas
        yield im


def _iter_uniform_py(texts: list[str], level: str, target_px: int):
    """退路：纯 Python 的 qrcode。慢 40 倍，只在 zxing 不可写码时用。"""
    import qrcode
    from PIL import Image

    counts = []
    for text in texts:
        proto = qrcode.QRCode(error_correction=_ec_const(level), border=4, box_size=1)
        proto.add_data(text, optimize=20)
        try:
            proto.make(fit=True)
        except Exception as exc:  # 段比这个纠错级别能装的还大
            raise QRError("二维码内容超出容量（%s）：请降低纠错级别，"
                          "或调小每帧字符数/改用 qr45 编码" % exc) from None
        counts.append(proto.modules_count + 8)      # + 两侧各 4 模块静区
    box = max(3, int(round(float(target_px) / max(counts))))
    side = max(counts) * box

    for text in texts:
        qr = qrcode.QRCode(error_correction=_ec_const(level), border=4, box_size=box)
        qr.add_data(text, optimize=20)
        qr.make(fit=True)
        im = qr.make_image(fill_color="black",
                           back_color="white").convert("L")
        if im.size != (side, side):
            canvas = Image.new("L", (side, side), 255)
            canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
            im = canvas
        yield im


def _render_uniform(texts: Sequence[str], ec: str, target_px: int) -> list:
    """``_iter_uniform`` 的列表版（拼总览 / 测试用）。"""
    return list(_iter_uniform(texts, ec, target_px))


#: L → 双色 P 的查找表：0~127（暗）→ 索引 1（黑），128~255（亮）→ 索引 0（白）。
#: 索引 0 **必须是白**：GIF 的逻辑背景色取调色板 0 号，而我们用
#: ``disposal=2``（每帧先清成背景色再画差异区域）—— 0 号是黑的话，
#: 有些播放器会把整屏刷黑再叠差异，画面就成了满屏噪点，怎么都扫不出。
_BIT_LUT = [1] * 128 + [0] * 128


def _bilevel(im):
    """黑白图转 2 色调色板（GIF 帧）。

    用固定调色板而不是 ``convert("P", palette=ADAPTIVE)``：自适应要先建直方图
    再量化，实测 27.9ms/帧；固定 LUT 只要 1.1ms，而黑白图两者**逐像素一致**。
    """
    from PIL import Image

    out = Image.new("P", im.size)
    out.putpalette([255, 255, 255, 0, 0, 0])      # 0 = 白（背景），1 = 黑
    out.paste(im.point(_BIT_LUT))
    return out


#: 动图的首/尾标识帧前缀。有它，识别端才知道「从哪开始、到哪算一轮收完」，
#: 而不是靠猜播放相位；没有它的旧 GIF 仍然按原来的方式收（向后兼容）。
MARKER_PREFIX = "#OPKmark"


def marker_text(kind: str, doc_id: str, total: int | None = None) -> str:
    """生成标识帧文本：``#OPKmark|S|<docId>|<总帧数>`` / ``#OPKmark|E|<docId>``。"""
    if kind.upper() == "S":
        return "%s|S|%s|%d" % (MARKER_PREFIX, doc_id, int(total or 0))
    return "%s|E|%s" % (MARKER_PREFIX, doc_id)


def is_marker(text: str) -> bool:
    return bool(text) and text.startswith(MARKER_PREFIX)


def parse_marker(text: str):
    """解析标识帧，返回 ``(kind, doc_id, total)``；不是标识返回 None。"""
    if not is_marker(text):
        return None
    parts = text.split("|")
    if len(parts) < 3:
        return None
    kind = parts[1].strip().upper()
    doc_id = parts[2].strip()
    total = None
    if len(parts) > 3:
        try:
            total = int(parts[3])
        except ValueError:
            total = None
    return kind, doc_id, total


def _mark_texts(frames: Sequence[str]) -> tuple[str, str] | None:
    """按分段的 doc_id / 总帧数生成首尾标识；非分段内容（单码容器）不加。"""
    try:
        segs = ct.parse_segments("\n".join(frames))
    except Exception:  # noqa: BLE001
        return None
    if not segs:
        return None
    seg = segs[0]
    return marker_text("S", seg.doc_id, seg.total), marker_text("E", seg.doc_id)


def render_gif(
    frames: Sequence[str],
    out_path: str,
    ec: str = DEFAULT_EC,
    fps: float = 3.0,
    target_px: int = 720,
    markers: bool = True,
    progress=None,
) -> str:
    """把多帧合成一张**循环播放的 GIF**：手机对着屏幕录像/扫码就能一次收全。

    为什么 GIF 不吃亏：二维码只有黑白两色，GIF 的 256 色调色板对它**完全无损**，
    再用 LZW 压缩，一张 720px 的黑白码通常只有 8~15KB —— 与同分辨率 PNG 相当。
    真正省体积靠的是 ``target_px``（每模块 3px 就够扫，无需 5px）。

    帧率别贪高：手机相机对焦+解码要 150ms 以上，``fps`` 取 2~4 最稳。

    ``markers=True`` 会在首尾各插一张**标识帧**（``#OPKmark|S`` / ``|E``）：
    屏幕框选时画面是动的，识别端不可能知道一轮从哪开始、到哪结束 —— 有了标识帧，
    它就是「见首帧清零、逐帧收、见尾帧收工」，漏帧还能靠下一轮补齐。

    ``progress(done, total, phase)`` 每出一帧回调一次，用来刷进度条。
    """
    if not frames:
        raise QRError("没有可渲染的帧")
    if len(frames) == 1:
        raise QRError("只有一帧，用静态图就够了，不需要动图")
    duration = _anim_duration(fps)

    # 边出图边转调色板：GIF 必须一次性拿到所有帧（PIL 的 save_all 要完整列表），
    # 但 L 模式的中间图可以用完就丢，峰值内存只多一份 P 图。
    images = [_bilevel(im)
              for im in _anim_source(frames, ec, target_px, markers, progress)]
    # 出图完了还有一步「把 N 帧压成一张动图」，几百帧能跑几十秒。
    # 不报这一步，进度条会停在 100% 一动不动，看起来就像卡死。
    _tick(progress, len(images), len(images), "写入 GIF")
    images[0].save(
        out_path, "GIF", save_all=True, append_images=images[1:],
        duration=duration, loop=0, optimize=True, disposal=2,
    )
    return out_path


#: WebP 动图的编码档位。``method`` 是 libwebp 的速度/压缩权衡（0 最快、6 最狠），
#: 实测 4 是拐点：再往上（6）压缩率只多 0.1%，耗时却从 80ms/帧 涨到 5.3s/帧。
WEBP_METHOD = 4


def render_webp(
    frames: Sequence[str],
    out_path: str,
    ec: str = DEFAULT_EC,
    fps: float = 3.0,
    target_px: int = 720,
    markers: bool = True,
    progress=None,
    method: int = WEBP_METHOD,
) -> str:
    """把多帧合成一张**循环播放的动图 WebP**（无损），体积约为 GIF 的 1/5。

    为什么能小这么多：GIF 的 LZW 是「按行」找重复串，二维码每行图案都不一样，
    于是几乎压不动（实测 33KB/帧）；WebP 无损走的是空间预测 + 熵编码，同尺寸
    逐像素无损还能压到 6.4KB/帧。

    但**默认不用它**，因为播放兼容性远不如 GIF：
    - 浏览器、Chrome、Edge、现代看图工具：能播
    - Windows「照片」、微信、部分看图软件：可能只显示第一帧

    咱们的流程是「让动图在屏幕/软件里播放，再框选连拍」。播不动就等于产物废掉，
    所以它只在用户**显式选择**时输出，GIF 仍是默认。

    纠错级别、标识帧、统一画布、帧率语义都和 :func:`render_gif` 完全一致，
    识别端无需区分 —— 扫的是屏幕像素，不看容器格式。
    """
    from PIL import Image, features

    if not features.check("webp"):
        raise QRError("当前 Pillow 没有 WebP 编码器，无法输出 --qr-webp；"
                      "请改用 GIF 动图，或安装带 WebP 支持的 Pillow")
    if not frames:
        raise QRError("没有可渲染的帧")
    if len(frames) == 1:
        raise QRError("只有一帧，用静态图就够了，不需要动图")
    duration = _anim_duration(fps)
    # 无损 WebP 直接吃 L 模式原图：它内部自己会做二值友好的预测，不需要调色板
    images = list(_anim_source(frames, ec, target_px, markers, progress))
    _tick(progress, len(images), len(images), "写入 WebP")
    images[0].save(
        out_path, "WEBP", save_all=True, append_images=images[1:],
        duration=duration, loop=0, lossless=True, quality=100, method=method,
    )
    return out_path


def _anim_duration(fps: float) -> int:
    """帧率 → 每帧毫秒数；夹在 0.5~10 之间（相机对焦+解码要 150ms 以上）。"""
    return max(0, int(round(1000.0 / max(0.5, min(10.0, float(fps))))))


def _anim_source(frames: Sequence[str], ec: str, target_px: int,
                 markers: bool, progress):
    """动图共用：首尾插标识帧 → 统一画布逐帧出图（生成器）。

    首帧 S 在前、尾帧 E 在后：识别端见 S 开始收、见 E 收工，不用猜播放相位。
    """
    texts = list(frames)
    marks = _mark_texts(frames) if markers else None
    if marks:
        texts = [marks[0]] + texts + [marks[1]]
    return _iter_uniform(texts, ec, target_px, progress=progress)


def _save_png(job) -> str:
    """线程池任务：把一张图写成 PNG。

    ``compress_level=6`` 是实测拐点：L 模式 7.6ms/帧、13KB；调成 1 反而
    18.5ms / 40KB（PNG 的 filter 选路在小图上更费）。多线程再做一次叠加。
    """
    im, path = job
    im.save(path, "PNG", compress_level=6)
    return path


def render_images(
    frames: Sequence[str],
    out_dir: str,
    stem: str,
    ec: str = DEFAULT_EC,
    sheet: bool = True,
    target_px: int = 960,
    single: bool = True,
    progress=None,
) -> dict:
    """把每帧写成一张 PNG，可选再拼总览图（多帧自动分页，每页 12 张）。

    单页塞太多码会有上万像素，扫起来反而容易漏（实测 125 帧的 11700×11000
    总览图会丢帧），所以按每页 12 张分页 —— 19 帧就是 2 张总览图，截两次图搞定。

    ``single=False`` 只写总览图、不写单帧 PNG：总览里的每张码和单帧是同一份
    位图（原尺寸拼贴，没有缩放），识别总览就等于识别全部单帧，少写 N 个文件。
    只有 1 帧时没有总览可拼，会强制回退成单帧输出。

    按页「渲染一页 → 并行写盘 → 拼总览 → 丢掉」流水走，450 帧也不会把
    几百兆像素全堆在内存里；写盘交给线程池（PIL 的 PNG 编码器放 GIL）。

    返回 ``{"images": [...], "sheet": 首页或 None, "sheets": [...], "dir": 目录}``。
    """
    from concurrent.futures import ThreadPoolExecutor

    if not frames:
        raise QRError("没有可渲染的帧")
    os.makedirs(out_dir, exist_ok=True)

    total = len(frames)
    # 只有一帧时没有总览可言，single=False 也必须老老实实落一张单帧图
    if not sheet or total == 1:
        single = True
    pages = int(math.ceil(total / float(MAX_SHEET_TILES)))
    images: list[str] = []
    sheets: list[str] = []

    with ThreadPoolExecutor(max_workers=RENDER_THREADS) as pool:
        for start in range(0, total, MAX_SHEET_TILES):
            page = list(frames[start:start + MAX_SHEET_TILES])
            tiles = [(start + i + 1, im) for i, im in
                     enumerate(_iter_uniform(page, ec, target_px,
                                             progress=progress, base=start,
                                             grand_total=total))]
            if single:
                jobs = [(im, os.path.join(out_dir, "%s.qr%03d.png" % (stem, idx)))
                        for idx, im in tiles]
                images.extend(pool.map(_save_png, jobs))
            if sheet and total > 1:
                name = ("%s.qr-all.png" % stem if pages == 1
                        else "%s.qr-all%d.png" % (stem, len(sheets) + 1))
                path = os.path.join(out_dir, name)
                _make_sheet(tiles, total, path, ec)
                sheets.append(path)
            del tiles
    return {"images": images, "sheet": sheets[0] if sheets else None,
            "sheets": sheets, "dir": out_dir}


def _make_sheet(tiles, total: int, path: str, ec: str) -> None:
    """把一页帧拼成一张总览图：一行截图就能把这一页全部带回来。"""
    from PIL import Image, ImageDraw, ImageFont

    count = len(tiles)
    cols = max(1, int(math.ceil(math.sqrt(count))))
    rows = int(math.ceil(count / cols))
    cell = max(im.width for _, im in tiles)
    pad = 16
    label_h = 34

    width = cols * cell + pad * (cols + 1)
    height = rows * (cell + label_h) + pad * (rows + 1)
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    # 字体是**可选**的：瘦身版删掉了 FreeType（_imagingft.pyd，2.1MB），
    # 这时 truetype 会抛 ImportError 而 load_default 仍可用；
    # 万一连默认字体都没有，就干脆不画序号标签 —— 顺序本来就无关紧要。
    font = None
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except Exception:  # noqa: BLE001  缺 FreeType / 缺字体文件
        try:
            font = ImageFont.load_default()
        except Exception:  # noqa: BLE001
            font = None

    for i, (idx, im) in enumerate(tiles):
        r, c = divmod(i, cols)
        x = pad + c * (cell + pad) + (cell - im.width) // 2
        y = pad + r * (cell + label_h + pad)
        sheet.paste(im, (x, y))
        if font is None:
            continue
        label = "%d / %d" % (idx, total)
        try:
            tw = int(draw.textlength(label, font=font))
        except Exception:  # noqa: BLE001  老 Pillow / 位图字体无 textlength
            tw = 6 * len(label)
        draw.text((x + (im.width - tw) // 2, y + im.height + 4), label,
                  fill="black", font=font)

    sheet.save(path, "PNG")


# --------------------------------------------------------------------------
# 识别图片
# --------------------------------------------------------------------------
def _iter_images(paths: Iterable[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.lower().endswith(IMAGE_EXTS):
                    out.append(os.path.join(p, name))
        elif os.path.isfile(p) and p.lower().endswith(IMAGE_EXTS):
            out.append(p)
    return out


def is_image_path(path: str) -> bool:
    return path.lower().endswith(IMAGE_EXTS)


def partition_sources(sources: Iterable[str]) -> tuple[list[str], list[str]]:
    """把输入拆成（图片源, 文本容器源）。目录按图片源处理。"""
    srcs = list(sources)
    images = [p for p in srcs if os.path.isdir(p) or is_image_path(p)]
    texts = [p for p in srcs if p not in images]
    return images, texts


def unpack_any(
    sources: Iterable[str],
    out_dir: str | None = None,
    password: str | None = None,
    out_name: str | None = None,
    progress=None,
):
    """文本容器与二维码图片混在一起也能还原；返回 (结果列表, 统计)。"""
    images, texts = partition_sources(sources)
    blocks: list[str] = []
    if images:
        blocks.extend(decode_images(images, progress=progress))
    for path in texts:
        with open(path, "r", encoding="utf-8-sig") as fh:
            blocks.append(fh.read())
    if not blocks:
        raise QRError("没有可还原的输入")
    results = ct.unpack_texts(blocks, out_dir, password, out_name)
    return results, {"images": len(images), "texts": len(texts), "blocks": len(blocks)}


def decode_image(image, deep: bool = True, fast: bool = False,
                 keep_markers: bool = False, progress=None) -> list[str]:
    """识别单张图里的所有二维码；可传路径、PIL 图或 numpy 数组。

    多帧图（GIF / 动图 PNG / TIFF）会**逐帧**识别 —— 所以一段动态二维码
    存成 gif 后，直接把它丢进来就能收全所有帧，不用逐帧截图。

    ``deep=False`` 是「一批图里的其中一张」：每张只给 1.5 秒、不做昂贵重试，
    避免一次多选十几张图等上几分钟。

    ``fast=True`` 只跑一遍原图，一个像素变换都不试（毫秒级）——
    给「对着动图连续抓帧」用，抓得快才能追上播放速度。

    ``keep_markers`` 只在现场连拍时用：需要拿到 ``#OPKmark`` 首尾标识帧判断
    一轮动图从哪开始、到哪结束；还原时必须丢掉它们，那是控制信号不是数据。
    """
    from PIL import Image

    if isinstance(image, str):
        with Image.open(image) as img:      # 一定要 close：动图会占着文件句柄
            return _decode_frames(img, deep, fast, keep_markers, progress)
    if not hasattr(image, "mode"):        # numpy 数组之类，交给 zxing 自己处理
        import zxingcpp

        results = zxingcpp.read_barcodes(image)
        return [r.text for r in results if r.text]
    return _decode_frames(image, deep, fast, keep_markers, progress)


def _probe_frame(item):
    """只跑一遍原图识别，不做任何像素变换 —— 一帧几十毫秒。"""
    _idx, frame = item
    try:
        return _read_one(frame, deep=False, fast=True)
    except Exception:  # noqa: BLE001  单帧读不出来不该拖垮整张动图
        return []


def _decode_frames(img, deep: bool = True, fast: bool = False,
                   keep_markers: bool = False, progress=None) -> list[str]:
    """逐帧识别一张图（静态图就是走一帧）。

    动图走两轮：先**并行快扫**每一帧的原图（一次像素变换都不做，几十毫秒
    一帧），收齐就收工；只有确实一个码都没读到的帧才回头做深度重试。

    老实现是每帧都直接进 6 秒重试预算 —— 129 帧的动图要跑两分半，用户看到
    的就是「点了之后什么反应都没有」。现在同样的图 2~3 秒。
    """
    from concurrent.futures import ThreadPoolExecutor

    texts: list[str] = []
    seen: set[str] = set()

    def _take(found) -> int:
        fresh = 0
        for text in found or ():
            if not text or text in seen:
                continue
            if not keep_markers and is_marker(text):
                continue
            seen.add(text)
            texts.append(text)
            fresh += 1
        return fresh

    try:
        count = int(getattr(img, "n_frames", 1))
    except Exception:  # noqa: BLE001
        count = 1

    if count <= 1:
        _take(_read_one(img, deep=deep, fast=fast))
        _tick(progress, 1, 1, "识别")
        return texts

    if fast:                       # 连拍模式：一枪只认原图，别在单帧上纠缠
        for i in range(count):
            img.seek(i)
            _take(_read_one(img, deep=False, fast=True))
        _tick(progress, count, count, "识别")
        return texts

    barren: list[int] = []
    workers = max(1, min(DECODE_THREADS, count))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, count, DECODE_BATCH):
            stop = min(start + DECODE_BATCH, count)
            batch = []
            for i in range(start, stop):
                img.seek(i)
                batch.append((i, img.copy()))
            for (idx, _frame), found in zip(batch, pool.map(_probe_frame, batch)):
                if not _take(found):
                    barren.append(idx)
            del batch
            _tick(progress, stop, count, "识别")
            if _looks_complete(texts):
                return texts

    if not barren or not deep:
        return texts

    # 还没收齐：只对「一个码都没读到」的帧回头深挖（6 秒预算只在真需要时花）。
    # 缺的帧常常是被水印糊住 / 被缩放插值毁掉的那几张，值得单独救一次。
    retry = barren[:DECODE_RETRY]
    for n, i in enumerate(retry, 1):
        img.seek(i)
        _take(_read_one(img.copy(), deep=True))
        _tick(progress, n, len(retry), "重试缺帧")
        if _looks_complete(texts):
            break
    return texts


def _dedup(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for t in items:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


#: 一次识别的重试预算（秒）。超时就带着已识别到的帧返回 ——
#: 屏幕上还有水印/缩放问题时，宁可少几帧让人补拍，也不能让界面卡死。
RETRY_BUDGET = 6.0

#: 一批图里的单张预算。多选 10 张图时按 6 秒/张算就是 1 分钟，用户等不了；
#: 批次里缺的帧通常就在别的图里（一张图装不下才拆的），深挖这张不划算。
BATCH_BUDGET = 1.5

#: 超过这个像素数就不再做 2 倍放大：放大后几十兆像素，zxing 要跑几十秒
UPSCALE_LIMIT = 6_000_000


def _read_one(img, deep: bool = True, fast: bool = False, budget: float | None = None) -> list[str]:
    """识别一张（单帧）图里的所有码；识别不全就逐级加码重试。

    屏幕截图里常有水印、半透明蒙层、看图软件的缩放插值，一次 zxing 掉帧很正常。
    这里先按原图识别，只要段头显示「还没收全」就依次试：固定阈值二值化（抹掉
    半透明水印）、Otsu、反色、单通道、放大（补缩放糊掉的模块）、中值滤波、
    重叠切块。收全就立刻停，且有总时间预算，不会卡死界面。

    ``deep=False`` 用于「一次给一批图」的场合：预算缩短到 1.5 秒、跳过切块，
    因为缺的帧多半在别的图里，不该在一张图上耗 6 秒。
    """
    import time

    import zxingcpp
    from PIL import Image

    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")

    # 超大整屏截图先等比缩小：几十兆像素的图既慢又容易漏码
    pixels = img.width * img.height
    if pixels > 50_000_000:
        scale = (50_000_000 / pixels) ** 0.5
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                         Image.LANCZOS)

    try:
        texts = [r.text for r in zxingcpp.read_barcodes(img) if r.text]
    except Exception as exc:  # 损坏图片 / 不支持的格式
        raise QRError("二维码识别失败：%s" % exc) from None
    if fast:                      # 连拍模式：只认原图，交给调用方下一枪再试
        return _dedup(texts)
    if texts and _looks_complete(texts):
        return _dedup(texts)

    if budget is None:
        budget = RETRY_BUDGET if deep else BATCH_BUDGET
    deadline = time.monotonic() + budget
    seen = set(texts)
    baseline = len(texts)

    def _sweep(variants, idle_limit: int = 0) -> tuple[int, bool]:
        """跑一组变体，返回（新增帧数, 是否超时）。

        ``idle_limit`` 是「连续几次没捞到新帧就放弃」——某一类变体完全无效时
        不必把所有档位都试完，省下时间去试更有希望的。
        """
        gained = 0
        idle = 0
        for variant in variants:
            if time.monotonic() > deadline:
                return gained, True
            try:
                found = [r.text for r in zxingcpp.read_barcodes(variant) if r.text]
            except Exception:  # noqa: BLE001  某个变体解不动不影响整体
                continue
            fresh = [t for t in found if t not in seen]
            if not fresh:
                idle += 1
                if idle_limit and idle >= idle_limit:
                    return gained, False
                continue
            idle = 0
            seen.update(fresh)
            texts.extend(fresh)
            gained += len(fresh)
            if _looks_complete(texts):
                return gained, False
        return gained, False

    # 第一轮只做便宜的像素变换（去水印阈值 / 二值化 / 反色 / 单通道 / 放大）
    cheap_gain, timed_out = _sweep(_cheap_variants(img), idle_limit=2 if not deep else 5)
    if not timed_out and not _looks_complete(texts):
        _sweep(_channel_variants(img), idle_limit=2 if not deep else 3)
    if timed_out or _looks_complete(texts):
        return _dedup(texts)
    # 一批图里的单张不做深挖：缺的帧八成在别的图里
    if not deep:
        return _dedup(texts)
    # 原图已经识别到好几帧、便宜的变换却一帧新东西都没捞着 —— 说明缺的帧压根
    # 不在这一张图里（跨页总览就是这种情况），再去切块/放大纯属白跑几十秒。
    # 反过来，原图只认出 0~1 帧说明是整张图质量有问题（码太小、水印太重），
    # 值得继续拿 3 倍放大和切块死磕。
    if baseline >= 2 and not cheap_gain:
        return _dedup(texts)
    _sweep(_expensive_variants(img))
    return _dedup(texts)


def _looks_complete(texts: list[str]) -> bool:
    """按段头判断是否已经收全：每个文档都攒够 total 帧才算齐。

    没有段头（普通容器文本、别人的二维码）就当作「无从判断」，不再折腾。
    """
    try:
        segs = ct.parse_segments("\n".join(texts))
    except Exception:  # noqa: BLE001
        return True
    if not segs:
        return True
    need: dict[str, int] = {}
    got: dict[str, set] = {}
    for seg in segs:
        need[seg.doc_id] = seg.total
        got.setdefault(seg.doc_id, set()).add(seg.index)
    return all(len(got[d]) >= need[d] for d in need)


#: 连拍参数：两枪间隔 / 多久没新帧就收工 / 最长连拍多久 / 空手多久就转入全力重试
SCAN_INTERVAL = 0.05
SCAN_IDLE = 3.0
SCAN_CAP = 30.0
#: 屏幕上有标识帧（也就是我们自己的动图）时，「多久没有新帧」的判死时间要长得多：
#: 播放器可能暂停、缓冲、被别的窗口挡住一会儿。3 秒就判死，用户回来一看白框了。
#: 真正判断「收完了」靠的是「连续两整轮颗粒无收」，这条只是兜住播放停住的情况。
SCAN_STALL = 20.0
#: 连开几枪一张码都没见着就提前收工。别定太小：整屏抓图 + 解码一枪约
#: 0.2 秒（多屏时 ``all_screens=True`` 要先把整个虚拟桌面抓下来再裁），
#: 6 枪只有 1.3 秒 —— 屏幕刚好在这一瞬刷新/滚动就白判了。10 枪约 2 秒，
#: 仍能很快报「框错了地方」，又不至于一枪不中就放弃。
SCAN_EMPTY_SHOTS = 10
#: 连拍结束后对最后一张图做全力重试的预算
SCAN_DEEP_BUDGET = 4.0
#: 单次扫描的绝对天花板。正常收一份文件是「播完一整轮」那么久（978 帧 3 帧/秒
#: 就是 326 秒），这里兜住极端情况：屏幕上放的是永远收不齐的东西（别人的动态码、
#: 播放器卡在同一帧、无限滚动），别让它对着屏幕空转半小时。
SCAN_CAP_MAX = 1800.0
#: 帧数太少时实测速率不准（前几帧往往挤在一个刷新里到齐），先攒够这么多帧
#: 再拿实测速度推算「还要多久」。
SCAN_RATE_MIN = 8


def _frame_key(img) -> bytes | None:
    """画面指纹：屏幕没变就不必重新解码，把时间省给下一帧。"""
    import hashlib

    try:
        return hashlib.blake2b(img.tobytes(), digest_size=16).digest()
    except Exception:  # noqa: BLE001  取不到就不去重
        return None


def _adaptive_cap(cap: float, need: int | None, fps: float,
                  got: int = 0, span: float = 0.0) -> float:
    """按总帧数放宽连拍上限。

    978 帧的动图按 3 帧/秒播一轮要 **326 秒**，而默认上限只有 30 秒 ——
    卡在这里永远只能收到一小半，用户得反复框十几次，每次还都随机漏。
    收到第一帧读出总帧数后，把上限放开到「够播一整轮」。

    光按标称帧率算还不够：屏幕上那段动图是**播放器**在放，它可能按 2 帧/秒
    慢慢放、也可能被拖慢（解码跟不上、远程桌面掉帧）。所以再拿**实测收帧
    速度**推一遍「收齐还剩多久」，取两者里大的那个 —— 播放比预期慢时自动
    续命，不会刚收到一半就超时收工。
    """
    if not need:
        return cap
    # 一轮播完必然收齐；留到 2.5 轮是为了「个别帧这一轮恰好没解出来」时还有
    # 下一轮的机会 —— 真收完了会靠「连续两轮没有新帧」提前退出，不会真跑满。
    guess = need / max(0.5, float(fps)) * 2.5 + 15.0
    if got >= SCAN_RATE_MIN and span > 0:
        eta = need / max(0.2, got / span) * 1.15 + 15.0
        guess = max(guess, min(eta, need * 2.5))   # 最慢按 0.4 帧/秒兜底
    return min(SCAN_CAP_MAX, max(cap, guess))


def _eta(need: int | None, got: int, span: float) -> float | None:
    """还要多久收齐（秒）。按实测收帧速度推；样本太少就返回 None。"""
    if not need or got < SCAN_RATE_MIN or span <= 0 or got >= need:
        return None
    return max(0.0, (need - got) / max(0.2, got / span))


def _total_frames(texts: Sequence[str]) -> int | None:
    """从已收的帧里读出「这份文件一共多少帧」（段头里写着）。"""
    try:
        segs = ct.parse_segments("\n".join(texts))
    except Exception:  # noqa: BLE001
        return None
    if not segs:
        return None
    return max(seg.total for seg in segs)


def scan_region(grab, idle: float | None = None, cap: float | None = None,
                interval: float | None = None, on_progress=None,
                should_stop=None, frames_per_sec: float = 3.0
                ) -> tuple[list[str], dict]:
    """对着同一块屏幕区域连续抓图，把动图里一帧帧的码全收下来。

    框选的真正用途就是动图：**一张截图只能拿到播放中的某一帧**，而一段动态
    二维码有十几帧，靠单次截图必然缺帧、必然还原失败。

    播放相位不可知，所以有两种收工方式：

    * **有首尾标识帧**（``render_gif`` 默认插入）：见 ``#OPKmark|S`` 记一轮开始，
      见 ``|E`` 记一轮结束，收齐即停；某轮漏了帧就自动再等下一轮，连续两轮
      一帧新东西都没收到才放弃。
    * **没有标识**（旧动图 / 别人的图）：连续 ``idle`` 秒没有新帧就收工。

    **一直收到齐为止**：``cap`` 默认 30 秒，但 978 帧的动图按 3 帧/秒播一轮
    就要 326 秒 —— 卡 30 秒永远只能收到一小半，用户得反复框十几次才凑得齐
    （实测有人框了 35 次）。所以收到第一帧、从段头读出总帧数之后，把上限放宽到
    「够播 2.5 轮」，并拿实测收帧速度续命。停止条件变成：

    * 收齐了 → 立刻停；
    * 用户点了「停止」→ 停；
    * 连续两整轮一帧新的都没收到（屏幕上确实没得收了）→ 停；
    * ``SCAN_STALL`` 秒一帧新的都没有（播放暂停/被挡住）→ 停。

    小文件不受影响（``max`` 取大的那个，30 秒照旧够）。

    ``grab()`` 由调用方提供（返回 PIL.Image），本模块不碰屏幕，便于测试。

    返回 ``(texts, stats)``，texts 不含标识帧；stats 含
    ``shots / frames / cycles / complete / elapsed / need``。
    """
    import time

    idle = SCAN_IDLE if idle is None else idle
    cap = SCAN_CAP if cap is None else cap
    interval = SCAN_INTERVAL if interval is None else interval
    if frames_per_sec >= 6.0:
        # 帧率高的时候别再加人为间隔：GDI 抓图 17ms + 指纹 7ms + 解码 37ms，
        # 不加间隔也有 16 枪/秒 —— 10 帧/秒的动图采得到 1.6 倍，够用。
        interval = 0.0

    started = time.monotonic()
    last_new = started
    texts: list[str] = []
    seen: set[str] = set()
    shots = 0
    complete = False
    last_img = None
    last_key = None
    starts = 0            # 见到首帧的次数 = 已经完整播了几轮
    progress_cycle = 0    # 最近一次收到新帧时的轮次
    saw_end = False
    marked = False
    need = None           # 文档总帧数（收到第一帧后从段头读出来）
    first_new = None      # 第一帧到手的时间，用来量「实测收帧速度」

    while True:
        now = time.monotonic()
        if (now - started) >= cap:
            break
        if marked:
            # 有标识帧：按轮次算，连续两轮颗粒无收才放弃（期间画面继续播）
            if saw_end and starts - progress_cycle >= 2:
                break
            # 尾帧一直没被认出来、或者播放器暂停/缓冲/被切走：给足时间再判死
            if (now - last_new) >= max(idle * 2, SCAN_STALL):
                break
        elif (now - last_new) >= idle:
            break
        # 连开几枪一张码都没见着：多半框错了地方，赶紧转全力重试，别干等
        if not texts and shots >= SCAN_EMPTY_SHOTS:
            break
        if should_stop is not None and should_stop():
            break
        try:
            img = grab()
        except Exception:  # noqa: BLE001  切桌面/锁屏导致的偶发抓图失败不该中断
            img = None
        shots += 1
        if img is not None:
            key = _frame_key(img)
            if key is None or key != last_key:      # 画面变了才解码
                last_key = key
                last_img = img
                try:
                    found = decode_image(img, fast=True, keep_markers=True)
                except Exception:  # noqa: BLE001
                    found = []
                if not found and not texts and shots <= 3:
                    # 裸读一枪没中：屏幕上显示的码经过缩放插值（125% 缩放 = 双线性
                    # 拉伸），往往就差一口气。前几枪换便宜的重试再来一次
                    # （阈值/放大/切块），别坐等到收工才拿最后一帧深挖 ——
                    # 静态画面下画面不变就不会再解码，第一次失手等于永久漏帧。
                    try:
                        found = _read_one(img, deep=False)
                    except Exception:  # noqa: BLE001
                        found = []
                data: list[str] = []
                for text in found:
                    mk = parse_marker(text)
                    if mk is None:
                        data.append(text)
                        continue
                    marked = True
                    if mk[0] == "S":
                        starts += 1
                    elif mk[0] == "E":
                        saw_end = True
                fresh = [t for t in data if t not in seen]
                if fresh:
                    seen.update(fresh)
                    texts.extend(fresh)
                    last_new = time.monotonic()
                    if first_new is None:
                        first_new = last_new
                    if starts:
                        progress_cycle = starts
                    if need is None:
                        # 收到第一帧就知道这份文件一共多少帧了 —— 978 帧按 3 帧/秒
                        # 播一轮要 326 秒，卡 30 秒上限永远只能收一小半。
                        need = _total_frames(texts)
                    # 边收边续命：播放比预期慢就自动把上限往后放
                    cap = _adaptive_cap(cap, need, frames_per_sec,
                                        len(texts), last_new - first_new)
                if texts and _looks_complete(texts):
                    complete = True
                    break
        if on_progress is not None:
            span = (time.monotonic() - first_new) if first_new else 0.0
            try:
                on_progress({"shots": shots, "frames": len(texts), "cycles": starts,
                             "elapsed": time.monotonic() - started,
                             "complete": complete, "need": need,
                             # 「还要多久收齐」——不然只能看着帧数一点点涨干等
                             "eta": _eta(need, len(texts), span)})
            except Exception:  # noqa: BLE001  回调出错不能拖垮识别
                pass
        time.sleep(interval)

    # 便宜的连拍认不出来（码小、有水印、压缩噪点），拿最后一张图做一次全力重试
    if last_img is not None and not complete:
        try:
            deep = _read_one(last_img, budget=SCAN_DEEP_BUDGET)
        except Exception:  # noqa: BLE001
            deep = []
        fresh = [t for t in deep if t not in seen and not is_marker(t)]
        if fresh:
            seen.update(fresh)
            texts.extend(fresh)
            complete = _looks_complete(texts)

    return texts, {"shots": shots, "frames": len(texts), "cycles": starts,
                   "complete": complete, "elapsed": time.monotonic() - started,
                   "need": need, "cap": cap,
                   # 最后一枪的画面：一无所获时调用方可以存下来，让「识别不了」
                   # 有据可查 —— 光靠嘴说定位不了是码太糊、框错地方还是被窗口挡了。
                   "image": last_img}


def _otsu(img):
    """Otsu 自动阈值二值化（纯 PIL，不依赖 numpy）。"""
    gray = img.convert("L")
    hist = gray.histogram()
    total = sum(hist)
    if not total:
        return gray
    sum_all = sum(i * h for i, h in enumerate(hist))
    best, threshold = -1.0, 128
    weight_bg = 0
    sum_bg = 0
    for level in range(256):
        weight_bg += hist[level]
        if not weight_bg:
            continue
        weight_fg = total - weight_bg
        if not weight_fg:
            break
        sum_bg += level * hist[level]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_all - sum_bg) / weight_fg
        var = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if var > best:
            best, threshold = var, level
    lut = [255 if v > threshold else 0 for v in range(256)]
    return gray.point(lut)


def _tiles(img, cols: int = 2, overlap: float = 0.12):
    """带重叠地切片：切断的码靠重叠区在邻块里补回来。"""
    w, h = img.width, img.height
    if w < 480 or h < 480:
        return
    step_x, step_y = w / cols, h / cols
    pad_x, pad_y = int(step_x * overlap), int(step_y * overlap)
    for r in range(cols):
        for c in range(cols):
            left = max(0, int(c * step_x) - pad_x)
            top = max(0, int(r * step_y) - pad_y)
            right = min(w, int((c + 1) * step_x) + pad_x)
            bottom = min(h, int((r + 1) * step_y) + pad_y)
            if right - left < 120 or bottom - top < 120:
                continue
            yield img.crop((left, top, right, bottom))


#: 固定阈值二值化的候选切点。水印是半透明的浅灰，二维码是纯黑，
#: 用低阈值把「不够黑」的像素全漂白，水印就被抹掉了；多档是因为水印深浅不可知。
#: 实测重水印（0.8 不透明度灰字铺满）原像素只能认 0~3 帧，扫一遍切点就能全认回来。
THRESHOLDS = (60, 80, 100, 120)


def _padded(img, ratio: float = 0.10):
    """四周补白：截图/框选把静区切掉了，是最常见的「怎么扫都扫不出」之一。

    二维码规范要求四周留 4 模块静区，少一圈 zxing 就定不到位。裁掉静区的
    图补白回来往往立刻能读，而且这一步几乎不花钱（一次 paste + 一次解码）。
    """
    from PIL import Image

    pad = max(8, int(min(img.width, img.height) * ratio))
    out = Image.new("L", (img.width + pad * 2, img.height + pad * 2), 255)
    out.paste(img.convert("L"), (pad, pad))
    return out


def _cheap_variants(img):
    """便宜的像素变换：补静区 → 固定阈值去屏幕水印 → Otsu → 反色 → 单通道 → 2 倍放大。

    屏幕/看图软件叠的水印是半透明浅色，二维码是纯黑；用低阈值把「不够黑」的
    像素全漂白，水印就没了。多档是因为水印深浅、透明度都不可知。
    """
    from PIL import Image

    yield _padded(img)
    gray = img.convert("L")
    for level in THRESHOLDS:
        yield gray.point([0 if v < level else 255 for v in range(256)])
    binarized = _otsu(img)
    yield binarized
    yield binarized.point(lambda v: 255 - v)
    if img.width * img.height <= UPSCALE_LIMIT:
        yield img.resize((img.width * 2, img.height * 2), Image.LANCZOS)


def _channel_variants(img):
    """彩色水印（蓝底白字之类）在单通道上往往直接消失，逐通道试一遍。"""
    if len(img.getbands()) < 3:
        return
    for channel in img.split()[:3]:
        yield channel


def _expensive_variants(img):
    """昂贵的兜底：3 倍放大（小码救命）、中值滤波、重叠切块。"""
    from PIL import Image, ImageFilter

    pixels = img.width * img.height
    # 3 倍放大：屏幕上的码被看/缩得很小（每模块不足 2px）时，实测 1/6 → 5/6
    if pixels * 9 <= UPSCALE_LIMIT * 4:
        yield img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    binarized = _otsu(img)
    if pixels <= UPSCALE_LIMIT * 2:
        yield binarized.filter(ImageFilter.MedianFilter(3))   # 去细线水印
    for tile in _tiles(img):
        yield tile
        yield _otsu(tile)


def decode_images(paths: Iterable[str], progress=None) -> list[str]:
    """把若干张图片/目录里的二维码全部扫出来，按出现顺序去重。

    同一张总览图里的多帧、多张单帧图混在一起都可以。

    多张图之间**并行**识别：zxing 的 ``read_barcodes`` 会放 GIL，实测 4 张
    同时读比一张张读快 2.4 倍。``progress`` 按「已读完几张图」回调。
    """
    from concurrent.futures import ThreadPoolExecutor

    files = _iter_images(paths)
    if not files:
        raise QRError("没有找到图片文件（支持 %s）" % "、".join(IMAGE_EXTS))

    total = len(files)
    # 只有一张图时值得深挖（可能是带水印的总览图）；多张时缺的帧在别的图里
    deep = total == 1

    def _one(path):
        try:
            return decode_image(path, deep=deep), None
        except Exception as exc:  # noqa: BLE001  坏图不该让整批失败
            return [], exc

    texts: list[str] = []
    seen = set()
    first_error = None
    if total == 1:
        found, error = _one(files[0])
        first_error = error
        for text in found:
            if text not in seen:
                seen.add(text)
                texts.append(text)
        _tick(progress, 1, 1, "识别")
    else:
        workers = max(1, min(DECODE_THREADS, total))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for done, (found, error) in enumerate(pool.map(_one, files), 1):
                if error is not None and first_error is None:
                    first_error = error
                for text in found:
                    if text not in seen:
                        seen.add(text)
                        texts.append(text)
                _tick(progress, done, total, "识别图片")
    if not texts:
        if first_error is not None:
            raise QRError(str(first_error))
        raise QRError(
            "这 %d 张图里没有识别到二维码%s。\n"
            "· 让整张码完整入镜、四周留白边，别把图缩得太小（屏幕上每模块至少 3 像素）\n"
            "· 如果是 1.4.1 之前生成的动图，请用新版重新生成一次 ——"
            "旧版每帧尺寸不一，GIF 会把大帧裁掉右/下边，任何扫码器都读不出\n"
            "· 一次不行别硬扛：「粘贴识别面板」可以一张一张累加，顺序随意"
            % (total, _animation_hint(files))
        )
    return _drop_garbage(texts)


def _drop_garbage(texts: list[str]) -> list[str]:
    """扔掉误识别出来的碎片。

    zxing 偶尔会从噪点里「读出」三五个字符（实测 129 帧的动图混进过一个 ``B40B``）。
    它本身没害处，但会作为「一个独立容器」进到还原流程里，把整批还原带崩
    （报「缺少起始标记」，用户看到的就是「明明所有码都扫到了却还原失败」）。

    只在这批结果里**确实有本工具的分段/容器**时才过滤 —— 万一用户拿本工具去读
    别人的二维码，那就原样返回，不能自作主张删东西。
    """
    heads = (ct.SEG_PREFIX, ct.BEGIN)
    if not any(t.lstrip().startswith(heads) for t in texts):
        return texts
    return [t for t in texts
            if t.lstrip().startswith(heads) or ct.parse_segments(t)]


def _animation_hint(files: list[str]) -> str:
    """失败时补一句「这里有个 N 帧的动图」—— 用户才知道自己给的是动图不是静图。"""
    try:
        from PIL import Image

        for path in files[:3]:
            with Image.open(path) as im:
                frames = int(getattr(im, "n_frames", 1))
                if frames > 1:
                    return "（其中 %s 是 %d 帧的动图）" % (os.path.basename(path), frames)
    except Exception:  # noqa: BLE001  诊断信息拿不到就不给，不能盖住真正的错误
        pass
    return ""


def unpack_images(
    paths: Iterable[str],
    out_dir: str | None = None,
    password: str | None = None,
    out_name: str | None = None,
    progress=None,
):
    """识别图片里的二维码 → 合并分帧 → 还原文件。"""
    texts = decode_images(paths, progress=progress)
    return ct.unpack_texts(texts, out_dir=out_dir, password=password, out_name=out_name)


def inspect_images(paths: Iterable[str]) -> dict:
    """只做体检不还原：报告扫到几帧、属于哪个文档、缺哪几帧。"""
    texts = decode_images(paths)
    segs: list[ct.Segment] = []
    for text in texts:
        segs.extend(ct.parse_segments(text))

    docs: dict[str, dict] = {}
    for seg in segs:
        info = docs.setdefault(seg.doc_id, {"total": seg.total, "got": set()})
        info["got"].add(seg.index)

    report = {"images": len(_iter_images(paths)), "frames": len(segs), "docs": []}
    for doc_id, info in sorted(docs.items()):
        missing = [i for i in range(1, info["total"] + 1) if i not in info["got"]]
        report["docs"].append({
            "doc_id": doc_id,
            "total": info["total"],
            "received": len(info["got"]),
            "missing": missing,
        })
    if not docs:
        report["reason"] = "扫到了内容但没有 #OPK1 分段头，可能不是本工具生成的二维码"
    return report
