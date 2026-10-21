"""可插拔加解密器。

- none          : 不加密，仅做编码载体
- aes-256-gcm   : 口令派生密钥 + 分块认证加密（防篡改）

分块加密保证内存占用与文件大小无关；每块独立 nonce（8B 随机前缀 + 4B 计数器），
AAD 绑定原始文件大小与 SHA-256，因此元信息被改动会导致解密直接失败。
"""

from __future__ import annotations

import base64
import hashlib
import os

CHUNK_SIZE = 1 << 20  # 1 MiB
TAG_LEN = 16


class CryptoError(ValueError):
    pass


try:  # pragma: no cover - 依赖环境
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _HAS_AESGCM = True
except ImportError:  # pragma: no cover
    AESGCM = None  # type: ignore
    _HAS_AESGCM = False


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _derive_key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=32,
        maxmem=128 * 1024 * 1024,
    )


class Cipher:
    name = "none"

    def encrypt(self, data: bytes, aad: bytes = b"") -> tuple[bytes, dict]:
        raise NotImplementedError  # pragma: no cover

    def decrypt(self, data: bytes, meta: dict, aad: bytes = b"") -> bytes:
        raise NotImplementedError  # pragma: no cover


class NoneCipher(Cipher):
    name = "none"

    def encrypt(self, data: bytes, aad: bytes = b"") -> tuple[bytes, dict]:
        return data, {"alg": "none"}

    def decrypt(self, data: bytes, meta: dict, aad: bytes = b"") -> bytes:
        return data


class AesGcmCipher(Cipher):
    name = "aes-256-gcm"

    def __init__(self, password: str, *, n: int = 16384, r: int = 8, p: int = 1,
                 salt: bytes | None = None, base_nonce: bytes | None = None,
                 chunk_size: int = CHUNK_SIZE, plain_size: int | None = None):
        if not password:
            raise CryptoError("AES-256-GCM 需要口令")
        if not _HAS_AESGCM:
            raise CryptoError(
                "缺少 cryptography 库，无法使用 AES-256-GCM。"
                "请执行：pip install cryptography"
            )
        self.password = password
        self.n, self.r, self.p = n, r, p
        self.salt = salt or os.urandom(16)
        self.base_nonce = base_nonce or os.urandom(8)
        self.chunk_size = chunk_size
        self.plain_size = plain_size
        self._key = _derive_key(password, self.salt, n, r, p)
        self._aead = AESGCM(self._key)

    def _nonce(self, index: int) -> bytes:
        if index > 0xFFFFFFFF:
            raise CryptoError("文件过大，分块计数溢出")
        return self.base_nonce + index.to_bytes(4, "big")

    def encrypt(self, data: bytes, aad: bytes = b"") -> tuple[bytes, dict]:
        out = bytearray()
        index = 0
        for off in range(0, len(data), self.chunk_size):
            block = data[off : off + self.chunk_size]
            out += self._aead.encrypt(self._nonce(index), block, aad)
            index += 1
        meta = {
            "alg": self.name,
            "kdf": "scrypt",
            "n": self.n,
            "r": self.r,
            "p": self.p,
            "salt": _b64e(self.salt),
            "nonce": _b64e(self.base_nonce),
            "chunk": self.chunk_size,
        }
        return bytes(out), meta

    def decrypt(self, data: bytes, meta: dict, aad: bytes = b"") -> bytes:
        total = meta.get("plain_size") or self.plain_size
        if total is None:
            raise CryptoError("缺少 plain_size，无法定位密文块")
        chunk = int(meta.get("chunk", self.chunk_size))
        out = bytearray()
        pos = 0
        index = 0
        remaining = total
        while remaining > 0:
            take = min(chunk, remaining) + TAG_LEN
            piece = data[pos : pos + take]
            if len(piece) != take:
                raise CryptoError("密文被截断：第 %d 块不足 %d 字节" % (index, take))
            try:
                out += self._aead.decrypt(self._nonce(index), piece, aad)
            except Exception as exc:
                raise CryptoError(
                    "解密失败：口令错误或文件已被篡改（%s）"
                    % type(exc).__name__
                ) from None
            pos += take
            remaining -= min(chunk, remaining)
            index += 1
        if pos != len(data):
            raise CryptoError("密文尾部存在多余数据")
        return bytes(out)


CIPHERS = {"none": NoneCipher, "aes-256-gcm": AesGcmCipher}


def make_cipher(alg: str, password: str | None, plain_size: int) -> Cipher:
    key = (alg or "none").lower()
    if key in ("none", "", "no", "off"):
        return NoneCipher()
    if key not in CIPHERS:
        raise CryptoError("未知加密器 %r，可用：none, aes-256-gcm" % alg)
    if not password:
        raise CryptoError("所选加密器需要提供口令")
    return AesGcmCipher(password, plain_size=plain_size, chunk_size=CHUNK_SIZE)


def cipher_from_meta(meta: dict, password: str | None, plain_size: int) -> Cipher:
    alg = (meta.get("cipher") or {}).get("alg", "none")
    if alg == "none":
        return NoneCipher()
    if not password:
        raise CryptoError("该文件已加密，请提供口令")
    info = meta["cipher"]
    return AesGcmCipher(
        password,
        n=int(info.get("n", 16384)),
        r=int(info.get("r", 8)),
        p=int(info.get("p", 1)),
        salt=_b64d(info["salt"]),
        base_nonce=_b64d(info["nonce"]),
        chunk_size=int(info.get("chunk", CHUNK_SIZE)),
        plain_size=plain_size,
    )
