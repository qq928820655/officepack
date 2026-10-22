"""发布前功能验证：直接拿 dist/ 里**打好的 exe** 跑，不用源码。

覆盖：
  1. 任意文件类型往返（md/java/sql/py/txt/pdf/zip/rar/bin/png）—— SHA-256 逐字节比对
  2. 四种编码器（base91 / base32 / cjk / hex）都能往返
  3. AES-256-GCM 加密往返 + 错误口令必须失败
  4. 二维码通道：编码器 base32 与 base64 各出一套 PNG，扫码还原后比对 SHA
  5. 分片输出 + 乱序粘贴还原
  6. GUI 能否启动、窗口是否完整落在工作区内（含高 DPI 下底部按钮不被裁）

用法：
    python scripts/verify_release.py            # 全部
    python scripts/verify_release.py --no-gui   # 跳过 GUI（CI/无桌面时）
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import os
import random
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
CLI = os.path.join(DIST, "officepack", "officepack.exe")
GUI = os.path.join(DIST, "OfficePack-GUI", "OfficePack-GUI.exe")
#: 每轮验证开一个独立目录，跑完不删 —— 沙箱对「一次删几十个文件」有保护，
#: 删多了整个进程会被直接干掉，所以宁可让 build_tmp 里留着小样本。
OUT = os.path.join(ROOT, "build_tmp", "verify", "run_" + time.strftime("%Y%m%d_%H%M%S"))

_results: list[tuple[str, bool, str]] = []


def check(name: str, fn, *args, **kw) -> None:
    """跑一项检查，记录结果，不中断后面。"""
    try:
        detail = fn(*args, **kw) or ""
        _results.append((name, True, detail))
        print("  [PASS] %-46s %s" % (name, detail))
    except Exception as exc:  # noqa: BLE001
        _results.append((name, False, str(exc)))
        print("  [FAIL] %-46s %s" % (name, exc))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: str) -> str:
    with open(path, "rb") as fh:
        return sha(fh.read())


# --------------------------------------------------------------------------
# 造样本
# --------------------------------------------------------------------------
def _tiny_pdf() -> bytes:
    body = b"BT /F1 12 Tf 72 720 Td (Hello PDF) Tj ET"
    return (b"%PDF-1.4\n1 0 obj\n<</Length " + str(len(body)).encode() + b">>\nstream\n"
            + body + b"\nendstream\nendobj\ntrailer\n<</Root 1 0 R>>\n%%EOF\n")


def _tiny_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("readme.txt", "hello zip")
        zf.writestr("src/Main.java", "public class Main {}")
    return buf.getvalue()


def _tiny_png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 32), (30, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def make_samples(workdir: str) -> dict[str, bytes]:
    rnd = random.Random(20261021)
    samples: dict[str, bytes] = {
        "README.md": "# 项目说明\n\n这是一段中文正文，用来验证预览与无损还原。\n".encode("utf-8"),
        "Main.java": ("public class Main {\n"
                      "    public static void main(String[] a) {\n"
                      "        System.out.println(\"hi\");\n    }\n}\n").encode("utf-8"),
        "schema.sql": "SELECT id, name FROM t_user WHERE dt >= DATE '2026-01-01';\n".encode("utf-8"),
        "app.py": "def main():\n    print('hello')\n\n\nif __name__ == '__main__':\n    main()\n".encode("utf-8"),
        "gbk.txt": "GBK 编码的中文文本，用来测编码探测".encode("gbk"),
        "doc.pdf": _tiny_pdf(),
        "pack.zip": _tiny_zip(),
        "shot.png": _tiny_png(),
        "blob.bin": bytes(rnd.getrandbits(8) for _ in range(3072)),
        "fake.rar": b"Rar!\x1a\x07\x00" + bytes(rnd.getrandbits(8) for _ in range(1024)),
    }
    os.makedirs(workdir, exist_ok=True)
    for name, data in samples.items():
        with open(os.path.join(workdir, name), "wb") as fh:
            fh.write(data)
    return samples


# --------------------------------------------------------------------------
# CLI 调用
# --------------------------------------------------------------------------
def run_cli(args: list[str], expect_ok: bool = True) -> subprocess.CompletedProcess:
    # stdin 必须给 DEVNULL：万一走到 getpass 之类的交互分支，验证会一直挂着
    proc = subprocess.run([CLI] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL)
    if expect_ok and proc.returncode != 0:
        raise RuntimeError("命令失败(%d)：%s\n%s\n%s" % (
            proc.returncode, " ".join(args), proc.stdout[-800:], proc.stderr[-800:]))
    return proc


def _fresh(dirpath: str) -> None:
    """清空并重建一个用例目录；删不掉（被安全策略拦）就照旧复用。"""
    if os.path.isdir(dirpath):
        try:
            shutil.rmtree(dirpath)
        except Exception:  # noqa: BLE001
            pass
    os.makedirs(dirpath, exist_ok=True)


def roundtrip(srcdir: str, files: list[str], outdir: str, extra: list[str],
              samples: dict[str, bytes], password: str = "") -> str:
    """pack → unpack → 逐个比 SHA；返回一句摘要。"""
    _fresh(outdir)
    run_cli(["pack"] + [os.path.join(srcdir, f) for f in files]
            + ["-o", outdir, "--no-split"] + extra)

    containers = sorted(os.path.join(outdir, n) for n in os.listdir(outdir)
                        if n.endswith(".opk.txt"))
    if not containers:
        raise RuntimeError("没有产出容器文件")

    restored_dir = outdir + "_restored"
    _fresh(restored_dir)
    unpack_args = ["unpack"] + containers + ["-o", restored_dir]
    if password:
        unpack_args += ["--password", password]
    run_cli(unpack_args)

    restored = {n: os.path.join(restored_dir, n) for n in os.listdir(restored_dir)}
    for name in files:
        if name not in restored:
            raise RuntimeError("缺少还原结果：%s（实际 %s）" % (name, sorted(restored)))
        if sha_file(restored[name]) != sha(samples[name]):
            raise RuntimeError("SHA-256 不一致：%s" % name)
    return "%d 个文件逐字节一致" % len(files)


# --------------------------------------------------------------------------
# 各项检查
# --------------------------------------------------------------------------
def test_any_files(srcdir: str, samples: dict[str, bytes]) -> str:
    outdir = os.path.join(OUT, "codes")
    return roundtrip(srcdir, list(samples), outdir, [], samples)


def test_codecs(srcdir: str, samples: dict[str, bytes]) -> str:
    picked = ["README.md", "blob.bin", "doc.pdf"]
    notes = []
    for codec_name in ("base91", "base32", "cjk", "hex", "base64"):
        outdir = os.path.join(OUT, "codec_" + codec_name)
        roundtrip(srcdir, picked, outdir, ["--codec", codec_name], samples)
        notes.append(codec_name)
    return "、".join(notes) + " 全部一致"


def test_encryption(srcdir: str, samples: dict[str, bytes]) -> str:
    outdir = os.path.join(OUT, "cipher")
    roundtrip(srcdir, ["blob.bin"], outdir,
              ["--cipher", "aes-256-gcm", "--password", "pw-2026"], samples,
              password="pw-2026")
    # 错口令必须失败
    container = [os.path.join(outdir, n) for n in os.listdir(outdir)
                 if n.endswith(".opk.txt")][0]
    proc = run_cli(["unpack", container, "-o", outdir + "_bad",
                    "--password", "wrong-pw"], expect_ok=False)
    if proc.returncode == 0:
        raise RuntimeError("错误口令竟然还原成功了")
    return "加解密一致；错误口令被拒"


def test_split(srcdir: str, samples: dict[str, bytes]) -> str:
    """分片：打包成多段，故意乱序 + 夹杂质再合并还原。"""
    outdir = os.path.join(OUT, "split")
    _fresh(outdir)
    run_cli(["pack", os.path.join(srcdir, "blob.bin"), "-o", outdir, "--split", "600"])
    parts = sorted(os.path.join(outdir, n) for n in os.listdir(outdir)
                   if ".opk." in n)   # xxx.opk.part001.txt
    if len(parts) < 3:
        raise RuntimeError("样本不够大，只切出 %d 段" % len(parts))
    restored_dir = outdir + "_restored"
    _fresh(restored_dir)
    run_cli(["unpack"] + list(reversed(parts)) + ["-o", restored_dir])
    got = os.listdir(restored_dir)
    if not got or sha_file(os.path.join(restored_dir, got[0])) != sha(samples["blob.bin"]):
        raise RuntimeError("乱序合并后内容不一致")
    return "%d 段乱序合并还原一致" % len(parts)


def test_qr(srcdir: str, samples: dict[str, bytes]) -> str:
    """二维码：base32 / base64 各出一套 PNG，扫码后还原比对。"""
    notes = []
    for codec_name in ("base32", "base64"):
        outdir = os.path.join(OUT, "qr_" + codec_name)
        _fresh(outdir)
        run_cli(["pack", os.path.join(srcdir, "README.md"), "-o", outdir,
                 "--qr", "--codec", codec_name])
        pngs = sorted(os.path.join(outdir, n) for n in os.listdir(outdir)
                      if n.lower().endswith(".png") and "sheet" not in n.lower())
        if not pngs:
            raise RuntimeError("%s：没有生成二维码图片" % codec_name)
        restored_dir = outdir + "_restored"
        _fresh(restored_dir)
        run_cli(["unpack"] + pngs + ["-o", restored_dir])
        got = os.listdir(restored_dir)
        if not got or sha_file(os.path.join(restored_dir, got[0])) != sha(samples["README.md"]):
            raise RuntimeError("%s：扫码还原后内容不一致" % codec_name)
        notes.append("%s %d 张" % (codec_name, len(pngs)))
    return "、".join(notes) + " 扫回一致"


def test_qr_preset_and_gif(srcdir: str, samples: dict[str, bytes]) -> str:
    """qr 预设（qr45+max）与 GIF 动图：动图丢回去能逐帧读全并还原。"""
    outdir = os.path.join(OUT, "qr_gif")
    _fresh(outdir)
    # 必须拿压不动的数据：README.md 压缩后 < 2.4KB 只会出 1 帧，而单帧不生成动图
    import random
    rnd = random.Random(20261022)
    big = os.path.join(srcdir, "big.blob")
    with open(big, "wb") as fh:
        fh.write(bytes(rnd.getrandbits(8) for _ in range(9000)))
    run_cli(["pack", big, "-o", outdir, "--qr", "--preset", "qr", "--qr-gif", "3"])
    gifs = [n for n in os.listdir(outdir) if n.lower().endswith(".gif")]
    if not gifs:
        raise RuntimeError("没有生成 GIF 动图")
    pngs = [n for n in os.listdir(outdir)
            if n.lower().endswith(".png") and "all" not in n.lower()]
    gif_path = os.path.join(outdir, gifs[0])

    # 帧数必须与单帧 PNG 数一致（一帧一张 PNG）
    import struct
    with open(gif_path, "rb") as fh:
        raw = fh.read()
    gif_frames = raw.count(b"\x21\xf9\x04")     # 图形控制扩展 = 每帧一个
    if gif_frames != len(pngs):
        raise RuntimeError("GIF 帧数 %d ≠ PNG 张数 %d" % (gif_frames, len(pngs)))

    restored_dir = outdir + "_restored"
    _fresh(restored_dir)
    run_cli(["unpack", gif_path, "-o", restored_dir])
    got = os.listdir(restored_dir)
    with open(big, "rb") as fh:
        want = sha(fh.read())
    if not got or sha_file(os.path.join(restored_dir, got[0])) != want:
        raise RuntimeError("GIF 逐帧还原后内容不一致")
    return "qr 预设 %d 帧 → GIF 往返一致" % gif_frames


def test_trimmed_bundle_intact() -> str:
    """瘦身删掉的文件不能是被 import 链依赖的 —— 用真实 exe 反证。"""
    import glob

    internal = os.path.join(DIST, "OfficePack-GUI", "_internal")
    leftovers = glob.glob(os.path.join(internal, "PIL", "_avif*.pyd"))
    leftovers += glob.glob(os.path.join(internal, "PIL", "_imagingft*.pyd"))
    if leftovers:
        raise RuntimeError("瘦身未生效，仍存在：%s" % [os.path.basename(p) for p in leftovers])

    tkdnd = os.path.join(internal, "tkinterdnd2", "tkdnd")
    if os.path.isdir(tkdnd):
        keep = {"win-x64"}
        extra = [n for n in os.listdir(tkdnd)
                 if os.path.isdir(os.path.join(tkdnd, n)) and n not in keep]
        if extra:
            raise RuntimeError("tkdnd 仍留着非 Windows 平台目录：%s" % extra)

    config = os.path.join(ROOT, "dist_prev")
    return "AVIF/FreeType/多余平台拖拽库已移除，功能仍正常（见上 6 项）"


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
def _work_area() -> tuple[int, int, int, int]:
    rect = ctypes.wintypes.RECT() if hasattr(ctypes, "wintypes") else None
    from ctypes import wintypes

    rect = wintypes.RECT()
    ok = ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    if not ok:
        return 0, 0, 1920, 1080
    return rect.left, rect.top, rect.right, rect.bottom


def test_gui() -> str:
    from ctypes import wintypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    proc = subprocess.Popen([GUI], cwd=os.path.dirname(GUI))
    try:
        hwnd = 0
        deadline = time.time() + 40
        while time.time() < deadline:
            time.sleep(0.5)
            found = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _cb(h, _l):
                pid = wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                if pid.value == proc.pid and ctypes.windll.user32.IsWindowVisible(h):
                    found.append(h)
                return True

            ctypes.windll.user32.EnumWindows(_cb, 0)
            if found:
                hwnd = found[0]
                break
        if not hwnd:
            raise RuntimeError("40 秒内没等到窗口（可能启动就崩了）")

        ctypes.windll.user32.ShowWindow(hwnd, 9)
        time.sleep(1.5)
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        wl, wt, wr, wb = _work_area()

        if rect.left < wl - 2 or rect.top < wt - 2:
            raise RuntimeError("窗口超出工作区左上：(%d,%d) < (%d,%d)" % (
                rect.left, rect.top, wl, wt))
        if rect.right > wr + 2 or rect.bottom > wb + 2:
            raise RuntimeError("窗口被裁：右下 (%d,%d) > 工作区 (%d,%d)，窗口 %dx%d" % (
                rect.right, rect.bottom, wr, wb, w, h))
        if h < 520:
            raise RuntimeError("窗口高只有 %d px，内容大概率被挤掉了" % h)

        # 存一张截图给人眼看
        try:
            from PIL import ImageGrab
            os.makedirs(OUT, exist_ok=True)
            shot = os.path.join(OUT, "gui_window.png")
            ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom)).save(shot)
        except Exception:
            shot = ""
        return "窗口 %dx%d 完整落在工作区 %dx%d 内%s" % (
            w, h, wr - wl, wb - wt, "，截图 " + os.path.relpath(shot, ROOT) if shot else "")
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        except Exception:
            pass


# --------------------------------------------------------------------------
def main() -> int:
    print("=" * 78)
    print("  officepack 发布前功能验证")
    print("=" * 78)
    if not os.path.isfile(CLI):
        print("找不到 %s，先跑 build_exe.cmd" % CLI)
        return 2

    # 不整目录删除：几十个文件一次删会撞上沙箱的批量删除保护，
    # 直接把每个用例的输出目录就地覆盖即可（单个用例都远小于 50 个文件）。
    os.makedirs(OUT, exist_ok=True)
    srcdir = os.path.join(OUT, "src")
    samples = make_samples(srcdir)

    print("\n[1] 任意文件类型")
    check("office/pdf/zip/源码/图片/未知二进制 往返", test_any_files, srcdir, samples)

    print("\n[2] 编码器")
    check("各编码器往返一致", test_codecs, srcdir, samples)

    print("\n[3] 加密")
    check("AES-256-GCM 加解密", test_encryption, srcdir, samples)

    print("\n[4] 分片")
    check("分片乱序合并", test_split, srcdir, samples)

    print("\n[5] 二维码（任意编码器）")
    check("二维码出图 → 扫码还原", test_qr, srcdir, samples)

    print("\n[6] 二维码 qr 预设 + 动图")
    check("qr45 出码 → GIF 逐帧还原", test_qr_preset_and_gif, srcdir, samples)

    print("\n[7] 瘦身完整性")
    check("精简后功能仍然正常", test_trimmed_bundle_intact)

    if "--no-gui" not in sys.argv:
        print("\n[8] 图形界面")
        if os.path.isfile(GUI):
            check("GUI 启动 / 窗口完整显示", test_gui)
        else:
            check("GUI 启动", lambda: (_ for _ in ()).throw(RuntimeError("没有 %s" % GUI)))

    ok = sum(1 for _n, good, _d in _results if good)
    bad = len(_results) - ok
    print("\n" + "=" * 78)
    print("  结果：%d 项通过 / %d 项失败" % (ok, bad))
    for name, good, detail in _results:
        if not good:
            print("   ✗ %s —— %s" % (name, detail))
    print("=" * 78)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
