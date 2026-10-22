"""officepack — 任意文件 ↔ 纯文本无损装换工具（Office / PDF / 压缩包 / 源码 / 图片都行）。

典型用法::

    from officepack import pack_file, unpack_file

    out, meta = pack_file("报告.docx", codec_name="base91", compress="auto",
                          cipher_name="aes-256-gcm", password="secret")
    restored, meta2 = unpack_file(out, password="secret")

还原后文件与原始文件 SHA-256 完全一致。
"""

from .container import (
    BEGIN,
    DEFAULT_CODEC,
    DEFAULT_COMPRESS,
    DEFAULT_SEGMENT_CHARS,
    DEFAULT_TARGET_CHARS,
    END,
    PAYLOAD_HEADER,
    PRESETS,
    SEG_PREFIX,
    ContainerError,
    PackResult,
    Segment,
    UnpackResult,
    default_unpack_dir,
    inspect_text,
    join_segments,
    pack_bytes,
    pack_file,
    parse_segments,
    read_container,
    render,
    split_pack,
    unpack_file,
    unpack_pasted,
    unpack_sources,
    unpack_text,
    unpack_texts,
)
from .codecs import CODECS, CodecError, get_codec
from .compress import CompressError, decompress
from .compress import compress as compress_bytes
from .crypto import CryptoError, make_cipher
from .preview import extract_preview, kind_of
from .qr import (
    ALNUM_CODECS,
    DEFAULT_EC,
    QR_ALNUM_CAPACITY,
    QR_CODEC,
    QRError,
    build_frames,
    capacity_report,
    decode_image,
    decode_images,
    inspect_images,
    is_image_path,
    overhead_ratio,
    render_gif,
    render_images,
    single_frame_bytes,
    unpack_images,
)

__version__ = "1.3.0"

__all__ = [
    "BEGIN",
    "END",
    "PAYLOAD_HEADER",
    "SEG_PREFIX",
    "DEFAULT_SEGMENT_CHARS",
    "DEFAULT_TARGET_CHARS",
    "Segment",
    "split_pack",
    "parse_segments",
    "join_segments",
    "unpack_sources",
    "unpack_pasted",
    "unpack_texts",
    "default_unpack_dir",
    "PRESETS",
    "DEFAULT_CODEC",
    "DEFAULT_COMPRESS",
    "CODECS",
    "PackResult",
    "UnpackResult",
    "ContainerError",
    "CodecError",
    "CompressError",
    "CryptoError",
    "pack_bytes",
    "pack_file",
    "unpack_text",
    "unpack_file",
    "read_container",
    "inspect_text",
    "render",
    "get_codec",
    "compress_bytes",
    "decompress",
    "make_cipher",
    "extract_preview",
    "kind_of",
    # 二维码通道
    "QR_CODEC",
    "QR_ALNUM_CAPACITY",
    "DEFAULT_EC",
    "ALNUM_CODECS",
    "overhead_ratio",
    "single_frame_bytes",
    "capacity_report",
    "QRError",
    "build_frames",
    "render_images",
    "render_gif",
    "decode_images",
    "decode_image",
    "unpack_images",
    "inspect_images",
    "is_image_path",
    "__version__",
]
