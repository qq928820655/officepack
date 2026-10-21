"""可插拔压缩器：在编码成文本之前先把二进制压小。

顺序固定为   原始字节 → 压缩 → 加密 → 编码 → 文本   ，逆序还原。
先压缩再加密是唯一正确的顺序（加密后数据熵已满，无法压缩）。

- auto : 自动试 lzma，若压缩后没有变小（docx/pptx 本身已是 ZIP）则存原样
- lzma : xz 容器，压缩率最高，体积通常比 zlib 再小 15%~35%
- zlib : 兼容性最好，速度快
- bz2  : 介于两者之间
- none : 不压缩
"""

from __future__ import annotations

import bz2
import lzma
import zlib

LARGE_FILE = 64 * 1024 * 1024  # 超过 64MB 降低 lzma 预设，避免吃满内存
MIN_GAIN = 0.005  # auto 模式下至少省 0.5% 才采用压缩结果


class CompressError(ValueError):
    pass


def _lzma_preset(size: int, level: int | None) -> int:
    if level is not None:
        return level
    if size > LARGE_FILE:
        return 6
    return 9 | lzma.PRESET_EXTREME


def _lzma_candidates(size: int, level: int | None):
    """返回若干 lzma 参数组合：不同 lc/lp/pb 对不同数据类型各有优势。"""
    preset = _lzma_preset(size, level)
    base = {"id": lzma.FILTER_LZMA2, "preset": preset}
    return [
        (dict(base), "默认"),
        ({**base, "lc": 0, "lp": 0, "pb": 0}, "lc0pb0"),
        ({**base, "lc": 1, "lp": 0, "pb": 0}, "lc1pb0"),
        ({**base, "lc": 0, "lp": 1, "pb": 1}, "lc0lp1"),
    ]


def _lzma_best(data: bytes, size: int, level: int | None) -> tuple[bytes, str]:
    best: tuple[bytes, str] | None = None
    for filt, tag in _lzma_candidates(size, level):
        try:
            packed = lzma.compress(data, format=lzma.FORMAT_XZ, filters=[filt])
        except (lzma.LZMAError, ValueError):
            continue
        if best is None or len(packed) < len(best[0]):
            best = (packed, tag)
    if best is None:
        preset = _lzma_preset(size, level)
        return lzma.compress(data, preset=preset, format=lzma.FORMAT_XZ), "默认"
    return best


def compress(data: bytes, mode: str = "auto", level: int | None = None) -> tuple[bytes, dict]:
    """返回 (压缩后字节, 压缩元信息)。"""
    key = (mode or "auto").lower()
    info = {"alg": "none", "level": level}

    if not data:
        info["alg"] = "none"
        return data, info

    if key == "none":
        return data, info

    if key == "max":
        # 多组 lzma 参数逐个试，取字符数最少的一组（慢但最省）
        packed, tag = _lzma_best(data, len(data), level)
        if len(packed) >= len(data):
            return data, {"alg": "none", "level": None, "auto_skipped": True}
        return packed, {"alg": "lzma", "level": level, "tuned": tag}

    if key == "auto":
        preset = _lzma_preset(len(data), level)
        packed = lzma.compress(data, preset=preset, format=lzma.FORMAT_XZ)
        if len(packed) < len(data) * (1 - MIN_GAIN):
            return packed, {"alg": "lzma", "level": preset}
        # 压缩无收益：这部分数据本身已是压缩格式（如 docx/pptx）
        return data, {"alg": "none", "level": None, "auto_skipped": True}

    if key == "lzma":
        preset = _lzma_preset(len(data), level)
        packed = lzma.compress(data, preset=preset, format=lzma.FORMAT_XZ)
        return packed, {"alg": "lzma", "level": preset}

    if key == "zlib":
        lv = 9 if level is None else int(level)
        packed = zlib.compress(data, lv)
        return packed, {"alg": "zlib", "level": lv}

    if key == "bz2":
        lv = 9 if level is None else int(level)
        packed = bz2.compress(data, lv)
        return packed, {"alg": "bz2", "level": lv}

    raise CompressError(
        "未知压缩器 %r，可用：auto, lzma, zlib, bz2, none" % mode
    )


def decompress(data: bytes, info: dict | None) -> bytes:
    alg = (info or {}).get("alg", "none")
    if alg in ("none", None, ""):
        return data
    try:
        if alg == "lzma":
            return lzma.decompress(data, format=lzma.FORMAT_AUTO)
        if alg == "zlib":
            return zlib.decompress(data)
        if alg == "bz2":
            return bz2.decompress(data)
    except (lzma.LZMAError, zlib.error, OSError, ValueError) as exc:
        raise CompressError("解压失败（%s）：%s" % (alg, exc)) from None
    raise CompressError("未知压缩算法 %r，无法解压" % alg)
