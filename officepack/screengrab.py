"""屏幕抓取：GDI 区域 BitBlt 与 DXGI 桌面复制，机器自适应。

为什么要有两条路：

* **GDI BitBlt**（``gdi``）：走 CPU，把屏幕像素从显存搬到内存。这台机器实测
  20~40ms/枪；没有独立显卡、或者跑在远程桌面上，它就是最稳的一条。
* **DXGI Desktop Duplication**（``dxgi``）：微软给录屏/串流用的接口，帧本来
  就在显存里，``CopySubresourceRegion`` 直接切我们框选的那一块到暂存纹理，
  再 ``Map`` 读回来 —— 不经过整屏拷贝，有独显的机器上通常 2~5ms/枪。

两条路的坐标系都是**物理像素**，和 ``ImageGrab`` 一致。

用法（自适应，别写死）：::

    g = pick_grabber(bbox)        # 只会返回可用的那个
    img = g.grab()

``pick_grabber`` 会各自试抓几帧，挑最快的那个，并把结论写进 ``g.info``。
"""

from __future__ import annotations

import ctypes
import time

__all__ = ["gdi_grabber", "dxgi_grabber", "pick_grabber", "Grabber"]

SRCCOPY = 0x00CC0020


# --------------------------------------------------------------------------- #
# GDI：只搬框内那一块
# --------------------------------------------------------------------------- #
def gdi_grabber(bbox):
    """做「只抓框内区域」的 GDI BitBlt 抓图函数；失败返回 None。

    为什么不直接用 ``ImageGrab``：它在 Windows 上**每次都把整个虚拟桌面抓下来
    再裁**，双屏 3840×1200 就是 18MB 的拷贝。实测抓同一块 971×948：

    * ``ImageGrab.grab(bbox=…, all_screens=True)``：**83ms/枪**
    * 这里只 BitBlt 框内那块：**20ms/枪**

    动图每帧停留 100ms，83ms 一枪时采样点会漂，实测 142 枪只收到 90 帧
    （漏 37%）。
    """
    try:
        from ctypes import wintypes

        from PIL import Image

        x1, y1, x2, y2 = [int(v) for v in bbox]
        width, height = x2 - x1, y2 - y1
        if width <= 0 or height <= 0:
            return None

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        hdc_screen = user32.GetDC(0)
        if not hdc_screen:
            return None
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
        gdi32.SelectObject(hdc_mem, hbmp)

        class _BitmapInfoHeader(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long),
                        ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        info = _BitmapInfoHeader()
        info.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.biWidth, info.biHeight = width, -height      # 负高度 = 自上而下
        info.biPlanes, info.biBitCount = 1, 32
        info.biCompression = 0
        buf = ctypes.create_string_buffer(width * height * 4)

        def grab():
            if not gdi32.BitBlt(hdc_mem, 0, 0, width, height,
                                hdc_screen, x1, y1, SRCCOPY):
                raise OSError("BitBlt 失败")
            if not gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf,
                                   ctypes.byref(info), 0):
                raise OSError("GetDIBits 失败")
            # copy() 必须留：buf 会被下一枪覆写，不拷出来所有帧会变成同一张
            return Image.frombuffer("RGB", (width, height), buf,
                                    "raw", "BGRX", 0, 1).copy()

        return grab
    except Exception:  # noqa: BLE001  非 Windows / API 不可用
        return None


# --------------------------------------------------------------------------- #
# DXGI 桌面复制
# --------------------------------------------------------------------------- #
#: ``D3D11_TEXTURE2D_DESC``（11 个 UINT，44 字节）
class _Texture2DDesc(ctypes.Structure):
    _fields_ = [("Width", ctypes.c_uint), ("Height", ctypes.c_uint),
                ("MipLevels", ctypes.c_uint), ("ArraySize", ctypes.c_uint),
                ("Format", ctypes.c_uint),
                ("SampleCount", ctypes.c_uint), ("SampleQuality", ctypes.c_uint),
                ("Usage", ctypes.c_uint), ("BindFlags", ctypes.c_uint),
                ("CPUAccessFlags", ctypes.c_uint), ("MiscFlags", ctypes.c_uint)]


