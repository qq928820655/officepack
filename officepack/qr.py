"""二维码通道：把容器文本装进 QR 码，也能从截图 / 图片里扫回来。

分工很明确：
    qrcode     负责生成 —— 能显式指定纠错级别，输出标准 PNG，任何扫码工具都认
    zxing-cpp  负责识别 —— 对截图、缩放、轻微倾斜、多码同图都比较稳

一帧 = 一个自包含的 ``#OPK1`` 分段，格式与文本分片**完全一致**，
所以二维码还原直接复用 ``container.unpack_texts()``，不存在第二套合并逻辑。

容量（QR 40，字母数字模式）：
    L 4296 / M 3391 / Q 2420 / H 1852 字符
这里按 L 取 4000 作默认上限，留出头部与 meta 行的余量。
base32 的字符集是 A-Z2-7，正好落在字母数字模式里，所以每字节只花 1.6 个字符；
换成 base64（含小写）会掉回字节模式，容量反而更小 —— 这是 QR 通道固定用 base32 的原因。
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
# 载荷是 base32（字母数字模式 5.5 bit/字符），而头部与 Meta 行含 # : 等符号，
# 只能走字节模式（8 bit/字符）。只数字符的话，首帧带了 Meta 就会悄悄超容量。
_BITS_ALNUM = 5.5
_BITS_BYTE = 8.0
_SEGMENT_OVERHEAD_BITS = 40   # 模式指示符 + 字符计数 + 终止符的余量

#: 允许出现在二维码里的编码器（必须全是大写字母/数字）
ALNUM_CODECS = ("base32", "hex")

#: 总览图最多拼多少帧。再多图幅会大到几万像素，识别率反而下降
MAX_SHEET_TILES = 12

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff")


class QRError(ValueError):
    pass


# --------------------------------------------------------------------------
# 分帧
# --------------------------------------------------------------------------
def _payload_budget(ec: str, byte_overhead_chars: int) -> int:
    """在给定纠错级别下，扣掉头部/Meta 的字节开销后，载荷最多能放几个 base32 字符。"""
    cap = QR_ALNUM_CAPACITY.get((ec or DEFAULT_EC).upper())
    if cap is None:
        raise QRError("未知纠错级别 %r，可用：L / M / Q / H" % ec)
    bits = cap * _BITS_ALNUM - byte_overhead_chars * _BITS_BYTE - _SEGMENT_OVERHEAD_BITS
    return int(bits // _BITS_ALNUM)


def build_frames(
    pack: ct.PackResult,
    ec: str = DEFAULT_EC,
    chars_per_frame: int = 0,
) -> list[str]:
    """把打包结果切成一批「每张二维码装得下」的分段文本。

    第 1 帧额外带 Meta 行（Meta 只在首帧出现一次，省容量）。
    ``chars_per_frame`` 给了就再压一道，用于「宁可多几帧、每帧更好扫」的场合。
    """
    meta = pack.meta
    codec_name = meta.get("codec", "")
    if codec_name not in ALNUM_CODECS:
        raise QRError(
            "二维码通道需要纯大写字母/数字的编码器（%s），当前是 %r。"
            "请改用 base32。" % (" / ".join(ALNUM_CODECS), codec_name)
        )
    ec = (ec or DEFAULT_EC).upper()
    if ec not in QR_ALNUM_CAPACITY:
        raise QRError("未知纠错级别 %r，可用：L / M / Q / H" % ec)

    codec = codec_mod.get_codec(codec_name, meta.get("alphabet"))
    encoded = codec.encode(pack.payload)
    meta["text_chars"] = len(encoded)

    meta_line = ct.META_PREFIX + ct._meta_b64(meta)
    doc_id = meta["sha256"][:6]
    header = "#OPK1 %s 000/000 %s 0000000" % (doc_id, "AAAAA")

    first_cap = _payload_budget(ec, len(header) + 1 + len(meta_line) + 1)
    rest_cap = _payload_budget(ec, len(header) + 1)
    if chars_per_frame > 0:
        first_cap = min(first_cap, chars_per_frame)
        rest_cap = min(rest_cap, chars_per_frame)
    if first_cap <= 16:
        raise QRError(
            "二维码容量不足以放下元信息（纠错 %s 只剩 %d 字符），"
            "请改用更低的纠错级别（L）或换用更小的文件" % (ec, first_cap)
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
def _ec_const(ec: str):
    import qrcode.constants as C

    table = {
        "L": C.ERROR_CORRECT_L,
        "M": C.ERROR_CORRECT_M,
        "Q": C.ERROR_CORRECT_Q,
        "H": C.ERROR_CORRECT_H,
    }
    key = (ec or DEFAULT_EC).upper()
    if key not in table:
        raise QRError("未知纠错级别 %r，可用：L / M / Q / H" % ec)
    return table[key]


def _make_qr_image(text: str, ec: str, target_px: int = 960):
    """生成一张二维码 PIL 图；模块大小按目标像素反推，保证每帧清晰度一致。"""
    import qrcode

    proto = qrcode.QRCode(error_correction=_ec_const(ec), border=4, box_size=1)
    proto.add_data(text, optimize=20)
    proto.make(fit=True)
    modules = proto.modules_count + 8          # + 两侧各 4 模块静区
    box = max(3, int(round(target_px / modules)))

    qr = qrcode.QRCode(error_correction=_ec_const(ec), border=4, box_size=box)
    qr.add_data(text, optimize=20)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def render_images(
    frames: Sequence[str],
    out_dir: str,
    stem: str,
    ec: str = DEFAULT_EC,
    sheet: bool = True,
    target_px: int = 960,
) -> dict:
    """把每帧写成一张 PNG，可选再拼一张总览图。

    帧数超过 MAX_SHEET_TILES 时不拼总览图：几十张码拼到一起会有上万像素，
    扫起来反而更容易漏（实测 125 帧的 11700×11000 总览图会丢帧）。

    返回 ``{"images": [...], "sheet": 路径或 None, "dir": 目录}``。
    """
    if not frames:
        raise QRError("没有可渲染的帧")
    os.makedirs(out_dir, exist_ok=True)

    total = len(frames)
    images: list[str] = []
    tiles = []
    for idx, frame in enumerate(frames, 1):
        img = _make_qr_image(frame, ec, target_px)
        path = os.path.join(out_dir, "%s.qr%03d.png" % (stem, idx))
        img.save(path, "PNG")
        images.append(path)
        tiles.append((idx, img))

    sheet_path = None
    if sheet and 1 < total <= MAX_SHEET_TILES:
        sheet_path = os.path.join(out_dir, "%s.qr-all.png" % stem)
        _make_sheet(tiles, total, sheet_path, ec)
    return {"images": images, "sheet": sheet_path, "dir": out_dir}


def _make_sheet(tiles, total: int, path: str, ec: str) -> None:
    """把所有帧拼成一张总览图：一行截图就能把全部帧带回来。"""
    from PIL import Image, ImageDraw, ImageFont

    cols = max(1, int(math.ceil(math.sqrt(total))))
    rows = int(math.ceil(total / cols))
    cell = max(im.width for _, im in tiles)
    pad = 16
    label_h = 34

    width = cols * cell + pad * (cols + 1)
    height = rows * (cell + label_h) + pad * (rows + 1)
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except (OSError, IOError):
        font = ImageFont.load_default()

    for i, (idx, im) in enumerate(tiles):
        r, c = divmod(i, cols)
        x = pad + c * (cell + pad) + (cell - im.width) // 2
        y = pad + r * (cell + label_h + pad)
        sheet.paste(im, (x, y))
        label = "%d / %d" % (idx, total)
        try:
            tw = int(draw.textlength(label, font=font))
        except AttributeError:  # 老 Pillow
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
):
    """文本容器与二维码图片混在一起也能还原；返回 (结果列表, 统计)。"""
    images, texts = partition_sources(sources)
    blocks: list[str] = []
    if images:
        blocks.extend(decode_images(images))
    for path in texts:
        with open(path, "r", encoding="utf-8-sig") as fh:
            blocks.append(fh.read())
    if not blocks:
        raise QRError("没有可还原的输入")
    results = ct.unpack_texts(blocks, out_dir, password, out_name)
    return results, {"images": len(images), "texts": len(texts), "blocks": len(blocks)}


def decode_image(image) -> list[str]:
    """识别单张图里的所有二维码；可传路径、PIL 图或 numpy 数组。"""
    import zxingcpp
    from PIL import Image

    if isinstance(image, str):
        img = Image.open(image)
    else:
        img = image

    if not hasattr(img, "mode"):        # numpy 数组之类，交给 zxing 自己处理
        results = zxingcpp.read_barcodes(img)
        return [r.text for r in results if r.text]

    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")

    # 超大整屏截图先等比缩小：几十兆像素的图既慢又容易漏码
    pixels = img.width * img.height
    if pixels > 50_000_000:
        scale = (50_000_000 / pixels) ** 0.5
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                         Image.LANCZOS)

    try:
        results = zxingcpp.read_barcodes(img)
    except Exception as exc:  # 损坏图片 / 不支持的格式
        raise QRError("二维码识别失败：%s" % exc) from None
    return [r.text for r in results if r.text]


def decode_images(paths: Iterable[str]) -> list[str]:
    """把若干张图片/目录里的二维码全部扫出来，按出现顺序去重。

    同一张总览图里的多帧、多张单帧图混在一起都可以。
    """
    files = _iter_images(paths)
    if not files:
        raise QRError("没有找到图片文件（支持 %s）" % "、".join(IMAGE_EXTS))

    texts: list[str] = []
    seen = set()
    for path in files:
        for text in decode_image(path):
            if text not in seen:
                seen.add(text)
                texts.append(text)
    if not texts:
        raise QRError(
            "这 %d 张图里没有识别到二维码。截图时请让整张码完整入镜、"
            "不要过度缩放或加滤镜。" % len(files)
        )
    return texts


def unpack_images(
    paths: Iterable[str],
    out_dir: str | None = None,
    password: str | None = None,
    out_name: str | None = None,
):
    """识别图片里的二维码 → 合并分帧 → 还原文件。"""
    texts = decode_images(paths)
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
