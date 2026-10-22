"""量一量「一枪」到底花多少时间：抓图 / 指纹 / 解码各占多少。"""
import sys, time
sys.path.insert(0, ".")

from officepack import qr
import zxingcpp
from PIL import Image

# 造一张和真机框选差不多大的码图
code = zxingcpp.create_barcode("#OPK1 DOC123 1/978 " + "A" * 900,
                              zxingcpp.BarcodeFormat.QRCode, ec_level="L")
view = memoryview(code.to_image(scale=4, add_quiet_zones=True))
h, w = view.shape[:2]
img = Image.frombuffer("L", (w, h), view.tobytes(), "raw", "L", 0, 1).copy()
print("码图 %dx%d" % img.size)
region = Image.new("RGB", (875, 875), (240, 240, 240))
region.paste(img.convert("RGB"), (10, 10))

N = 20

t = time.perf_counter()
for _ in range(N):
    qr._frame_key(region)
key_ms = (time.perf_counter() - t) / N * 1000

t = time.perf_counter()
for _ in range(N):
    qr.decode_image(region, fast=True, keep_markers=True)
dec_ms = (time.perf_counter() - t) / N * 1000

print("指纹 %6.1f ms" % key_ms)
print("解码 %6.1f ms  （fast=True）" % dec_ms)

t = time.perf_counter()
for _ in range(max(3, N // 4)):
    qr._read_one(region, deep=False)
deep_ms = (time.perf_counter() - t) / max(3, N // 4) * 1000
print("解码 %6.1f ms  （_read_one deep=False）" % deep_ms)

# 抓图
import ctypes
from officepack.gui import _make_grabber
from PIL import ImageGrab

box = (100, 100, 975, 975)
g = _make_grabber(box)
for name, fn in (("GDI BitBlt", g),
                 ("ImageGrab", lambda: ImageGrab.grab(bbox=box, all_screens=True))):
    if fn is None:
        continue
    fn()
    t = time.perf_counter()
    for _ in range(N):
        fn()
    print("抓图 %6.1f ms  （%s）" % ((time.perf_counter() - t) / N * 1000, name))

grab_ms = 20.0
if g is not None:
    t = time.perf_counter()
    for _ in range(N):
        g()
    grab_ms = (time.perf_counter() - t) / N * 1000

shot = grab_ms + key_ms + dec_ms + 50.0   # 50ms = interval
print()
print("单枪 ≈ %.0f ms → 约 %.1f 枪/秒" % (shot, 1000.0 / shot))
print("978 帧按 3 帧/秒播一轮 = 326 秒；这一轮能打 %.0f 枪，超采样 %.1f 倍"
      % (326000.0 / shot, 326000.0 / shot / 978))
