"""一条命令查清「文件图标为什么不对」。

分两层，顺序很重要：

  A. 文件/打包层 —— exe 里到底有没有图标
     （`RT_GROUP_ICON` 的组数/ID/尺寸，以及和源 .ico 是否一致）
     这一层不对 → 打包参数问题，去改 --icon。

  B. 系统显示层 —— 系统能不能把图标画出来
     （关联 → UserChoice → Shell Icons 覆盖 → IconsOnly → 图标缓存尺寸）
     这一层不对 → 与程序无关，改打包没用。

用法：
    python scripts/diagnose_file_icons.py                 # 查默认目标
    python scripts/diagnose_file_icons.py a.exe b.zip     # 查指定文件
"""

from __future__ import annotations

import ctypes
import glob
import os
import struct
import sys
import winreg
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)


# ---------------------------------------------------------------- A. exe 图标
def ico_sizes(path: str) -> set:
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 6 or data[2:4] != b"\x01\x00":
        raise ValueError("%s 不是 ico" % path)
    count = data[4] | (data[5] << 8)
    out = set()
    for i in range(count):
        off = 6 + i * 16
        if off + 16 > len(data):
            break
        w, h = data[off], data[off + 1]
        out.add((w or 256, h or 256))
    return out


def exe_icon_groups(path: str) -> list[tuple]:
    """返回 [(组ID, 语言, 尺寸集合)] —— 组数 >1 时 Windows 只认 ID 最小的那组。"""
    import pefile
    pe = pefile.PE(path, fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
    groups = []
    root = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
    if root is not None:
        for entry in root.entries:
            if entry.id != 14:
                continue
            for sub in entry.directory.entries:
                gid = sub.id
                for lang in sub.directory.entries:
                    data = lang.data.struct
                    blob = pe.get_data(data.OffsetToData, data.Size)
                    sizes = set()
                    count = struct.unpack_from("<H", blob, 4)[0] if len(blob) > 6 else 0
                    for i in range(count):
                        off = 6 + i * 14
                        if off + 14 > len(blob):
                            break
                        w, h = blob[off], blob[off + 1]
                        sizes.add((w or 256, h or 256))
                    groups.append((gid, lang.id, sizes))
    pe.close()
    return groups


def check_packaging(targets: list[str]) -> list[str]:
    print("=" * 70)
    print("A. 文件/打包层：exe 里有没有图标")
    print("=" * 70)
    ico = os.path.join(ROOT, "assets", "officepack.ico")
    want = ico_sizes(ico) if os.path.isfile(ico) else set()
    if want:
        print("源图标 assets/officepack.ico：%d 张 %s" % (len(want), sorted(want)))
    problems = []
    for path in targets:
        if not os.path.isfile(path):
            continue
        name = os.path.basename(path)
        if not path.lower().endswith((".exe", ".dll")):
            continue
        try:
            groups = exe_icon_groups(path)
        except Exception as exc:  # noqa: BLE001
            print("  %-34s 读取失败：%s" % (name, exc))
            problems.append(name)
            continue
        if not groups:
            print("  %-34s ★ 没有任何图标资源" % name)
            problems.append(name)
            continue
        for gid, lang, sizes in groups:
            mark = ""
            if want and sizes != want:
                mark = "  ★ 与源 ico 不一致"
                problems.append(name)
            print("  %-34s 组ID=%-4s 语言=0x%04x  %d 张%s"
                  % (name, gid, lang, len(sizes), mark))
        if len(groups) > 1:
            print("  %-34s ★ 有 %d 组图标，Windows 只用 ID 最小的那组"
                  % ("", len(groups)))
    if not problems:
        print("  → 打包层正常：图标已写进 exe，且只有一组")
    return problems


# ---------------------------------------------------------------- B. 系统显示层
def reg(path: str, name: str, hive=winreg.HKEY_CURRENT_USER):
    try:
        with winreg.OpenKey(hive, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def check_system(sample_exts=(".exe", ".zip", ".txt", ".pdf", ".rar", ".md")) -> list[str]:
    print()
    print("=" * 70)
    print("B. 系统显示层：系统能不能把图标画出来")
    print("=" * 70)
    problems = []

    print("· 扩展名关联")
    for ext in sample_exts:
        progid = reg(ext, "", winreg.HKEY_CLASSES_ROOT)
        line = "    %-6s → %s" % (ext, progid or "★ 无关联")
        if progid:
            icon = reg(progid + r"\DefaultIcon", "", winreg.HKEY_CLASSES_ROOT)
            line += "   DefaultIcon=%s" % (icon or "★ 无")
        print(line)

    print("· UserChoice（优先级高于上面的 HKCR）")
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
    for ext in sample_exts:
        progid = reg(os.path.join(base, ext, "UserChoice"), "ProgId")
        if not progid:
            print("    %-6s 未设置（走默认，正常）" % ext)
            continue
        try:
            winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid).Close()
            print("    %-6s → %-38s ProgID 存在" % (ext, progid))
        except OSError:
            print("    %-6s → %-38s ★ ProgID 不存在（会白纸）" % (ext, progid))
            problems.append("UserChoice " + ext)

    print("· Shell Icons 覆盖键（美化工具后遗症）")
    for hive, label in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"),
                        (winreg.HKEY_CURRENT_USER, "HKCU")):
        path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Shell Icons"
        try:
            with winreg.OpenKey(hive, path) as key:
                n = winreg.QueryInfoKey(key)[1]
                for i in range(n):
                    name, value, _t = winreg.EnumValue(key, i)
                    head = str(value).split(",")[0].strip().strip('"')
                    exists = os.path.exists(os.path.expandvars(head)) if head else True
                    print("    %s %-4s = %s%s" % (label, name, value,
                                                  "" if exists else "  ★ 文件不存在"))
                    if not exists:
                        problems.append("ShellIcons %s" % name)
        except FileNotFoundError:
            print("    %s 不存在（正常）" % label)

    print("· 视图设置")
    adv = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
    icons_only = reg(adv, "IconsOnly")
    print("    IconsOnly = %s  %s" % (
        icons_only,
        "（只显示图标，正常）" if icons_only == 1
        else "（优先缩略图；缩略图链路坏了会全白）"))

    print("· 图标缓存尺寸 vs 当前缩放")
    try:
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
        class RECT(ctypes.Structure):
            pass
        scales = []

        def cb(hmon, _hdc, _lprc, _data):
            v = ctypes.c_int()
            try:
                ctypes.windll.shcore.GetScaleFactorForMonitor(hmon, ctypes.byref(v))
                scales.append(v.value)
            except Exception:  # noqa: BLE001
                pass
            return 1

        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), None)
        if scales:
            scale = max(scales)
            need = round(16 * scale / 100)
            print("    屏幕缩放 %d%%，列表视图图标约 %d px" % (scale, need))
            base_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                    "Microsoft", "Windows", "Explorer")
            have = set()
            for p in glob.glob(os.path.join(base_dir, "iconcache_*.db")):
                stem = os.path.basename(p).split("_")[1].split(".")[0]
                if stem.isdigit():
                    have.add(int(stem))
            print("    现有缓存尺寸：%s" % sorted(have))
            if need not in have:
                print("    ★ 缺 iconcache_%d.db —— 该缩放下图标可能画不出来" % need)
                problems.append("iconcache_%d" % need)
    except Exception as exc:  # noqa: BLE001
        print("    查询缩放失败：%s" % exc)

    print()
    if problems:
        print("发现问题：%s" % problems)
        print("→ 与程序打包无关。先试 scripts/fix_file_icons.py --yes（清缓存重启 explorer），")
        print("  还不行就改一次显示缩放（125% → 100% → 125%）强制系统重建对应尺寸的缓存。")
    else:
        print("系统层未发现异常。图标若仍不对，重启一次电脑基本能恢复。")
    return problems


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        targets = [os.path.abspath(a) for a in args]
    else:
        targets = [os.path.join(ROOT, "release", "OfficePack-GUI", "OfficePack-GUI.exe"),
                   os.path.join(ROOT, "release", "OfficePack-GUI-portable-singlefile.exe"),
                   os.path.join(ROOT, "release", "OfficePack-GUI", "启动.bat")]
    check_packaging(targets)
    check_system()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
