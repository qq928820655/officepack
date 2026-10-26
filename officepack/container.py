"""容器格式：把任意二进制文件无损封装为纯 ASCII 文本，并可完全还原。

文本骨架（可直接放进邮件正文、IM、Excel 单元格或 .txt 文件）：

    -----BEGIN OFFICEPACK v1-----
    Meta: eyJ2IjoxLCJuYW1lIjoi...
    Info: 报表.docx | 48213 B | 文本 59187 字符 | sha256=ab12… | base91 | lzma | aes-256-gcm
    Preview:
      | 这里是人可直接阅读的正文预览（不参与还原）
    Payload:
    MZXW6YTBOI...
    -----END OFFICEPACK v1-----

还原只依赖 Meta 行 + Payload 行，其余行（Info / Preview / 注释）一律忽略。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import codecs as codec_mod
from . import compress as compress_mod
from . import crypto as crypto_mod
from .preview import extract_preview

VERSION = 1
BEGIN = "-----BEGIN OFFICEPACK v1-----"
END = "-----END OFFICEPACK v1-----"
META_PREFIX = "Meta: "
PAYLOAD_HEADER = "Payload:"
PREVIEW_HEADER = "Preview:"
PREVIEW_PREFIX = "  | "
TOOL_ID = "officepack/1.0"

DEFAULT_CODEC = "base91"
DEFAULT_COMPRESS = "auto"

# 预设：一行搞定「体积」和「兼容性」的取舍
PRESETS = {
    "qr": {"codec": "qr45", "compress": "max"},           # 单张二维码装最多（1.5 字符/字节）
    "chars": {"codec": "cjk", "compress": "max"},         # 字符数最少（万分之级目标）
    "compact": {"codec": "base91", "compress": "auto"},   # ASCII 体积最小
    "safe": {"codec": "base32", "compress": "auto"},      # 纯大写+数字，最耐复制
    "fast": {"codec": "base64", "compress": "zlib"},      # 速度优先
    "plain": {"codec": "base64", "compress": "none"},     # 不压缩，最透明
}

DEFAULT_TARGET_CHARS = 10000  # 目标：整份文档的文本总量控制在万字级


class ContainerError(ValueError):
    pass


@dataclass
class PackResult:
    meta: dict
    payload: bytes          # 已压缩+加密的字节流（编码前的最终密文）
    preview: str = ""
    ascii_only: bool = False
    #: 已编码好的文本。打包时算过一次，出码/分段直接复用 —— 16.5MB 走 qr45
    #: 编码一次就要 5 秒，没理由每往下走一步都重算一遍。
    encoded: str | None = None

    @property
    def text_chars(self) -> int:
        return int(self.meta.get("text_chars", 0))

    def encode_text(self) -> str:
        """取得（必要时才计算）编码后的文本。"""
        if self.encoded is None:
            codec = codec_mod.get_codec(self.meta["codec"], self.meta.get("alphabet"))
            self.encoded = codec.encode(self.payload)
            self.meta["text_chars"] = len(self.encoded)
        return self.encoded


@dataclass
class UnpackResult:
    name: str
    raw: bytes
    meta: dict = field(default_factory=dict)
    verified: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _aad(sha256_hex: str, size: int) -> bytes:
    return ("%s|v%d|%s|%d" % (TOOL_ID, VERSION, sha256_hex, size)).encode("ascii")


def _wrap(text: str, width: int = codec_mod.LINE_WIDTH) -> list[str]:
    return [text[i : i + width] for i in range(0, len(text), width)] or [""]


def _expected_enc_size(plain_size: int, cipher_meta: dict) -> int:
    alg = (cipher_meta or {}).get("alg", "none")
    if alg == "none":
        return plain_size
    chunk = int(cipher_meta.get("chunk", crypto_mod.CHUNK_SIZE))
    full, last = divmod(plain_size, chunk)
    total = full * (chunk + crypto_mod.TAG_LEN)
    if last:
        total += last + crypto_mod.TAG_LEN
    return total


#: 打包流水线的进度刻度总数。三段各自耗时差异极大（16.5MB 时压缩 8s、
#: 加密 0.5s、编码 5s），合成一条 0~1000 的进度才不会被某一段「吃掉」。
PACK_SCALE = 1000

#: 能按 N 字节对齐就分块编码的编码器（都是定长分组映射，切开拼回去逐字节相同）。
#: 不在表里的（base91 这类带跨块 bit 状态的）只能整块编码。
_ENCODE_ALIGN = {"qr45": 2, "base64": 3, "base32": 5, "hex": 1}


def _fire(progress, done: int, step: str) -> None:
    if progress is not None:
        try:
            progress(int(done), PACK_SCALE, step)
        except Exception:  # noqa: BLE001  进度回调绝不能把打包搞崩
            pass


def _stage_progress(progress, base: int, span: int, step: str):
    """把某一段自己的 done/total 换算到总刻度 base..base+span 上。"""
    if progress is None:
        return None

    def report(done, total, _step: str = "") -> None:
        try:
            ratio = 0.0 if not total else min(1.0, max(0.0, float(done) / total))
            progress(int(base + span * ratio), PACK_SCALE, step)
        except Exception:  # noqa: BLE001
            pass

    return report


def _encode_text(codec, data: bytes, progress=None) -> str:
    """把密文编码成文本；能分块的编码器按块报进度。

    对齐切分保证每块都是编码器的完整分组，拼回去与整体编码逐字节相同。
    """
    align = _ENCODE_ALIGN.get(getattr(codec, "name", ""), 0)
    total = len(data)
    if not align or total <= (1 << 20):
        text = codec.encode(data) if total else ""
        if progress is not None:
            progress(1, 1, "编码")
        return text
    chunk = max(1 << 20, (total // 40 + align - 1) // align * align)
    out: list[str] = []
    for start in range(0, total, chunk):
        out.append(codec.encode(data[start:start + chunk]))
        if progress is not None:
            progress(min(start + chunk, total), total, "编码")
    return "".join(out)


def pack_bytes(
    raw: bytes,
    name: str,
    *,
    codec_name: str = DEFAULT_CODEC,
    alphabet: str | None = None,
    cipher_name: str = "none",
    password: str | None = None,
    compress: str = DEFAULT_COMPRESS,
    level: int | None = None,
    preview: bool = True,
    ascii_only: bool = False,
    progress=None,
) -> PackResult:
    """打包一段字节。

    ``progress(done, total, 阶段)`` 把「压缩 → 加密 → 编码」三段合成一条
    0~1000 的进度（``total`` 固定 1000）。不拆开的话，16.5MB 的文件在这三步
    上要花一分钟，界面上就只能挂个「0%」，看着像死了。
    """
    codec = codec_mod.get_codec(codec_name, alphabet)
    sha256_hex = hashlib.sha256(raw).hexdigest()
    size = len(raw)

    body, compress_info = compress_mod.compress(
        raw, compress, level, progress=_stage_progress(progress, 0, 700, "压缩"))
    cipher = crypto_mod.make_cipher(cipher_name, password, len(body))
    aad = _aad(sha256_hex, size)
    _fire(progress, 700, "加密")
    encrypted, cipher_meta = cipher.encrypt(body, aad)
    cipher_meta["plain_size"] = len(body)

    ext = os.path.splitext(name)[1].lower()
    text_preview = extract_preview(ext, raw) if preview else ""
    encoded = _encode_text(codec, encrypted,
                           progress=_stage_progress(progress, 770, 230, "编码"))

    meta = {
        "v": VERSION,
        "name": name,
        "size": size,
        "sha256": sha256_hex,
        "ext": ext,
        "codec": codec.name,
        "compress": compress_info,
        "plain_size": len(body),
        "enc_size": len(encrypted),
        "text_chars": len(encoded),
        "ratio": round(size / len(encoded), 3) if encoded else 0,
        "cipher": cipher_meta,
        "created": _now(),
        "tool": TOOL_ID,
    }
    if codec.name == "custom":
        meta["alphabet"] = codec.alphabet  # type: ignore[attr-defined]
    return PackResult(meta=meta, payload=encrypted, preview=text_preview,
                      ascii_only=ascii_only, encoded=encoded)



def _ascii_safe(text: str) -> str:
    """把任意文本压成 7-bit ASCII（中文转 \\uXXXX 转义）。"""
    return text.encode("ascii", "backslashreplace").decode("ascii")


def render(pack: PackResult) -> str:
    meta = pack.meta
    # 打包时已经编码过一次，这里直接复用（16.5MB 走 qr45 再编一次要 5 秒）
    encoded = pack.encode_text()
    meta["text_chars"] = len(encoded)

    meta_b64 = _meta_b64(meta)

    name_shown = meta["name"]
    preview = pack.preview
    if pack.ascii_only:
        name_shown = _ascii_safe(name_shown)
        preview = ""  # 严格 ASCII 模式下不写中文预览，避免破坏 7-bit 通道

    buf = io.StringIO()
    buf.write(BEGIN + "\n")
    buf.write(META_PREFIX + meta_b64 + "\n")
    buf.write(
        "Info: %s | %d B | text=%d chars | codec=%s | compress=%s | cipher=%s\n"
        % (
            name_shown,
            meta["size"],
            meta["text_chars"],
            meta["codec"],
            (meta.get("compress") or {}).get("alg", "none"),
            (meta.get("cipher") or {}).get("alg", "none"),
        )
    )
    if preview:
        buf.write(PREVIEW_HEADER + "\n")
        for line in preview.splitlines():
            buf.write(PREVIEW_PREFIX + line.rstrip() + "\n")
    buf.write(PAYLOAD_HEADER + "\n")
    for line in _wrap(encoded):
        buf.write(line + "\n")
    buf.write(END + "\n")
    return buf.getvalue()


def default_out_path(src: str, codec_name: str) -> str:
    return "%s.%s.opk.txt" % (src, codec_name)


DEFAULT_SEGMENT_CHARS = 10000  # 每段默认上限：一万字符


def pack_file(
    src: str,
    out_path: str | None = None,
    *,
    codec_name: str = DEFAULT_CODEC,
    alphabet: str | None = None,
    cipher_name: str = "none",
    password: str | None = None,
    compress: str = DEFAULT_COMPRESS,
    level: int | None = None,
    preview: bool = True,
    ascii_only: bool = False,
    segment: int = 0,
    parts: int = 0,
    progress=None,
) -> tuple[list[str], dict]:
    """打包文件；返回 (输出路径列表, 元信息)。分段时返回多个路径。"""
    with open(src, "rb") as fh:
        raw = fh.read()
    result = pack_bytes(
        raw,
        os.path.basename(src),
        codec_name=codec_name,
        alphabet=alphabet,
        cipher_name=cipher_name,
        password=password,
        compress=compress,
        level=level,
        preview=preview,
        ascii_only=ascii_only,
        progress=progress,
    )

    if segment or parts:
        chunks = split_pack(result, max_chars=segment, parts=parts)
        base = out_path or default_out_path(src, result.meta["codec"])
        stem = base[:-4] if base.lower().endswith(".txt") else base
        paths: list[str] = []
        for idx, seg_text in enumerate(chunks, 1):
            path = "%s.part%03d.txt" % (stem, idx)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(seg_text)
            paths.append(path)
        return paths, result.meta

    if out_path is None:
        out_path = default_out_path(src, result.meta["codec"])
    text = render(result)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return [out_path], result.meta


SEG_PREFIX = "#OPK1"
_B91 = codec_mod._B91_ALPHABET


@dataclass
class Segment:
    """一个可独立传输的文本片段。"""

    doc_id: str
    index: int
    total: int
    crc: str
    length: int | None = None
    lines: list[str] = field(default_factory=list)
    meta_line: str | None = None

    @property
    def body(self) -> str:
        text = "".join(self.lines)
        return text[: self.length] if self.length is not None else text


def _crc91(text: str) -> str:
    """把 CRC32 编成 5 个 base91 字符（91^5 > 2^32）。"""
    value = zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF
    out = []
    for _ in range(5):
        out.append(_B91[value % 91])
        value //= 91
    return "".join(reversed(out))


def _meta_b64(meta: dict) -> str:
    """把 meta 序列化成一行 base64。

    能压小就压小：JSON 里 ``sha256``、``salt``、``nonce`` 这些字段压缩率很高，
    zlib 后通常只剩原文的 55~70%。压缩版用一个 `~` 打头（不在 base64 字母表里），
    解码端据此区分 —— 所以 Meta 行前缀、分段解析全都不用改，老文本照样能读。

    meta 省下的每个字符，在二维码里都是一张码能多装的字节（首帧曾为它让出 640 字符）。
    """
    raw = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    plain = base64.b64encode(raw).decode("ascii")
    packed = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    return "~" + packed if len(packed) + 1 < len(plain) else plain


def _meta_decode(meta_b64: str) -> dict:
    """还原 ``_meta_b64`` 的两种写法（明文 / ``~`` 打头的压缩版）。"""
    if meta_b64.startswith("~"):
        raw = zlib.decompress(base64.b64decode(meta_b64[1:].encode("ascii")))
    else:
        raw = base64.b64decode(meta_b64.encode("ascii"))
    return json.loads(raw.decode("utf-8"))


def _segment_count(total: int, budget: int, meta_len: int) -> int:
    first = budget - meta_len
    if first <= 0:
        return 1 << 30
    if total <= first:
        return 1
    return 1 + (total - first + budget - 1) // budget


def split_pack(
    pack: PackResult,
    max_chars: int = 0,
    parts: int = 0,
) -> list[str]:
    """把容器切成多个片段，每段字符数不超过 max_chars（或尽量接近 parts 段）。

    段 1 额外携带 Meta 行；其余段只有一行头部 + 载荷字符。
    每段头部含文档 ID、序号/总数、段 CRC32 —— 可乱序收集，缺段/截断能立刻定位。
    """
    meta = pack.meta
    encoded = pack.encode_text()
    meta["text_chars"] = len(encoded)
    meta_line = META_PREFIX + _meta_b64(meta)
    meta_len = len(meta_line) + 1
    total_chars = len(encoded)
    doc_id = meta["sha256"][:6]

    overhead = len("#OPK1 %s 000/000 %s 0000000\n" % (doc_id, "AAAAA"))

    if max_chars:
        budget = max_chars - overhead
        if budget - meta_len <= 0:
            raise ContainerError(
                "每段字符数太小：至少需要 %d 字符（第 1 段要放下元信息）"
                % (overhead + meta_len + 16)
            )
    elif parts:
        if parts < 1:
            raise ContainerError("段数必须 >= 1")
        if parts == 1:
            return [render(pack)]
        lo, hi = 1, total_chars + meta_len + 1
        while lo < hi:
            mid = (lo + hi) // 2
            if _segment_count(total_chars, mid, meta_len) <= parts:
                hi = mid
            else:
                lo = mid + 1
        budget = lo
    else:
        return [render(pack)]

    chunks: list[str] = []
    rest = encoded
    capacity = budget - meta_len
    while rest:
        chunks.append(rest[:capacity])
        rest = rest[capacity:]
        capacity = budget
    if not chunks:  # 空载荷
        chunks = [""]

    n = len(chunks)
    if n == 1:
        # 内容本来就没超限，不必分段，直接给标准容器
        return [render(pack)]
    out: list[str] = []
    for idx, chunk in enumerate(chunks, 1):
        header = "%s %s %03d/%03d %s %d" % (
            SEG_PREFIX, doc_id, idx, n, _crc91(chunk), len(chunk)
        )
        pieces = [header]
        if idx == 1:
            pieces.append(meta_line)
        pieces.extend(_wrap(chunk))
        out.append("\n".join(pieces) + "\n")
    return out


def parse_segments(text: str) -> list[Segment]:
    """从任意文本中提取所有分段（一个文件可含多段，段间可夹无关文字）。

    段头格式：``#OPK1 <docId> <序号>/<总数> <CRC> <段字符数>``
    末字段让解析器能精确截断，多粘的尾部杂字会被忽略而不是破坏校验。
    """
    segs: list[Segment] = []
    cur: Segment | None = None
    filled = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(SEG_PREFIX):
            fields = line.split()
            cur = None
            filled = 0
            if len(fields) >= 4 and "/" in fields[2]:
                try:
                    idx_s, total_s = fields[2].split("/", 1)
                    seg = Segment(fields[1], int(idx_s), int(total_s), fields[3])
                except ValueError:
                    continue
                if len(fields) >= 5:
                    try:
                        seg.length = int(fields[4])
                    except ValueError:
                        seg.length = None
                if seg.index >= 1 and seg.total >= seg.index:
                    segs.append(seg)
                    cur = seg
            continue
        if cur is None:
            continue
        if line.startswith(META_PREFIX) and cur.index == 1 and cur.meta_line is None:
            cur.meta_line = line
            continue
        if cur.length is not None and filled >= cur.length:
            continue  # 该段已收满，尾部杂字忽略
        cur.lines.append(line)
        filled += len(line)
    return segs


def join_segments(segs: list[Segment]) -> str:
    """把片段合并回标准容器文本；缺段/重复/校验失败都直接报错。"""
    if not segs:
        raise ContainerError("没有找到任何分段（缺少 #OPK1 头部）")

    doc_ids = {s.doc_id for s in segs}
    if len(doc_ids) > 1:
        raise ContainerError(
            "混入了多个文档的分段：%s。请分开还原。" % "、".join(sorted(doc_ids))
        )
    total = segs[0].total
    if any(s.total != total for s in segs):
        raise ContainerError("各段声明的总段数不一致，可能来自不同次打包")

    # 先用 CRC 把「扫花了」的帧筛掉，再判断重复。
    # 这样同一张码扫了两遍（其中一遍截断/糊了）不会把整次还原拖死 ——
    # 逐帧 CRC 正是为此存在的：坏帧丢弃，好帧照样拼得回去。
    broken = [s for s in segs if _crc91(s.body) != s.crc]
    intact = [s for s in segs if _crc91(s.body) == s.crc]

    by_index: dict[int, Segment] = {}
    for seg in intact:
        first = by_index.get(seg.index)
        if first is None:
            by_index[seg.index] = seg
        elif first.body != seg.body:
            raise ContainerError(
                "第 %d 段出现了两份不同内容（可能是两次打包混在一起）" % seg.index
            )
        # 内容完全相同的重复帧直接忽略：多扫几遍是无害的

    missing = [i for i in range(1, total + 1) if i not in by_index]
    if missing:
        hint = ""
        if broken:
            bad = sorted({s.index for s in broken})
            hint = "；另有第 %s 段校验失败（扫歪/截断），重扫这几张即可" % "、".join(
                str(i) for i in bad)
        raise ContainerError(
            "缺少第 %s 段（共需 %d 段，已收到 %d 段）%s"
            % ("、".join(str(i) for i in missing), total, len(by_index), hint)
        )

    meta_line = next((s.meta_line for s in intact if s.meta_line), None)
    if not meta_line:
        raise ContainerError("未找到 Meta 行，第 1 段可能缺失或已损坏")

    payload = "".join(by_index[i].body for i in range(1, total + 1))
    return "\n".join([BEGIN, meta_line, PAYLOAD_HEADER, payload, END]) + "\n"


def read_container(text: str) -> tuple[dict, str]:
    meta_b64 = None
    payload: list[str] = []
    in_payload = False
    found_begin = False
    found_end = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == BEGIN:
            found_begin = True
            continue
        if stripped == END:
            found_end = True
            break
        if not found_begin:
            continue
        if not in_payload:
            if stripped.startswith(META_PREFIX):
                meta_b64 = stripped[len(META_PREFIX) :]
            elif stripped == PAYLOAD_HEADER:
                in_payload = True
            continue
        payload.append(stripped)

    if not found_begin:
        raise ContainerError("不是有效的 OFFICEPACK 文本：缺少起始标记")
    if meta_b64 is None:
        raise ContainerError("容器缺少 Meta 行")
    if not found_end:
        raise ContainerError("容器不完整：缺少结束标记（文本可能被截断）")

    try:
        meta = _meta_decode(meta_b64)
    except Exception as exc:
        raise ContainerError("Meta 解析失败：%s" % exc) from None
    if meta.get("v") != VERSION:
        raise ContainerError("不支持的容器版本：%r" % meta.get("v"))
    return meta, "".join(payload)


def unpack_text(text: str, password: str | None = None) -> UnpackResult:
    if SEG_PREFIX in text:
        text = join_segments(parse_segments(text))
    meta, payload_text = read_container(text)
    codec = codec_mod.get_codec(meta["codec"], meta.get("alphabet"))
    cipher_meta = dict(meta.get("cipher") or {"alg": "none"})
    enc_size = int(
        meta.get("enc_size")
        or _expected_enc_size(int(meta.get("plain_size", 0)), cipher_meta)
    )
    encrypted = codec.decode(payload_text, enc_size)

    plain_size = int(meta.get("plain_size", len(encrypted)))
    cipher_meta["plain_size"] = plain_size
    cipher = crypto_mod.cipher_from_meta({"cipher": cipher_meta}, password, plain_size)
    aad = _aad(meta["sha256"], int(meta["size"]))
    body = cipher.decrypt(encrypted, cipher_meta, aad)

    raw = compress_mod.decompress(body, meta.get("compress"))

    if len(raw) != int(meta["size"]):
        raise ContainerError(
            "长度校验失败：期望 %d 字节，实际 %d 字节" % (meta["size"], len(raw))
        )
    digest = hashlib.sha256(raw).hexdigest()
    if digest != meta["sha256"]:
        raise ContainerError(
            "SHA-256 校验失败，内容已损坏或被篡改\n  期望 %s\n  实际 %s"
            % (meta["sha256"], digest)
        )
    return UnpackResult(name=meta["name"], raw=raw, meta=meta, verified=True)


def _write_result(
    result: UnpackResult,
    target_dir: str,
    out_name: str | None = None,
) -> str:
    os.makedirs(target_dir, exist_ok=True)
    filename = out_name or os.path.basename(result.name) or "restored.bin"
    out_path = os.path.join(target_dir, filename)
    stem, ext = os.path.splitext(out_path)
    counter = 1
    while os.path.exists(out_path):
        out_path = "%s_%d%s" % (stem, counter, ext)
        counter += 1
    with open(out_path, "wb") as fh:
        fh.write(result.raw)
    return out_path


def unpack_file(
    src: str,
    out_dir: str | None = None,
    password: str | None = None,
    out_name: str | None = None,
) -> tuple[str, dict]:
    with open(src, "r", encoding="utf-8-sig", errors="strict") as fh:
        text = fh.read()
    result = unpack_text(text, password)
    target_dir = out_dir or os.path.dirname(os.path.abspath(src))
    return _write_result(result, target_dir, out_name), result.meta


def default_unpack_dir() -> str:
    """没有源文件可参照时的默认输出位置：桌面 → 用户目录 → 当前目录。"""
    home = os.path.expanduser("~")
    for cand in (os.path.join(home, "Desktop"), home):
        if os.path.isdir(cand):
            return cand
    return os.getcwd()


def unpack_pasted(
    text: str,
    out_dir: str | None = None,
    password: str | None = None,
    out_name: str | None = None,
) -> tuple[str, dict]:
    """从手动输入/粘贴的文本还原。

    支持以下输入形态：
      * 一个完整容器（BEGIN ... END）
      * 一个分片
      * 多个分片直接首尾相接 / 换行分隔（顺序随意）
      * 分片之间夹着任意说明文字（会被忽略）
    """
    result = unpack_text(text, password)
    target = out_dir or default_unpack_dir()
    return _write_result(result, target, out_name), result.meta


def unpack_texts(
    texts: list[str],
    out_dir: str | None = None,
    password: str | None = None,
    out_name: str | None = None,
) -> list[tuple[str, dict]]:
    """还原若干段文本；本次调用中同一文档的多个分片会自动合并成一个输出。"""
    groups: dict[str, list[Segment]] = {}
    standalone: list[str] = []

    for text in texts:
        if not text.strip():
            continue
        segs = parse_segments(text)
        if segs:
            for seg in segs:
                groups.setdefault(seg.doc_id, []).append(seg)
        else:
            standalone.append(text)

    target_dir = out_dir or default_unpack_dir()
    results: list[tuple[str, dict]] = []
    for doc_id in sorted(groups):
        result = unpack_text(join_segments(groups[doc_id]), password)
        results.append((_write_result(result, target_dir, out_name), result.meta))
    # 独立的整容器（不是分片）。这里要**宽容**一点：扫码时 zxing 偶尔会从噪点里
    # 「读出」三五个字符（实测 129 帧的动图混进过一个 `B40B`），如果它作为一块
    # 独立文本去解析就会抛「缺少起始标记」，把整批还原全带崩。已经收到正常分片时
    # 直接忽略解不动的块；只有**全都**解不动才把原始报错抛出去。
    failed: Exception | None = None
    for text in standalone:
        try:
            result = unpack_text(text, password)
        except ContainerError as exc:
            failed = failed or exc
            continue
        results.append((_write_result(result, target_dir, out_name), result.meta))
    if not results:
        if failed is not None:
            raise failed
        raise ContainerError("没有可还原的内容")
    return results


def unpack_sources(
    paths: list[str],
    out_dir: str | None = None,
    password: str | None = None,
    out_name: str | None = None,
) -> list[tuple[str, dict]]:
    """还原一个或多个文件；自动把同一文档的分段合并成一个输出。

    支持：单个完整容器、单个分段文件、多个分段文件、含多段的单个文本文件，
    以及**二维码图片**（扩展名是图片就自动扫码，不再当文本读 —— 以前直接
    ``open(..., "r")`` 撞上 PNG 会抛 UnicodeDecodeError）。
    """
    from . import qr as _qr          # 惰性导入：qr 反过来依赖本模块

    texts: list[str] = []
    images: list[str] = []
    for path in paths:
        if _qr.is_image_path(path):
            images.append(path)
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", errors="strict") as fh:
                texts.append(fh.read())
        except (OSError, UnicodeDecodeError) as exc:
            raise ContainerError("读取 %s 失败：%s" % (os.path.basename(path), exc)) from None
    if images:
        texts.extend(_qr.decode_images(images))
    # 未指定输出目录时，默认写在第一个源文件旁边
    if out_dir is None and paths:
        out_dir = os.path.dirname(os.path.abspath(paths[0]))
    return unpack_texts(texts, out_dir, password, out_name)


def inspect_text(text: str) -> dict:
    if SEG_PREFIX in text:
        segs = parse_segments(text)
        if segs:
            total = segs[0].total
            received = {s.index for s in segs}
            missing = [i for i in range(1, total + 1) if i not in received]
            out = {
                "name": "（分段容器，需合并后查看）",
                "doc_id": segs[0].doc_id,
                "segments_total": total,
                "segments_received": len(received),
                "segments_missing": missing,
                "payload_chars": sum(len(s.body) for s in segs),
                "codec": "?",
                "sha256": "?",
                "size": 0,
                "text_chars": sum(len(s.body) for s in segs),
                "compress": {},
                "cipher": {},
            }
            for seg in segs:
                if seg.meta_line:
                    try:
                        out.update(_meta_decode(seg.meta_line[len(META_PREFIX):]))
                    except Exception:
                        pass
                    break
            return out
    meta, payload_text = read_container(text)
    out = dict(meta)
    out["payload_chars"] = len(payload_text)
    return out
