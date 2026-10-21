"""officepack — Office 文档二进制 ↔ 纯文本无损装换工具。

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
from .preview import extract_preview

__version__ = "1.0.1"

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
    "__version__",
]
