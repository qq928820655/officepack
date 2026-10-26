"""生成应用图标：assets/officepack.ico + assets/officepack.png。

设计：蓝色圆角底 + 一张白纸 + 纸上的二维码 —— 「文件 ↔ 码」一眼能看懂。
小尺寸（≤32）会自动换成简化画法：三个定位块 + 一小撮数据块，
因为 21×21 的格子缩到 16px 就糊成一团灰，什么都不像。
"""
from __future__ import annotations

import os
import struct
import tempfile

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "assets")

BLUE_TOP = (86, 145, 255)
BLUE_BOTTOM = (29, 78, 216)
INK = (23, 54, 138)

#: 21×21 的码面：True = 黑块。三个定位块 + 伪随机数据区（固定种子，每次一样）。
GRID = 21
FINDERS = ((0, 0), (GRID - 7, 0), (0, GRID - 7))


def _finder_cells():
    """三个定位块：7×7 外环 + 3×3 实心。分隔带交给 _data_cells 跳过。"""
    cells = set()
    for ox, oy in FINDERS:
        for y in range(7):
            for x in range(7):
                ring = x in (0, 6) or y in (0, 6)
                core = 2 <= x <= 4 and 2 <= y <= 4
                if ring or core:
                    cells.add((ox + x, oy + y))
    return cells


def _data_cells(step: int = 1, density: int = 18):
    """数据区：确定性伪随机，避开定位块和它们的一格分隔带。

    ``density`` 别调高：46% 时整张码在 64px 就糊成一团深蓝，三个定位块
    反而看不出来了；30% 才留得住「白底黑格」的呼吸感。
    """
    cells = set()
    for y in range(GRID):
        for x in range(GRID):
            if any(ox - 1 <= x <= ox + 7 and oy - 1 <= y <= oy + 7
                   for ox, oy in FINDERS):
                continue
            if x == 6 or y == 6:          # 时序线，固定黑白相间
                if (x + y) % 2 == 0:
                    cells.add((x, y))
                continue
            if ((x * 73856093) ^ (y * 19349663)) % 100 < density:
                cells.add((x, y))
    return cells


DARK = _finder_cells() | _data_cells()


def _gradient(size: int) -> Image.Image:
    img = Image.new("RGB", (1, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(1, size - 1)
        d.point((0, y), tuple(int(a + (b - a) * t) for a, b in zip(BLUE_TOP, BLUE_BOTTOM)))
    return img.resize((size, size), Image.NEAREST)


def _draw_finders(d, box, grid, ink, light, core=True):
    """在 box 里画三个定位块（7×7 比例），用于粗格子的小尺寸变体。"""
    x0, y0, x1, y1 = box
    unit = (x1 - x0) / grid
    for gx, gy in ((0, 0), (grid - 7, 0), (0, grid - 7)):
        a, b = x0 + gx * unit, y0 + gy * unit
        d.rectangle([a, b, a + 7 * unit, b + 7 * unit], fill=ink)
        d.rectangle([a + unit, b + unit, a + 6 * unit, b + 6 * unit], fill=light)
        if core:
            d.rectangle([a + 2 * unit, b + 2 * unit,
                         a + 5 * unit, b + 5 * unit], fill=ink)


def _draw_data_blocks(d, box, take, ink):
    """右下角几撮数据块：没有它，三个定位环看着像三个孤立的方框。"""
    unit = (box[2] - box[0]) / 21
    blocks = ((13, 13), (17, 14), (14, 17), (9, 14), (14, 9), (18, 18))
    for gx, gy in blocks[:take]:
        d.rectangle([box[0] + gx * unit, box[1] + gy * unit,
                     box[0] + (gx + 2) * unit, box[1] + (gy + 2) * unit], fill=ink)


def draw_icon(size: int) -> Image.Image:
    """按尺寸切换画法：大图标用「纸 + 细码」，小图标用「蓝底 + 粗码」。

    为什么必须分档：21×21 的细码缩到 48px，每格不到 1.7px，黑格会连成一片
    深蓝色块，三个定位块全糊掉（实测预览就是这个效果）。小尺寸换成三个
    大定位环 + 几个数据块，16px 也一眼能认出是二维码。
    """
    s = float(size)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # 圆角底
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1],
        radius=max(2, int(s * (0.22 if size > 96 else 0.18))), fill=255)
    img.paste(_gradient(size), (0, 0), mask)
    d = ImageDraw.Draw(img)

    if size <= 32:
        # 16~32px：蓝底白码。这个尺寸放不下「纸」，白纸会缩成一个白方块，
        # 反而把码挤没；直接用最高对比度的白码。
        pad = s * 0.15
        box = [pad, pad, s - pad, s - pad]
        _draw_finders(d, box, 21, (255, 255, 255, 255), BLUE_BOTTOM + (255,), core=False)
        _draw_data_blocks(d, box, 3, (255, 255, 255, 255))
        return img

    # 白纸（右上角折角）+ 码
    pad = s * 0.13
    d.rounded_rectangle([pad, pad, s - pad, s - pad],
                        radius=max(1, int(s * 0.05)), fill=(255, 255, 255, 255))
    fold = (s - 2 * pad) * 0.22
    d.polygon([(s - pad - fold, pad), (s - pad, pad), (s - pad, pad + fold)],
              fill=BLUE_TOP + (255,))
    d.polygon([(s - pad - fold, pad), (s - pad, pad + fold),
               (s - pad - fold, pad + fold)], fill=(198, 219, 255, 255))

    if size <= 96:
        # 40~96px：纸上的「粗码」—— 21 格细码在这个尺寸每格不到 1.7px，
        # 黑格会连成一团，所以换成三个大定位环 + 几个数据块。
        pad2 = s * 0.20
        box = [pad2, pad2, s - pad2, s - pad2]
        _draw_finders(d, box, 21, INK, (255, 255, 255, 255))
        _draw_data_blocks(d, box, 4 if size <= 48 else 6, INK)
        return img

    inner = s - 2 * pad
    qr_pad = inner * 0.09
    x0, y0 = pad + qr_pad, pad + qr_pad
    x1 = s - pad - qr_pad
    cell = (x1 - x0) / GRID
    for (gx, gy) in sorted(DARK):
        px, py = x0 + gx * cell, y0 + gy * cell
        d.rectangle([px, py, px + cell + 0.5, py + cell + 0.5], fill=INK)
    return img