class _MappedSubresource(ctypes.Structure):
    _fields_ = [("pData", ctypes.c_void_p), ("RowPitch", ctypes.c_uint),
                ("DepthPitch", ctypes.c_uint)]


class _Box(ctypes.Structure):
    _fields_ = [("left", ctypes.c_uint), ("top", ctypes.c_uint), ("front", ctypes.c_uint),
                ("right", ctypes.c_uint), ("bottom", ctypes.c_uint), ("back", ctypes.c_uint)]


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _OutputDesc(ctypes.Structure):
    _fields_ = [("DeviceName", ctypes.c_wchar * 32),
                ("DesktopCoordinates", _Rect),
                ("AttachedToDesktop", ctypes.c_int),
                ("Rotation", ctypes.c_int),
                ("Monitor", ctypes.c_void_p)]


class _Guid(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s):
    import uuid

    u = uuid.UUID(s)
    g = _Guid()
    g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


def _vcall(ptr, index, restype, *argtypes):
    """按 vtable 序号取 COM 方法并包成可调用对象。"""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])


def _release(ptr):
    if ptr:
        try:
            _vcall(ptr, 2, ctypes.c_ulong)(ptr)
        except Exception:  # noqa: BLE001
            pass


class _AdapterDesc(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128),
                ("VendorId", ctypes.c_uint), ("DeviceId", ctypes.c_uint),
                ("SubSysId", ctypes.c_uint), ("Revision", ctypes.c_uint),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", ctypes.c_uint64)]


