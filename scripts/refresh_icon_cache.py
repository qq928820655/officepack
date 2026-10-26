"""刷新 Windows 的图标缓存（换过 exe 却还显示旧图标时用）。

为什么会这样：资源管理器把「路径 → 图标」缓存在
``%LocalAppData%\\Microsoft\\Windows\\Explorer\\iconcache_*.db`` 里，
判断依据是文件路径 + 时间戳。而我们每次发布都是**同名同路径覆盖**
``OfficePack-GUI.exe``，缓存经常不认账，于是你看到的还是上一版的
Python 默认图标 —— 但 exe 里的图标其实早就是新的了。

三种办法，从轻到重：

  1. ``ie4uinit.exe -show`` —— 让 Explorer 自己重读，多数情况够用，无感；
  2. 删 iconcache_*.db + 重启 explorer.exe —— 最彻底，桌面会闪一下，
     开着的资源管理器窗口会关掉；
  3. 把 exe 换个文件名（比如 OfficePack-GUI-1.4.11.exe）—— 缓存键变了，
     立刻生效，不动系统任何东西。

本脚本默认做第 1 种；加 ``--hard`` 做第 2 种。第 3 种用 ``--copy``。
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys


def soft() -> int:
    print("· 让 Explorer 重读图标（ie4uinit -show）…")
    try:
        subprocess.run(["ie4uinit.exe", "-show"], check=False,
                       capture_output=True, timeout=20)
        print("  已请求。打开一个文件夹按 F5 看看。")
    except Exception as exc:  # noqa: BLE001
        print("  调用失败：%s" % exc)
    return 0


def cache_files() -> list[str]:
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                        "Microsoft", "Windows", "Explorer")
    out = glob.glob(os.path.join(base, "iconcache_*.db"))
    old = os.path.join(os.environ.get("LOCALAPPDATA", ""), "IconCache.db")
    if os.path.exists(old):
        out.append(old)
    return out


def hard(assume_yes: bool) -> int:
    files = cache_files()
    if not files:
        print("没找到 iconcache 文件（可能已被清理过）")
    else:
        total = sum(os.path.getsize(p) for p in files if os.path.exists(p))
        print("将删除 %d 个图标缓存文件（共 %.1f MB）：" % (len(files), total / 1e6))
        for path in files[:8]:
            print("   %s" % os.path.basename(path))
        if len(files) > 8:
            print("   …还有 %d 个" % (len(files) - 8))
        print("\n这些是纯缓存，删掉系统会自动重建，不影响任何程序和文件。")
        print("但需要先结束 explorer.exe 才能删（它正占着），桌面会闪一下。")
        if not assume_yes:
            try:
                answer = input("\n继续？输入 y 回车：").strip().lower()
            except EOFError:
                answer = ""
            if answer != "y":
                print("已取消。可以改用 --copy 换文件名的方式。")
                return 1

        subprocess.run(["taskkill", "/F", "/IM", "explorer.exe"],
                       check=False, capture_output=True)
        removed = 0
        for path in files:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
        print("已删除 %d / %d 个缓存文件" % (removed, len(files)))
        subprocess.Popen(["explorer.exe"])
        print("已重新启动资源管理器。稍等几秒再看图标。")
    return 0


def copy_mode(targets: list[str]) -> int:
    """把 exe 复制成带版本号的新名字 —— 缓存键变了，图标立刻生效。"""
    if not targets:
        targets = glob.glob("release/*.exe") + glob.glob("release/*/OfficePack-GUI.exe")
    made = []
    for src in targets:
        if not os.path.isfile(src):
            continue
        stem, ext = os.path.splitext(src)
        dst = "%s-%s%s" % (stem, "1.4.11", ext)
        shutil.copy2(src, dst)
        made.append(dst)
        print("已复制 → %s" % dst)
    if made:
        print("\n这些是**新文件名**，资源管理器一定会重新读图标 —— "
              "如果新文件显示的是二维码图标，就证明确实只是缓存问题。")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--hard" in args:
        return hard("--yes" in args)
    if "--copy" in args:
        return copy_mode([a for a in args if not a.startswith("-")])
    return soft()


if __name__ == "__main__":
    raise SystemExit(main())
