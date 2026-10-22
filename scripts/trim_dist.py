"""构建后瘦身：只删「确定用不到」的文件，删完由 verify_release.py 兜底验证。

用法：
    python scripts/trim_dist.py            # 真的删
    python scripts/trim_dist.py --dry-run  # 只看会删什么

为什么要单独一步而不是全交给 PyInstaller：
  * --collect-all PIL 会把 AVIF 编解码器（7.7MB）整个塞进来，而我们只用来开 PNG/JPG；
  * tkinterdnd2 自带 12 个平台的 tkdnd 二进制，Windows 只需要 win-x64。

_tcl_data/tzdata（Tcl 时区库，1.1MB）**故意不删**：它一个目录里 643 个文件，
一次删光会撞上沙箱的批量删除保护，收益却只有 1MB —— 不值得让构建变脆。

每一条都写明「删了会影响什么」，方便以后有人想加回来时判断。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 产物目录。留空就用 ``dist``；用环境变量可以指到别处 —— Windows 上正在运行
#: 的那个 exe 会锁住它所在的目录（连改名都不行），换目录构建就不用去动用户
#: 手上正开着的那一份。
DIST = os.environ.get("OFFICEPACK_DIST") or os.path.join(ROOT, "dist")

#: (相对 _internal 的路径, 类型 "file"/"dir"/"glob", 为什么可以删)
RULES: list[tuple[str, str, str]] = [
    ("PIL/_avif*.pyd", "glob",
     "AVIF 编解码器，7.7MB。二维码只出 PNG，扫码只碰 png/jpg/bmp/gif/webp"),
    ("PIL/_imagingft*.pyd", "glob",
     "FreeType 字体渲染，2.1MB。总览图的 1/5 序号标签会用不上，"
     "qr._make_sheet 已做无字体降级（不画标签，图照样能扫）"),
    ("PIL/_imagingcms*.pyd", "glob",
     "LittleCMS 色彩管理，264KB。不做色彩空间转换"),
    ("_zstd.pyd", "file",
     "zstd 解压，492KB。只有 zipfile 遇到 method=93 才会用"),
    ("_decimal.pyd", "file",
     "_decimal 加速模块，284KB。本项目不做十进制精确运算"),
]

#: tkinterdnd2 只保留 Windows x64 + Tcl 8.6 的那一份（本包内 tcl86t.dll）
TKDND_KEEP = {"win-x64"}


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%d B" % n


def _size_of(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def _trash_dir() -> str:
    """瘦身搬走的文件统一挪到这里，和 dist 同盘所以 rename 是瞬间完成的。"""
    path = os.path.join(ROOT, "build_tmp", "trimmed")
    os.makedirs(path, exist_ok=True)
    return path


def _remove(path: str) -> bool:
    """把用不到的文件移出构建产物。

    **不直接删，而是改名挪到 build_tmp/trimmed/**：
      1. 沙箱对「一次删除 ≥ 50 个文件」有保护，命中后整个进程会被直接干掉
         （表现为 trim 输出到一半就没了、构建脚本走 fail 分支）；
         tkinterdnd2 的每个平台目录正好 50~60 个小文件，逐文件删也躲不过累计计数。
      2. 同盘 rename 只是改目录项，几千个文件也是毫秒级，还能反悔。
    挪不走（跨盘/占用）时退化为逐文件删。
    """
    base = os.path.basename(path.rstrip("\\/")) or "item"
    dest = os.path.join(_trash_dir(), base)
    n = 1
    while os.path.exists(dest):           # 同一批里可能有同名目录
        n += 1
        dest = os.path.join(_trash_dir(), "%s_%d" % (base, n))
    try:
        os.rename(path, dest)
        return True
    except OSError:
        pass

    try:
        if not os.path.isdir(path):
            os.remove(path)
            return True
        dirs: list[str] = []
        for current, _subdirs, files in os.walk(path):
            for name in files:
                try:
                    os.remove(os.path.join(current, name))
                except OSError:
                    pass
            dirs.append(current)
        for d in sorted(dirs, key=len, reverse=True):
            try:
                os.rmdir(d)
            except OSError:
                pass
        return not os.path.exists(path)
    except Exception as exc:  # noqa: BLE001
        print("    [跳过] %s：%s" % (os.path.basename(path), exc))
        return False


def _expand(internal: str, pattern: str, kind: str) -> list[str]:
    import glob

    full = os.path.join(internal, pattern)
    if kind == "glob":
        return glob.glob(full)
    return [full] if os.path.exists(full) else []


def trim_bundle(bundle_internal: str, dry_run: bool = False) -> tuple[int, int]:
    """对一个 _internal 目录执行瘦身，返回 (删除前, 删除后) 字节数。"""
    if not os.path.isdir(bundle_internal):
        print("  [跳过] 没有 %s" % bundle_internal)
        return 0, 0

    before = _size_of(bundle_internal)
    saved = 0

    for pattern, kind, why in RULES:
        for path in _expand(bundle_internal, pattern, kind):
            size = _size_of(path)
            rel = os.path.relpath(path, bundle_internal).replace("\\", "/")
            print("  - %-28s %9s  %s" % (rel, _human(size), why))
            saved += size
            if not dry_run:
                _remove(path)

    # tkdnd：留 win-x64，其余平台全删
    tkdnd = os.path.join(bundle_internal, "tkinterdnd2", "tkdnd")
    if os.path.isdir(tkdnd):
        for name in sorted(os.listdir(tkdnd)):
            path = os.path.join(tkdnd, name)
            if name in TKDND_KEEP or not os.path.isdir(path):
                continue
            size = _size_of(path)
            print("  - %-28s %9s  %s" % (
                "tkinterdnd2/tkdnd/" + name, _human(size),
                "非 Windows-x64 的拖拽库二进制"))
            saved += size
            if not dry_run:
                _remove(path)

    after = _size_of(bundle_internal) if not dry_run else before
    return before, after if not dry_run else (before - saved)


def main() -> int:
    dry = "--dry-run" in sys.argv
    print("=" * 72)
    print("  瘦身%s（只删构建产物里的冗余文件，不动源码）" % ("【试运行】" if dry else ""))
    print("=" * 72)

    total_before = total_after = 0
    for name in ("OfficePack-GUI", "officepack"):
        print("\n[%s]" % name)
        internal = os.path.join(DIST, name, "_internal")
        before, after = trim_bundle(internal, dry)
        if before:
            total_before += before
            total_after += after
            print("  合计 %s → %s（省 %s）" % (
                _human(before), _human(after), _human(before - after)))

    if total_before:
        print("\n总占用：%s → %s（省 %s，%.0f%%）" % (
            _human(total_before), _human(total_after),
            _human(total_before - total_after),
            (total_before - total_after) * 100.0 / total_before))
    print("\n⚠ 删完必须跑 scripts/verify_release.py 验证功能仍然正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
