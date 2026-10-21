"""可插拔文本编码器：把任意字节无损映射为可打印 ASCII 字符。

设计为「整段」编解码：caller 拿到完整字符串后按宽度自由折行，
解码时忽略所有空白，因此不需要关心行边界。

膨胀率（文本字符数 / 原始字节数）：
    base91  1.231  ← 最紧凑
    base85  1.250
    base64  1.334
    base32  1.600  ← 最耐复制（纯大写字母+数字）
    hex     2.000
"""

from __future__ import annotations

import base64
import binascii
import math

LINE_WIDTH = 100  # 输出文本每行字符数
DEFAULT_ALPHABET = (
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
)

# Henke base91 字母表：刻意排除了 - ' \ 三个易被渠道转义的字符
_B91_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "!#$%&()*+,./:;<=>?@[]^_`{|}~\""
)
_B91_REVERSE = {ch: i for i, ch in enumerate(_B91_ALPHABET)}


class CodecError(ValueError):
    pass


def _clean(text: str) -> str:
    return "".join(ch for ch in text if not ch.isspace())


class Codec:
    name = "base"
    expansion = 1.0

    def encode(self, data: bytes) -> str:  # pragma: no cover - 抽象
        raise NotImplementedError

    def decode(self, text: str, total_len: int) -> bytes:  # pragma: no cover
        raise NotImplementedError