def _single_ico(img: Image.Image, path: str) -> None:
    img.save(path, "ICO", sizes=[(img.width, img.height)])


def _dib_blob(img: Image.Image) -> bytes:
    """把一张图转成 ICO 里用的 DIB（32bpp BGRA + AND 掩码）。

    为什么不能用 PNG：Windows 只认 256×256 那一档用 PNG 压缩，16/32/48 这些
    小尺寸必须是 DIB 位图。全写成 PNG 时资源管理器读不出小图，图标就是白板
    （实测：8 个尺寸全是 PNG，exe 在文件夹里显示空白）。
    """
    rgba = img.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()

    xor = bytearray()
    for y in range(h - 1, -1, -1):          # DIB 行序自下而上
        for x in range(w):
            r, g, b, a = px[x, y]
            xor += bytes((b, g, r, a))

    stride = ((w + 31) // 32) * 4            # 1bpp，按 4 字节对齐
    and_mask = bytearray(stride * h)
    for y in range(h):
        row = (h - 1 - y) * stride
        for x in range(w):
            if px[x, y][3] == 0:             # 全透明的点标记为「挖掉」
                and_mask[row + (x >> 3)] |= 0x80 >> (x & 7)

    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0,
                         len(xor) + len(and_mask), 0, 0, 0, 0)
    return header + bytes(xor) + bytes(and_mask)


def merge_ico(images: dict[int, Image.Image], out_path: str) -> None:
    """把多张不同尺寸的图拼成一个 ico。

    为什么要自己拼：Pillow 的 ``sizes=`` 只会把**同一张**大图缩小，小尺寸会糊；
    这里每个尺寸都是单独画的（16px 用简化画法），拼装只是搬运条目。

    编码分两档：256 用 PNG（体积小），其余用 DIB（Windows 只认 DIB）。
    """
    import io

    blobs = []
    for size in sorted(images, reverse=True):
        img = images[size]
        if size >= 256:
            buf = io.BytesIO()
            img.save(buf, "PNG")
            blob = buf.getvalue()
        else:
            blob = _dib_blob(img)
        blobs.append((size, blob))

    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries, payload = b"", b""
    for size, blob in blobs:
        byte = size if size < 256 else 0     # 256 在这一字节里写字面量 0
        entries += struct.pack("<BBBBHHII", byte, byte, 0, 0, 1, 32, len(blob), offset)
        payload += blob
        offset += len(blob)
    with open(out_path, "wb") as fh:
        fh.write(header + entries + payload)


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    sizes = (256, 128, 64, 48, 40, 32, 24, 16)
    images = {s: draw_icon(s) for s in sizes}
    ico = os.path.join(OUT_DIR, "officepack.ico")
    merge_ico(images, ico)
    images[256].save(os.path.join(OUT_DIR, "officepack.png"), "PNG")
    # 给 Tk 的 wm iconphoto 用：多尺寸一起给，它在标题栏/任务栏各自挑合适的
    for s in (48, 32, 16):
        images[s].save(os.path.join(OUT_DIR, "officepack-%d.png" % s), "PNG")
    images[512] = draw_icon(512)
    images[512].save(os.path.join(OUT_DIR, "officepack-512.png"), "PNG")

    # 预览：把各尺寸放大到 128 排一行，方便肉眼检查
    preview = Image.new("RGB", (128 * len(sizes) + 8 * (len(sizes) + 1), 152), "#f2f4f8")
    x = 8
    for s in sizes:
        preview.paste(images[s].resize((128, 128), Image.NEAREST), (x, 12))
        x += 128 + 8
    preview.save(os.path.join(OUT_DIR, "icon_preview.png"), "PNG")
    print("已生成：%s（%d 字节）、officepack.png、icon_preview.png"
          % (ico, os.path.getsize(ico)))


if __name__ == "__main__":
    main()
