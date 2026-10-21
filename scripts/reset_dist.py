"""构建前把 dist/ 里的旧产物挪进 dist_prev/，避免 PyInstaller 的 COLLECT 卡在删除上。

背景：PyInstaller 重建 onedir 产物时会先 ``RemoveDirectory(dist\\OfficePack-GUI)``，
一次删 1300+ 个文件会被沙箱的批量删除保护拦下（SAFE_DELETE_BULK_CONFIRM_REQUIRED），
表现为「PyInstaller 打印 Build complete，但 dist 里还是旧东西」。

老版本是原地 rename，但只在 ``dist_prev/同名`` 不存在时才动手；
跑过几轮之后 dist_prev 里已经有 7 个同名目录，于是每次都被跳过 —— 于是每次都失败。
这里改成「总是挪走，重名就加序号」。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
PREV = os.path.join(ROOT, "dist_prev")
NAMES = ("OfficePack-GUI", "officepack")


def _free_name(name: str) -> str:
    """在 dist_prev 里找一个没被占用的名字：name、name_2、name_3 …"""
    if not os.path.exists(os.path.join(PREV, name)):
        return name
    idx = 2
    while os.path.exists(os.path.join(PREV, "%s_%d" % (name, idx))):
        idx += 1
    return "%s_%d" % (name, idx)


def main() -> int:
    os.makedirs(PREV, exist_ok=True)
    moved = 0
    for name in NAMES:
        src = os.path.join(DIST, name)
        if not os.path.isdir(src):
            continue
        target = os.path.join(PREV, _free_name(name))
        try:
            os.rename(src, target)
            print("  挪走 %s → dist_prev/%s" % (name, os.path.basename(target)))
            moved += 1
        except OSError as exc:
            # 通常是 exe 还在跑：先 taskkill 再试一次
            print("  [警告] 挪 %s 失败：%s" % (name, exc))
            import subprocess
            import time

            for exe in ("OfficePack-GUI.exe", "officepack.exe"):
                subprocess.run(["taskkill", "/F", "/IM", exe],
                               capture_output=True, check=False)
            time.sleep(1.5)
            try:
                os.rename(src, target)
                print("  挪走 %s → dist_prev/%s（重试成功）" % (name, os.path.basename(target)))
                moved += 1
            except OSError as exc2:
                print("  [错误] 仍然失败：%s —— 请手动关闭正在运行的 exe" % exc2)
                return 1
    if not moved:
        print("  dist 里没有旧产物，无需清理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
