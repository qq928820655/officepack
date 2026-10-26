"""把 dist 里的产物同步到 release/。

跟 cp -r 的差别只有两点，都是为了绕开 Windows 上的两个坑：
  1. 目标目录存在时先**整目录改名挪到 dist_prev**，而不是递归删除 ——
     一次删 1400 个文件会被安全策略拦下来，改名是原子的、零成本；
  2. 大文件先复制到 .part 再改名，中断时不会留下半个 zip 冒充成品。
"""

import os
import shutil
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
REL = os.path.join(ROOT, "release")
PREV = os.path.join(ROOT, "dist_prev")

PAIRS = [
    ("OfficePack-GUI-win64.zip", "OfficePack-GUI-win64.zip"),
    ("OfficePack-CLI-win64.zip", "OfficePack-CLI-win64.zip"),
    ("OfficePack-GUI-便携单文件（启动慢）.exe",
     "OfficePack-GUI-portable-singlefile.exe"),
    ("OfficePack-GUI", "OfficePack-GUI"),          # 目录：解压即用的那份
]


def copy_file(src, dst):
    part = dst + ".part"
    shutil.copy2(src, part)
    os.replace(part, dst)


def sync_dir(src, dst):
    if os.path.isdir(dst):
        stamp = time.strftime("%H%M%S")
        backup = os.path.join(PREV, "%s_before_%s" % (os.path.basename(dst), stamp))
        os.makedirs(PREV, exist_ok=True)
        shutil.move(dst, backup)
        print("   旧目录挪到 dist_prev/%s" % os.path.basename(backup))
    shutil.copytree(src, dst)
    n = sum(len(files) for _r, _d, files in os.walk(dst))
    print("   复制 %d 个文件" % n)


def main() -> int:
    totals = []
    for src_name, dst_name in PAIRS:
        src = os.path.join(DIST, src_name)
        dst = os.path.join(REL, dst_name)
        if not os.path.exists(src):
            print("[跳过] dist 里没有 %s" % src_name)
            continue
        print("[同步] %s → release/%s" % (src_name, dst_name))
        if os.path.isdir(src):
            sync_dir(src, dst)
            size = sum(os.path.getsize(os.path.join(r, f))
                       for r, _d, fs in os.walk(dst) for f in fs)
        else:
            copy_file(src, dst)
            size = os.path.getsize(dst)
        totals.append((dst_name, size))
    print()
    for name, size in totals:
        print("  %-46s %6.1f MB" % (name, size / 1024 / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