def adapter_name(adapter) -> str:
    """读适配器名；读不到返回空串。"""
    try:
        d = _AdapterDesc()
        hr = _vcall(adapter, 8, ctypes.c_long, ctypes.POINTER(_AdapterDesc))(
            adapter, ctypes.byref(d))
        if hr < 0:
            return ""
        return (d.Description or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _is_software_adapter(adapter) -> bool:
    """软件渲染适配器（虚拟机 / 没装显卡驱动）判定。

    这类适配器上调 ``DuplicateOutput`` 不是返回错误码，而是**直接崩溃**，
    所以只能提前认出来、绕开。
    """
    name = adapter_name(adapter)
    low = name.lower()
    return ("basic render" in low or "basic display" in low
            or "microsoft basic" in low or not name)


def gpu_name() -> str:
    """当前主适配器名字，给日志和探测缓存用。"""
    try:
        d3d11 = ctypes.WinDLL("d3d11")
        device = ctypes.c_void_p()
        context = ctypes.c_void_p()
        feature = ctypes.c_int()
        hr = d3d11.D3D11CreateDevice(
            None, 1, None, 0x20, None, 0, 7,
            ctypes.byref(device), ctypes.byref(feature), ctypes.byref(context))
        if hr < 0 or not device:
            return ""
        try:
            iid = _guid("54ec77fa-1377-44e6-8c32-88fd5f44c84c")
            dxgi_device = ctypes.c_void_p()
            hr = _vcall(device, 0, ctypes.c_long, ctypes.POINTER(_Guid),
                        ctypes.POINTER(ctypes.c_void_p))(
                device, ctypes.byref(iid), ctypes.byref(dxgi_device))
            if hr < 0 or not dxgi_device:
                return ""
            try:
                adapter = ctypes.c_void_p()
                hr = _vcall(dxgi_device, 7, ctypes.c_long,
                            ctypes.POINTER(ctypes.c_void_p))(
                    dxgi_device, ctypes.byref(adapter))
                if hr < 0 or not adapter:
                    return ""
                try:
                    return adapter_name(adapter)
                finally:
                    _release(adapter)
            finally:
                _release(dxgi_device)
        finally:
            _release(context)
            _release(device)
    except Exception:  # noqa: BLE001
        return ""


def dxgi_grabber(bbox):
    """用 DXGI 桌面复制抓框内区域；不可用返回 ``None``。

    帧本来就在显存里：``CopySubresourceRegion`` 只裁框内那块到一张 STAGING
    纹理，再 ``Map`` 读回内存。全程不碰整屏，所以有独显的机器上比 GDI 快一个
    数量级。

    失败原因很常见（远程桌面、没有 DXGI 输出、另一个进程已经独占），一律返回
    ``None`` 让调用方回退到 GDI，绝不抛异常。
    """
    try:
        from PIL import Image

        x1, y1, x2, y2 = [int(v) for v in bbox]
        width, height = x2 - x1, y2 - y1
        if width <= 0 or height <= 0:
            return None

        d3d11 = ctypes.WinDLL("d3d11")
        dxgi = ctypes.WinDLL("dxgi")

        D3D_DRIVER_TYPE_HARDWARE = 1
        D3D11_CREATE_DEVICE_BGRA = 0x20
        D3D11_SDK_VERSION = 7
        DXGI_FORMAT_B8G8R8A8_UNORM = 87
        D3D11_USAGE_STAGING = 3
        D3D11_CPU_ACCESS_READ = 0x20000
        D3D11_MAP_READ = 1
        WAIT_TIMEOUT = 0x887A0027
        RPC_E_CHANGED_MODE = -2147417850

        device = ctypes.c_void_p()
        context = ctypes.c_void_p()
        feature = ctypes.c_int()
        hr = d3d11.D3D11CreateDevice(
            None, D3D_DRIVER_TYPE_HARDWARE, None, D3D11_CREATE_DEVICE_BGRA,
            None, 0, D3D11_SDK_VERSION,
            ctypes.byref(device), ctypes.byref(feature), ctypes.byref(context))
        if hr < 0:
            return None

        # device → IDXGIDevice → 适配器 → 输出 → IDXGIOutput1
        iid_dxgi_device = _guid("54ec77fa-1377-44e6-8c32-88fd5f44c84c")
        iid_output1 = _guid("00cddea8-939b-4b83-a340-a685226666cc")
        dxgi_device = ctypes.c_void_p()
        hr = _vcall(device, 0, ctypes.c_long, ctypes.POINTER(_Guid),
                    ctypes.POINTER(ctypes.c_void_p))(
            device, ctypes.byref(iid_dxgi_device), ctypes.byref(dxgi_device))
        if hr < 0 or not dxgi_device:
            return None

        adapter = ctypes.c_void_p()
        hr = _vcall(dxgi_device, 7, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
            dxgi_device, ctypes.byref(adapter))
        if hr < 0 or not adapter:
            _release(dxgi_device)
            return None

        output = ctypes.c_void_p()
        hr = _vcall(adapter, 7, ctypes.c_long, ctypes.c_uint,
                    ctypes.POINTER(ctypes.c_void_p))(adapter, 0, ctypes.byref(output))
        if hr < 0 or not output:
            _release(adapter)
            _release(dxgi_device)
            return None

        desc = _OutputDesc()
        _vcall(output, 7, ctypes.c_long, ctypes.POINTER(_OutputDesc))(
            output, ctypes.byref(desc))
        r = desc.DesktopCoordinates
        # 框选必须完全落在主输出里，否则（副屏/跨屏）交给 GDI
        if not (r.left <= x1 and r.top <= y1 and x2 <= r.right and y2 <= r.bottom):
            _release(output)
            _release(adapter)
            _release(dxgi_device)
            return None

        # 先看适配器是谁：Microsoft Basic Render Driver（虚拟机、无独显、驱动没装）
        # 上没有真正的桌面复制，再往下调 DuplicateOutput 会直接**访问违例崩进程**，
        # 这在 try/except 里拦不住。所以这里必须先拦住。
        if _is_software_adapter(adapter):
            _release(output)
            _release(adapter)
            _release(dxgi_device)
            return None

        output1 = ctypes.c_void_p()
        hr = _vcall(output, 0, ctypes.c_long, ctypes.POINTER(_Guid),
                    ctypes.POINTER(ctypes.c_void_p))(
            output, ctypes.byref(iid_output1), ctypes.byref(output1))
        if hr < 0 or not output1:
            _release(output)
            _release(adapter)
            _release(dxgi_device)
            return None

        dup = ctypes.c_void_p()
        hr = _vcall(output1, 19, ctypes.c_long, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p))(output1, device, ctypes.byref(dup))
        if hr < 0 or not dup:
            _release(output1)
            _release(output)
            _release(adapter)
            _release(dxgi_device)
            return None

        # 一张框内大小的 STAGING 纹理，反复复用
        sdesc = _Texture2DDesc()
        sdesc.Width, sdesc.Height = width, height
        sdesc.MipLevels = sdesc.ArraySize = 1
        sdesc.Format = DXGI_FORMAT_B8G8R8A8_UNORM
        sdesc.SampleCount, sdesc.SampleQuality = 1, 0
        sdesc.Usage = D3D11_USAGE_STAGING
        sdesc.BindFlags = 0
        sdesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ
        sdesc.MiscFlags = 0
        staging = ctypes.c_void_p()
        hr = _vcall(device, 5, ctypes.c_long, ctypes.POINTER(_Texture2DDesc),
                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            device, ctypes.byref(sdesc), None, ctypes.byref(staging))
        if hr < 0 or not staging:
            _release(dup)
            _release(output1)
            _release(output)
            _release(adapter)
            _release(dxgi_device)
            return None

        box = _Box(x1 - r.left, y1 - r.top, 0, x2 - r.left, y2 - r.top, 1)
        iid_tex2d = _guid("6f15aaf2-d208-4e89-9ab4-489535d34f9c")
        state = {"last": None, "dead": False}

        def _blank():
            return Image.new("RGB", (width, height), (0, 0, 0))

        def grab():
            if state["dead"]:
                raise OSError("DXGI 已失效")
            raw = ctypes.c_void_p()
            acquired = False
            try:
                hr = _vcall(dup, 4, ctypes.c_long, ctypes.c_uint, ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_void_p))(
                    dup, 0, None, ctypes.byref(raw))
                if hr == WAIT_TIMEOUT:
                    # 这一拍屏幕上没有新帧（画面没更新）：给上次的结果
                    return state["last"] if state["last"] is not None else _blank()
                if hr < 0 or not raw:
                    raise OSError("AcquireNextFrame 失败 0x%08X" % (hr & 0xFFFFFFFF))
                acquired = True
                tex = ctypes.c_void_p()
                hr = _vcall(raw, 0, ctypes.c_long, ctypes.POINTER(_Guid),
                            ctypes.POINTER(ctypes.c_void_p))(
                    raw, ctypes.byref(iid_tex2d), ctypes.byref(tex))
                if hr < 0 or not tex:
                    raise OSError("QI ID3D11Texture2D 失败")
                try:
                    hr = _vcall(context, 46, None, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(_Box))(
                        context, staging, 0, 0, 0, 0, tex, 0, ctypes.byref(box))
                    mapped = _MappedSubresource()
                    hr = _vcall(context, 14, ctypes.c_long, ctypes.c_void_p,
                                ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                                ctypes.POINTER(_MappedSubresource))(
                        context, staging, 0, D3D11_MAP_READ, 0, ctypes.byref(mapped))
                    if hr < 0 or not mapped.pData:
                        raise OSError("Map 失败")
                    try:
                        row = width * 4
                        stride = int(mapped.RowPitch)
                        if stride == row:
                            data = ctypes.string_at(mapped.pData, row * height)
                        else:
                            # 行尾有 padding：整块读出来再按行剔掉
                            whole = ctypes.string_at(mapped.pData, stride * height)
                            data = b"".join(whole[i * stride:i * stride + row]
                                            for i in range(height))
                    finally:
                        _vcall(context, 15, None, ctypes.c_void_p, ctypes.c_uint)(
                            context, staging, 0)
                    img = Image.frombuffer("RGB", (width, height), data,
                                           "raw", "BGRX", 0, 1).copy()
                    state["last"] = img
                    return img
                finally:
                    _release(tex)
            except Exception:
                state["dead"] = True
                raise
            finally:
                if acquired:
                    _vcall(dup, 7, ctypes.c_long)(dup)

        return grab
    except Exception:  # noqa: BLE001  任何一步不满足都退回 GDI
        return None