class Base91Codec(Codec):
    name = "base91"
    expansion = math.log(256) / math.log(91)

    def encode(self, data: bytes) -> str:
        table = _B91_ALPHABET
        out = []
        buf = 0
        nbits = 0
        for byte in data:
            buf |= byte << nbits
            nbits += 8
            if nbits > 13:
                val = buf & 8191
                if val > 88:
                    buf >>= 13
                    nbits -= 13
                else:
                    val = buf & 16383
                    buf >>= 14
                    nbits -= 14
                out.append(table[val % 91])
                out.append(table[val // 91])
        if nbits:
            out.append(table[buf % 91])
            if nbits > 7 or buf > 90:
                out.append(table[buf // 91])
        return "".join(out)

    def decode(self, text: str, total_len: int) -> bytes:
        text = _clean(text)
        out = bytearray()
        buf = 0
        nbits = 0
        val = -1
        for ch in text:
            try:
                digit = _B91_REVERSE[ch]
            except KeyError:
                raise CodecError("字符 %r 不在 base91 字母表中" % ch) from None
            if val < 0:
                val = digit
                continue
            val += digit * 91
            buf |= val << nbits
            nbits += 13 if (val & 8191) > 88 else 14
            while nbits > 7:
                out.append(buf & 0xFF)
                buf >>= 8
                nbits -= 8
            val = -1
        if val + 1:
            out.append((buf | (val << nbits)) & 0xFF)
        if len(out) < total_len:
            raise CodecError(
                "解码长度不足：期望 %d 字节，实际 %d 字节" % (total_len, len(out))
            )
        return bytes(out[:total_len])


class Base85Codec(Codec):
    name = "base85"
    expansion = 1.25

    def encode(self, data: bytes) -> str:
        return base64.a85encode(data, adobe=False).decode("ascii")

    def decode(self, text: str, total_len: int) -> bytes:
        text = _clean(text)
        try:
            out = base64.a85decode(text, adobe=False)
        except (ValueError, binascii.Error) as exc:
            raise CodecError("base85 解码失败：%s" % exc) from None
        if len(out) < total_len:
            raise CodecError(
                "解码长度不足：期望 %d 字节，实际 %d" % (total_len, len(out))
            )
        return out[:total_len]


class Base64Codec(Codec):
    name = "base64"
    expansion = 4 / 3

    def encode(self, data: bytes) -> str:
        return base64.b64encode(data).decode("ascii").rstrip("=")

    def decode(self, text: str, total_len: int) -> bytes:
        text = _clean(text)
        text += "=" * ((-len(text)) % 4)
        try:
            out = base64.b64decode(text, validate=False)
        except (ValueError, binascii.Error) as exc:
            raise CodecError("base64 解码失败：%s" % exc) from None
        if len(out) < total_len:
            raise CodecError(
                "解码长度不足：期望 %d 字节，实际 %d" % (total_len, len(out))
            )
        return out[:total_len]


class Base32Codec(Codec):
    name = "base32"
    expansion = 8 / 5

    def encode(self, data: bytes) -> str:
        return base64.b32encode(data).decode("ascii").rstrip("=")

    def decode(self, text: str, total_len: int) -> bytes:
        text = _clean(text).upper()
        text += "=" * ((-len(text)) % 8)
        try:
            out = base64.b32decode(text)
        except (ValueError, binascii.Error) as exc:
            raise CodecError("base32 解码失败：%s" % exc) from None
        if len(out) < total_len:
            raise CodecError(
                "解码长度不足：期望 %d 字节，实际 %d" % (total_len, len(out))
            )
        return out[:total_len]


class HexCodec(Codec):
    name = "hex"
    expansion = 2.0

    def encode(self, data: bytes) -> str:
        return binascii.hexlify(data).decode("ascii").upper()

    def decode(self, text: str, total_len: int) -> bytes:
        text = _clean(text).upper()
        if len(text) % 2:
            text = text[:-1]
        try:
            out = binascii.unhexlify(text)
        except (ValueError, binascii.Error) as exc:
            raise CodecError("hex 解码失败：%s" % exc) from None
        if len(out) < total_len:
            raise CodecError(
                "解码长度不足：期望 %d 字节，实际 %d" % (total_len, len(out))
            )
        return out[:total_len]


class CustomCodec(Codec):
    """自定义 64 字符码表编码（6 bit 一组，尾部补零位）。

    码表由你指定 64 个不重复的可打印 ASCII 字符，
    相当于在编码层加一层轻量置换：拿到 wrong 码表解不出正确内容。
    """

    name = "custom"
    expansion = 4 / 3

    def __init__(self, alphabet: str | None = None):
        alphabet = "".join(dict.fromkeys(alphabet or DEFAULT_ALPHABET))
        if len(alphabet) != 64:
            raise CodecError(
                "自定义码表必须正好 64 个不重复字符，当前 %d 个" % len(alphabet)
            )
        if any(not (32 <= ord(ch) < 127) for ch in alphabet):
            raise CodecError("自定义码表只能包含可打印 ASCII 字符（32-126）")
        self.alphabet = alphabet
        self._reverse = {ch: i for i, ch in enumerate(alphabet)}

    def encode(self, data: bytes) -> str:
        out = []
        buf = 0
        nbits = 0
        for byte in data:
            buf = (buf << 8) | byte
            nbits += 8
            while nbits >= 6:
                nbits -= 6
                out.append(self.alphabet[(buf >> nbits) & 0x3F])
        if nbits:
            out.append(self.alphabet[(buf << (6 - nbits)) & 0x3F])
        return "".join(out)

    def decode(self, text: str, total_len: int) -> bytes:
        out = bytearray()
        buf = 0
        nbits = 0
        for ch in _clean(text):
            try:
                digit = self._reverse[ch]
            except KeyError:
                raise CodecError("字符 %r 不在自定义码表中" % ch) from None
            buf = (buf << 6) | digit
            nbits += 6
            if nbits >= 8:
                nbits -= 8
                out.append((buf >> nbits) & 0xFF)
        if len(out) < total_len:
            raise CodecError(
                "解码长度不足：期望 %d 字节，实际 %d" % (total_len, len(out))
            )
        return bytes(out[:total_len])


# 汉字码表：U+4E00 起连续 16384 个 CJK 统一汉字（14 bit 对齐）
_CJK_BASE = 0x4E00
_CJK_SIZE = 16384
_CJK_TABLE = "".join(chr(_CJK_BASE + i) for i in range(_CJK_SIZE))


class CjkCodec(Codec):
    """汉字编码：每 14 bit 映射为 1 个汉字，字符数比 base91 再少 54%。

    字符表为 U+4E00 起的连续 16384 个 CJK 统一汉字 —— 全部是常用汉字，
    字体覆盖好、Unicode NFC/NFD 归一化下保持稳定、不会被输入框当特殊字符过滤。

    这是唯一「文本字符数少于原始字节数」的编码器（0.571 字符/字节），
    用于每次只能输入少量字符的场景。代价是 UTF-8 字节数约 1.71 倍。
    """

    name = "cjk"
    expansion = 8 / 14

    _BASE = _CJK_BASE
    _SIZE = _CJK_SIZE
    _TABLE = _CJK_TABLE
    _MASK = _CJK_SIZE - 1

    def encode(self, data: bytes) -> str:
        table = self._TABLE
        out = []
        buf = 0
        nbits = 0
        for byte in data:
            buf = ((buf << 8) | byte) & 0x3FFFFF
            nbits += 8
            while nbits >= 14:
                nbits -= 14
                out.append(table[(buf >> nbits) & self._MASK])
            buf &= (1 << nbits) - 1
        if nbits:
            out.append(table[(buf << (14 - nbits)) & self._MASK])
        return "".join(out)

    def decode(self, text: str, total_len: int) -> bytes:
        out = bytearray()
        buf = 0
        nbits = 0
        base = self._BASE
        for ch in text:
            if ch.isspace():
                continue
            digit = ord(ch) - base
            if not (0 <= digit < self._SIZE):
                raise CodecError(
                    "字符 %r (U+%04X) 不在汉字码表范围 U+%04X-U+%04X 内"
                    % (ch, ord(ch), base, base + self._SIZE - 1)
                )
            buf = (buf << 14) | digit
            nbits += 14
            while nbits >= 8:
                nbits -= 8
                out.append((buf >> nbits) & 0xFF)
            buf &= (1 << nbits) - 1
        if len(out) < total_len:
            raise CodecError(
                "解码长度不足：期望 %d 字节，实际 %d" % (total_len, len(out))
            )
        return bytes(out[:total_len])


CODECS: dict[str, type[Codec]] = {
    Base91Codec.name: Base91Codec,
    Base85Codec.name: Base85Codec,
    Base64Codec.name: Base64Codec,
    Base32Codec.name: Base32Codec,
    HexCodec.name: HexCodec,
    CustomCodec.name: CustomCodec,
    CjkCodec.name: CjkCodec,
}
def get_codec(name: str, alphabet: str | None = None) -> Codec:
    key = (name or "base91").lower()
    if key not in CODECS:
        raise CodecError(
            "未知编码器 %r，可用：%s" % (name, ", ".join(sorted(CODECS)))
        )
    if key == CustomCodec.name:
        return CustomCodec(alphabet)
    return CODECS[key]()
