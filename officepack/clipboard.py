"""读取 Windows 剪贴板 + 集成 Ditto 剪贴板管理器。

为什么要自己读，而不是直接用 ``PIL.ImageGrab.grabclipboard()``：

* 它只认剪贴板里「最前面那一项」，遇到 Ditto 塞进来的 CF_DIB / CF_DIBV5
  时好时坏，表现出来就是「复制了图却读不到」。
* 它不会告诉你剪贴板里到底是什么（图 / 文件列表 / 文本），没法给用户解释。

这里直接调 Win32 剪贴板 API，把 PNG、DIBV5、DIB、CF_BITMAP、CF_HDROP 都试一遍，
最后才退回 Pillow 兜底。全部函数在非 Windows 上返回空结果，不抛异常。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import io
import os
import struct
import subprocess

IS_WIN = os.name == "nt"

CF_TEXT = 1
CF_BITMAP = 2
CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIBV5 = 17
CF_PNG = 0

_user32 = _kernel32 = _shell32 = None
if IS_WIN:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _shell32 = ctypes.windll.shell32

# HGLOBAL 在 64 位下是 8 字节，不声明 argtypes 会被截断成 32 位
if IS_WIN:
    _user32.OpenClipboard.argtypes = [wt.HWND]
    _user32.OpenClipboard.restype = wt.BOOL
    _user32.GetClipboardData.argtypes = [wt.UINT]
    _user32.GetClipboardData.restype = wt.HANDLE
    _user32.IsClipboardFormatAvailable.argtypes = [wt.UINT]
    _user32.RegisterClipboardFormatW.argtypes = [wt.LPCWSTR]
    _user32.RegisterClipboardFormatW.restype = wt.UINT
    _user32.GetClipboardSequenceNumber.restype = wt.DWORD
    _kernel32.GlobalLock.argtypes = [wt.HANDLE]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalUnlock.argtypes = [wt.HANDLE]
    _kernel32.GlobalSize.argtypes = [wt.HANDLE]
    _kernel32.GlobalSize.restype = ctypes.c_size_t
    _shell32.DragQueryFileW.argtypes = [wt.HANDLE, wt.UINT, wt.LPWSTR, wt.UINT]
    _shell32.DragQueryFileW.restype = wt.UINT
    try:
        CF_PNG = _user32.RegisterClipboardFormatW("PNG")
    except Exception:  # pragma: no cover
        CF_PNG = 0

#: 我们认得的格式（用于给用户看「剪贴板里到底有什么」）
_FMT_NAMES = {
    CF_TEXT: "文本",
    CF_BITMAP: "位图(CF_BITMAP)",
    CF_DIB: "位图(CF_DIB)",
    CF_UNICODETEXT: "文本(Unicode)",
    CF_HDROP: "文件列表",
    CF_DIBV5: "位图(CF_DIBV5)",
}

SEQUENCE = 0          # 最近一次读取到的剪贴板序号，用于判断「有没有新内容」


# --------------------------------------------------------------------------
# 基础封装
# --------------------------------------------------------------------------
def sequence() -> int:
    """剪贴板序号：内容一变这个数就变，用来判断有没有新东西。"""
    if not IS_WIN:
        return 0
    try:
        return int(_user32.GetClipboardSequenceNumber())
    except Exception:  # pragma: no cover
        return 0


def _open_clipboard(retries: int = 10) -> bool:
    """打开剪贴板。别的程序（Ditto 就在其列）会短暂占用，需要重试。"""
    if not IS_WIN:
        return False
    for _ in range(retries):
        try:
            if _user32.OpenClipboard(None):
                return True
        except Exception:  # pragma: no cover
            return False
        import time

        time.sleep(0.03)
    return False


def _close_clipboard() -> None:
    try:
        _user32.CloseClipboard()
    except Exception:  # pragma: no cover
        pass


def _raw(fmt: int) -> bytes | None:
    """取某个格式的原始字节（调用前必须已打开剪贴板）。"""
    try:
        handle = _user32.GetClipboardData(fmt)
    except Exception:  # pragma: no cover
        return None
    if not handle:
        return None
    ptr = _kernel32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        size = int(_kernel32.GlobalSize(handle))
        if size <= 0:
            return None
        return ctypes.string_at(ptr, size)
    finally:
        _kernel32.GlobalUnlock(handle)


def formats() -> list[str]:
    """剪贴板当前可用的格式名（给用户看的诊断信息）。"""
    if not _open_clipboard():
        return []
    try:
        out = []
        fmt = 0
        while True:
            fmt = _user32.EnumClipboardFormats(fmt)
            if not fmt:
                break
            if fmt in _FMT_NAMES:
                out.append(_FMT_NAMES[fmt])
            else:
                buf = ctypes.create_unicode_buffer(128)
                n = _user32.GetClipboardFormatNameW(fmt, buf, 128)
                out.append(buf.value if n else "格式#%d" % fmt)
        return out
    except Exception:  # pragma: no cover
        return []
    finally:
        _close_clipboard()


# --------------------------------------------------------------------------
# 位图：DIB → PIL
# --------------------------------------------------------------------------
def _dib_to_bmp(dib: bytes) -> bytes:
    """给 DIB 套一个 14 字节 BMP 文件头，剩下的交给 Pillow 解。"""
    header_size = struct.unpack_from("<I", dib, 0)[0]
    bit_count = struct.unpack_from("<H", dib, 14)[0]
    compression = struct.unpack_from("<I", dib, 16)[0]
    clr_used = struct.unpack_from("<I", dib, 32)[0] if len(dib) >= 36 else 0

    palette = 0
    if compression == 3:                      # BI_BITFIELDS：三个掩码
        palette = 12
    elif bit_count <= 8:
        palette = (clr_used or (1 << bit_count)) * 4

    offset = 14 + header_size + palette
    file_header = b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, offset)
    return file_header + dib


def _image_from(data: bytes, fmt: int):
    from PIL import Image

    if fmt == CF_PNG:
        return Image.open(io.BytesIO(data))
    if fmt == CF_DIBV5:
        # BITMAPV5HEADER 前 40 字节与 BITMAPINFOHEADER 兼容，压缩方式可能是
        # BI_JPEG(4) / BI_PNG(5)，那时 header 后面直接就是整张图片
        compression = struct.unpack_from("<I", data, 16)[0]
        if compression in (4, 5):
            header_size = struct.unpack_from("<I", data, 0)[0]
            return Image.open(io.BytesIO(data[header_size:]))
        return Image.open(io.BytesIO(_dib_to_bmp(data)))
    if fmt == CF_DIB:
        return Image.open(io.BytesIO(_dib_to_bmp(data)))
    raise ValueError("unsupported clipboard image format %r" % fmt)


def read_images() -> list:
    """剪贴板里的图片（PIL Image 列表）。读不到就返回空列表。"""
    images = []
    if _open_clipboard():
        try:
            for fmt in (CF_PNG, CF_DIBV5, CF_DIB):
                if not fmt:
                    continue
                data = _raw(fmt)
                if not data:
                    continue
                try:
                    img = _image_from(data, fmt)
                    img.load()                    # 立刻解码，剪贴板马上就关了
                    images.append(img.convert("RGB"))
                except Exception:  # noqa: BLE001  格式不认识的 DIB 就跳过
                    continue
                if images:                        # 同一张图别收三遍
                    break
        finally:
            _close_clipboard()

    if images:
        return images

    # 兜底：CF_BITMAP 这类 GDI 句柄只有 Pillow 知道怎么转
    try:
        from PIL import ImageGrab

        clip = ImageGrab.grabclipboard()
    except Exception:  # pragma: no cover
        return []
    if clip is not None and hasattr(clip, "mode"):
        return [clip.convert("RGB")]
    return []


def read_files() -> list[str]:
    """剪贴板里「复制的文件」列表（资源管理器复制文件、截图工具存文件后复制）。"""
    if not _open_clipboard():
        return []
    try:
        if not _user32.IsClipboardFormatAvailable(CF_HDROP):
            return []
        handle = _user32.GetClipboardData(CF_HDROP)
        if not handle:
            return []
        count = _shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        paths = []
        for i in range(int(count)):
            buf = ctypes.create_unicode_buffer(1024)
            if _shell32.DragQueryFileW(handle, i, buf, 1024):
                paths.append(buf.value)
        return paths
    finally:
        _close_clipboard()


def read_text() -> str | None:
    """剪贴板里的文本（优先 Unicode）。"""
    if not _open_clipboard():
        return None
    try:
        if _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            data = _raw(CF_UNICODETEXT)
            if data:
                return data.decode("utf-16-le", "ignore").split("\x00")[0]
        return None
    finally:
        _close_clipboard()


def snapshot() -> dict:
    """一次拿全：图 / 文件 / 文本 / 格式名。面板用它决定这次粘贴该干什么。"""
    return {
        "images": read_images(),
        "files": read_files(),
        "text": read_text(),
        "formats": formats(),
        "sequence": sequence(),
    }


# --------------------------------------------------------------------------
# Ditto
# --------------------------------------------------------------------------
def _process_image_paths() -> list[str]:
    """枚举正在运行的进程，取出它们的 exe 路径。"""
    if not IS_WIN:
        return []
    try:
        arr = (wt.DWORD * 2048)()
        needed = wt.DWORD()
        if not _kernel32.K32EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
            return []
        total = needed.value // ctypes.sizeof(wt.DWORD)
        paths = []
        for pid in arr[:total]:
            if not pid:
                continue
            handle = _kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
            if not handle:
                continue
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wt.DWORD(1024)
                if _kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                          ctypes.byref(size)):
                    paths.append(buf.value)
            finally:
                _kernel32.CloseHandle(handle)
        return paths
    except Exception:  # pragma: no cover
        return []


def _registry_paths() -> list[str]:
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return []
    found = []
    roots = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    keys = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
    for root in roots:
        for key in keys:
            try:
                with winreg.OpenKey(root, key) as uninstall:
                    for i in range(winreg.QueryInfoKey(uninstall)[0]):
                        try:
                            name = winreg.EnumKey(uninstall, i)
                            if "ditto" not in name.lower():
                                continue
                            with winreg.OpenKey(uninstall, name) as sub:
                                for value in ("InstallLocation", "DisplayIcon",
                                              "UninstallString"):
                                    try:
                                        val = winreg.QueryValueEx(sub, value)[0]
                                    except OSError:
                                        continue
                                    val = str(val).strip('"')
                                    cand = val if val.lower().endswith(".exe") else \
                                        os.path.join(val, "Ditto.exe")
                                    if os.path.isfile(cand):
                                        found.append(cand)
                        except OSError:
                            continue
            except OSError:
                continue
    return found


def _logical_drives() -> list[str]:
    """当前真实存在的本地盘符（不猜 C~H，绿色版装在任意盘都能找到）。

    网络盘/光驱一律跳过：``os.path.isfile`` 打到断线的网络路径上会卡好几秒。
    """
    if not IS_WIN:
        return []
    try:
        mask = _kernel32.GetLogicalDrives()
    except Exception:  # pragma: no cover
        return []
    out = []
    for i in range(26):
        if not mask & (1 << i):
            continue
        letter = chr(ord("A") + i)
        try:
            kind = _kernel32.GetDriveTypeW("%s:\\" % letter)
        except Exception:  # pragma: no cover
            kind = 3
        if kind in (2, 3):          # DRIVE_REMOVABLE / DRIVE_FIXED
            out.append(letter)
    return out


def _candidate_dirs() -> list[str]:
    """Ditto 可能出现的位置：环境变量指向的目录 + 每个真实盘符的常见目录。"""
    dirs: list[str] = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
                "LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = os.environ.get(env)
        if not base:
            continue
        dirs += [os.path.join(base, "Ditto"),
                 os.path.join(base, "Programs", "Ditto"),
                 os.path.join(base, "Programs", "Ditto Portable"),
                 os.path.join(base, "Ditto Portable")]
    subs = (r"Program Files\Ditto", r"Program Files (x86)\Ditto",
            r"Programs\Ditto", "Ditto", "Ditto Portable", r"Ditto\Ditto Portable")
    for drive in _logical_drives():
        for sub in subs:
            dirs.append("%s:\\%s" % (drive, sub))
    return dirs


def ditto_path() -> str | None:
    """自动探测 Ditto.exe —— 不写死任何路径。

    顺序：正在运行的进程（装在哪儿都能从进程里问出来）→ 卸载注册表 →
    PATH → 环境变量与各真实盘符的常见目录。都没有就返回 None，走截图兜底。
    """
    for path in _process_image_paths():
        if os.path.basename(path).lower() == "ditto.exe":
            return path
    for path in _registry_paths():
        if os.path.isfile(path):
            return path
    try:
        import shutil

        found = shutil.which("Ditto.exe") or shutil.which("Ditto")
        if found and os.path.isfile(found):
            return found
    except Exception:  # pragma: no cover
        pass
    for folder in _candidate_dirs():
        cand = os.path.join(folder, "Ditto.exe")
        if os.path.isfile(cand):
            return cand
    return None


def open_ditto(path: str | None = None) -> tuple[bool, str]:
    """把 Ditto 的历史窗口叫出来。

    Ditto 已在后台跑的时候，``/Open`` 会让已有实例弹窗，不会再起一个。
    返回 ``(成功与否, 说明)``。
    """
    exe = path or ditto_path()
    if not exe:
        return False, ("没找到 Ditto。用 Win+Shift+S 截一张图，"
                       "回到面板直接 Ctrl+V 也一样。")
    try:
        subprocess.Popen([exe, "/Open"], close_fds=True)
        return True, ("已打开 Ditto 历史：在 Ditto 里双击要用的图（可按住 Shift/Ctrl 多选），"
                      "它会自动贴进本面板。")
    except Exception as exc:  # noqa: BLE001
        return False, "打开 Ditto 失败：%s" % exc