# --------------------------------------------------------------------------- #
# 自适应选择
# --------------------------------------------------------------------------- #
class Grabber:
    """抓图函数的包装：带上「用的哪条路、多快」的信息。"""

    __slots__ = ("grab", "method", "ms")

    def __init__(self, grab, method: str, ms: float):
        self.grab, self.method, self.ms = grab, method, ms

    def __call__(self):
        return self.grab()

    @property
    def info(self) -> str:
        return "%s（%.0fms/枪）" % (self.method, self.ms)

    def __repr__(self):
        return "<Grabber %s>" % self.info


def _timeit(grab, rounds=6):
    """试抓几帧取中位数；失败返回 None。"""
    samples = []
    try:
        for i in range(rounds):
            t = time.perf_counter()
            grab()
            dt = (time.perf_counter() - t) * 1000
            if i:                      # 第一枪含建纹理/建 DIB，不算
                samples.append(dt)
    except Exception:  # noqa: BLE001
        return None
    if not samples:
        return None
    samples.sort()
    return samples[len(samples) // 2]


# --------------------------------------------------------------------------- #
# 流水线：抓屏放后台，主循环只管解码
# --------------------------------------------------------------------------- #
class Prefetcher:
    """把抓屏挪到后台线程，只保留最新一帧。

    为什么值：主循环里「抓屏 33ms + 指纹 7ms + 收结果」是串行的，而解码本来
    已经在线程池里并行。抓屏一进后台，循环就只等解码，枪数上限从
    ``1/(抓屏+解码)`` 变成 ``1/解码``。队列深度压到 2，抓快了会自动等消费，
    不会把帧堆在内存里。
    """

    __slots__ = ("_grab", "_q", "_stop", "_thread", "_err")

    def __init__(self, grab, depth: int = 2):
        import queue
        import threading

        self._grab = grab
        self._q: queue.Queue = queue.Queue(maxsize=max(1, depth))
        self._stop = threading.Event()
        self._err = False

        def run():
            while not self._stop.is_set():
                try:
                    img = self._grab()
                except Exception:  # noqa: BLE001  抓图失败不该终止整轮扫描
                    img = None
                if img is None:
                    # 抓不到（锁屏、切桌面、BitBlt 被拦）也要立刻叫醒消费者，
                    # 否则主循环得白等 1 秒；再压 50ms 防死循环空转。
                    try:
                        self._q.put(None, timeout=0.2)
                    except Exception:  # noqa: BLE001  队列满了说明消费端更慢
                        pass
                    self._stop.wait(0.05)
                    continue
                try:
                    self._q.put(img, timeout=0.5)
                except Exception:  # noqa: BLE001  队列满且消费停了
                    if self._stop.is_set():
                        break

        self._thread = threading.Thread(target=run, name="grab-prefetch", daemon=True)
        self._thread.start()

    def __call__(self):
        try:
            return self._q.get(timeout=1.0)
        except Exception:  # noqa: BLE001  1 秒还没抓到（锁屏/切桌面）
            return None

    def close(self):
        self._stop.set()
        try:
            self._q.put(None, timeout=0.2)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._thread.join(timeout=1.5)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# DXGI 能不能用：子进程自检 + 缓存
# --------------------------------------------------------------------------- #
def _cache_path():
    import os

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return os.path.join(base, "OfficePack", "grab_probe.json")


def _read_cache() -> dict:
    import json
    import os

    try:
        with open(_cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_cache(data: dict) -> None:
    import json
    import os

    try:
        p = _cache_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:  # noqa: BLE001
        pass


def probe_argv(bbox, out_file: str) -> list[str] | None:
    """拼出「在子进程里跑一次 DXGI 自检」的命令行；拼不出返回 None。"""
    import os
    import sys

    box = ",".join(str(int(v)) for v in bbox)
    if getattr(sys, "frozen", False):
        return [sys.executable, "--probe-grab", box, "--probe-out", out_file]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not root:
        return None
    return [sys.executable, "-m", "officepack.screengrab",
            "--probe-grab", box, "--probe-out", out_file]


def probe_in_child(bbox, timeout: float = 8.0) -> dict | None:
    """在**子进程**里试一次 DXGI。

    必须隔进程：驱动不支持时 ``DuplicateOutput`` 会访问违例，那是进程级崩溃，
    Python 的 try/except 拦不住，只能让子进程去送死、父进程看退出码。

    结果走**临时文件**而不是 stdout —— 打包成窗口程序（无控制台）后，
    子进程根本没有 stdout，``print`` 出去的东西父进程一个字也读不到。
    """
    import json
    import os
    import subprocess
    import tempfile

    fd, out_file = tempfile.mkstemp(prefix="opk-grab-", suffix=".json")
    os.close(fd)
    cmd = probe_argv(bbox, out_file)
    if not cmd:
        return None
    try:
        subprocess.run(cmd, timeout=timeout, creationflags=0x08000000,  # 无控制台窗口
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(out_file, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        return json.loads(txt) if txt else None
    except Exception:  # noqa: BLE001  超时 / 子进程崩了 / 没写出文件
        return None
    finally:
        try:
            os.remove(out_file)
        except Exception:  # noqa: BLE001
            pass


def gpu_usable(bbox, refresh: bool = False) -> tuple[bool, str]:
    """这台机器能不能走 DXGI；结论按显卡名缓存（换显卡/重装驱动自动重判）。"""
    name = gpu_name()
    cache = _read_cache()
    if not refresh and cache.get("gpu") == name and isinstance(cache.get("dxgi"), bool):
        return bool(cache["dxgi"]), str(cache.get("ms") or "")
    res = probe_in_child(bbox)
    ok = bool(res and res.get("ok"))
    ms = "%.1f" % res["ms"] if (ok and res.get("ms")) else ""
    _write_cache({"gpu": name, "dxgi": ok, "ms": ms, "ts": int(time.time())})
    return ok, ms


def pick_grabber(bbox, allow_gpu: bool = True) -> Grabber | None:
    """选一个抓屏方式：能上 GPU 就用 GPU，否则 GDI；并标出实测速度。

    ``allow_gpu=False`` 可以强制走 GDI（排查问题用）。即使这里返回了 DXGI，
    中途它失效（独占全屏被切走、换显示器）也会抛错，由调用方回退 GDI。
    """
    gpu_ok, _ = (False, "")
    if allow_gpu:
        try:
            gpu_ok, _ = gpu_usable(bbox)
        except Exception:  # noqa: BLE001
            gpu_ok = False
    if gpu_ok:
        fn = dxgi_grabber(bbox)
        if fn is not None:
            ms = _timeit(fn)
            if ms is not None:
                return Grabber(fn, "DXGI 桌面复制（GPU）", ms)
    fn = gdi_grabber(bbox)
    if fn is not None:
        ms = _timeit(fn)
        if ms is not None:
            return Grabber(fn, "GDI BitBlt", ms)
    return None


def _main(argv=None):
    """子进程入口：``--probe-grab x1,y1,x2,y2 --probe-out <json 文件>``。"""
    import json
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if "--probe-grab" not in argv:
        print(__doc__)
        return 0
    i = argv.index("--probe-grab")
    out_file = ""
    if "--probe-out" in argv:
        out_file = argv[argv.index("--probe-out") + 1]

    def emit(payload):
        text = json.dumps(payload)
        if out_file:
            try:
                with open(out_file, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception:  # noqa: BLE001
                pass
        try:
            print(text)          # 有控制台时顺便打出来，方便手工排查
        except Exception:  # noqa: BLE001
            pass

    try:
        bbox = tuple(int(v) for v in argv[i + 1].split(","))
    except Exception:  # noqa: BLE001
        emit({"ok": False, "why": "bad bbox"})
        return 0
    # 子进程也要 DPI 感知，否则坐标是多一层虚拟化的
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        pass
    fn = dxgi_grabber(bbox)
    if fn is None:
        emit({"ok": False, "why": "unavailable", "gpu": gpu_name()})
        return 0
    ms = _timeit(fn, rounds=4)
    if ms is None:
        emit({"ok": False, "why": "grab failed", "gpu": gpu_name()})
        return 0
    emit({"ok": True, "ms": round(ms, 2), "gpu": gpu_name()})
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
