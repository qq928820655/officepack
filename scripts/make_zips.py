"""把 dist 里的两个目录打成发布用 zip。

只做两件事：给 GUI 目录补一个双击即可启动的 `启动.bat`，然后按目录名压成
`dist/OfficePack-GUI-win64.zip` 和 `dist/OfficePack-CLI-win64.zip`。
"""

from __future__ import annotations

import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")

TARGETS = [
    ("OfficePack-GUI", "OfficePack-GUI-win64.zip", True),
    ("officepack", "OfficePack-CLI-win64.zip", False),
]

LAUNCHER = """@echo off
rem 双击启动（不弹黑框）；exe 就在同目录，_internal 必须跟着一起走
start "" "%~dp0OfficePack-GUI.exe"
"""

README = """OfficePack 图形版（免安装）
--------------------------------
双击 启动.bat 或 OfficePack-GUI.exe 即可。
整个文件夹要一起拷走（_internal 是运行库），不要只拿 exe。
"""


def _prepare(folder: str, gui: bool) -> None:
    if not gui:
        return
    with open(os.path.join(folder, "启动.bat"), "w", encoding="gbk", newline="\r\n") as fh:
        fh.write(LAUNCHER)
    with open(os.path.join(folder, "使用说明.txt"), "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(README)


def _zip(folder: str, out_path: str) -> int:
    base = os.path.basename(folder)
    count = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                full = os.path.join(root, name)
                arc = os.path.join(base, os.path.relpath(full, folder))
                zf.write(full, arc)
                count += 1
    return count


def main() -> int:
    for name, zip_name, gui in TARGETS:
        folder = os.path.join(DIST, name)
        if not os.path.isdir(folder):
            print("✗ 缺少 %s，先跑打包脚本" % folder)
            return 1
        _prepare(folder, gui)
        out = os.path.join(DIST, zip_name)
        count = _zip(folder, out)
        print("✅ %-28s %6.1f MB（%d 个文件）" % (zip_name, os.path.getsize(out) / 1048576,
                                                 count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
