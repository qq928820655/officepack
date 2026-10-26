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

#: 判断「这份数据压不压得动」的采样体积。已压缩格式（rar/zip/7z/docx/mp4）
#: 的局部熵和整体一样接近饱和，256KB 采样足以定性，成本却只有全量的 1.5%。
PROBE_BYTES = 256 * 1024
#: 低于这个体积仍然**全量**试全部 lzma 参数组：小文件全量试也只要几百毫秒，
#: 没必要为了省这点时间去赌采样选参的准确度。
TUNE_FULL_MAX = 2 * 1024 * 1024
#: 采样压缩率高于这个值（几乎压不动）就干脆不压：全量压一遍纯属白烧 CPU。
PROBE_GIVE_UP = 0.99


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


def _probe_sample(data: bytes) -> bytes:
    """从首/中/尾各取 1/3 拼成采样块，用来定性「这份数据压得动吗」。

    只取开头会误判：很多格式（zip/rar/7z）头部的目录区冗余度远高于数据区，
    只看头会说「能压」，结果压全量时白跑几十秒。
    """
    n = len(data)
    if n <= PROBE_BYTES:
        return data
    part = PROBE_BYTES // 3
    mid = (n - part) // 2
    return data[:part] + data[mid:mid + part] + data[n - part:]


def _lzma_stream(data: bytes, filt: dict, progress=None, step: str = "压缩") -> bytes:
    """分块喂 lzma，边压边报进度。

    输出与 ``lzma.compress(format=FORMAT_XZ, filters=[filt])`` **逐字节相同**
    （后者内部就是「LZMACompressor.compress 全量 + flush」），只是把一次
    几十秒的调用切成能报进度的若干块。
    """
    comp = lzma.LZMACompressor(format=lzma.FORMAT_XZ, filters=[filt])
    total = len(data)
    chunk = max(1 << 20, total // 40)      # 至少 40 段，进度才够细
    out: list[bytes] = []
    for start in range(0, total, chunk):
        out.append(comp.compress(data[start:start + chunk]))
        if progress is not None:
            progress(min(start + chunk, total), total, step)
    out.append(comp.flush())
    return b"".join(out)


def _lzma_best(data: bytes, size: int, level: int | None,
               progress=None) -> tuple[bytes, str]:
    """挑一组 lzma 参数压全量。

    大数据集不再「全量试 4 组」——实测 16.5MB 要跑 34.6 秒，压缩率却只差
    千分之几。改成先在 256KB 采样上比出最优参数组，再按这组压一次全量：
    实测 34.6s → 8.5s，同样的数据同样是 4 组里挑出来的那组。
    """
    cands = _lzma_candidates(size, level)
    if len(data) <= TUNE_FULL_MAX:
        best: tuple[bytes, str] | None = None
        for filt, tag in cands:
            try:
                packed = _lzma_stream(data, filt, progress)
            except (lzma.LZMAError, ValueError):
                continue
            if best is None or len(packed) < len(best[0]):
                best = (packed, tag)
        if best is not None:
            return best
        preset = _lzma_preset(size, level)
        return lzma.compress(data, preset=preset, format=lzma.FORMAT_XZ), "默认"

    sample = _probe_sample(data)
    best_tag, best_len = "默认", None
    for filt, tag in cands:
        try:
            got = len(_lzma_stream(sample, filt))
        except (lzma.LZMAError, ValueError):
            continue
        if best_len is None or got < best_len:
            best_tag, best_len = tag, got
    filt = next((f for f, t in cands if t == best_tag), cands[0][0])
    return _lzma_stream(data, filt, progress), best_tag



def compress(data: bytes, mode: str = "auto", level: int | None = None,
             progress=None) -> tuple[bytes, dict]:
    """返回 (压缩后字节, 压缩元信息)。

    ``progress(done, total, "压缩")`` 会在压缩过程中持续回调 —— 16MB 的
    lzma 要跑八秒，没有进度的话界面只能停在 0% 让人以为卡死。
    """
    key = (mode or "auto").lower()
    info = {"alg": "none", "level": level}

    if not data:
        info["alg"] = "none"
        return data, info

    if key == "none":
        return data, info

    # 大文件先花 0.2 秒探一下「压得动吗」。压不动还全量压一遍，16MB 就是
    # 白烧几十秒 CPU，最后结果照样被丢掉 —— 用户看到的就是「一直 0%」。
    if key in ("max", "auto") and len(data) > TUNE_FULL_MAX:
        sample = _probe_sample(data)
        try:
            probe = lzma.compress(sample, preset=_lzma_preset(len(data), level),
                                  format=lzma.FORMAT_XZ)
        except (lzma.LZMAError, ValueError):
            probe = None
        if probe is not None and len(probe) >= len(sample) * PROBE_GIVE_UP:
            info.update(auto_skipped=True, probe_skipped=True)
            return data, info

    if key == "max":
        # 多组 lzma 参数挑字符数最少的一组（慢但最省）
        packed, tag = _lzma_best(data, len(data), level, progress)
        if len(packed) >= len(data):
            return data, {"alg": "none", "level": None, "auto_skipped": True}
        return packed, {"alg": "lzma", "level": level, "tuned": tag}

    if key == "auto":
        preset = _lzma_preset(len(data), level)
        filt = {"id": lzma.FILTER_LZMA2, "preset": preset}
        packed = _lzma_stream(data, filt, progress)
        if len(packed) < len(data) * (1 - MIN_GAIN):
            return packed, {"alg": "lzma", "level": preset}
        # 压缩无收益：这部分数据本身已是压缩格式（如 docx/pptx）
        return data, {"alg": "none", "level": None, "auto_skipped": True}

    if key == "lzma":
        preset = _lzma_preset(len(data), level)
        filt = {"id": lzma.FILTER_LZMA2, "preset": preset}
        packed = _lzma_stream(data, filt, progress)
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
