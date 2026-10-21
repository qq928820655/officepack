"""等 GUI 窗口关闭后，把新构建的目录版替换到 dist/ 下。

运行中的 exe / dll 被系统独占，无法覆盖，所以只能等进程退出。
"""

import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

OLD = "dist/OfficePack-GUI"
NEW = "build/gui/OfficePack-GUI"
PARK = "dist/_old_gui"
LOG = "dist/替换结果.txt"

def alive() -> bool:
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq OfficePack-GUI.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="replace",
    )
    return "officepack-gui.exe" in r.stdout.lower()

def log(msg: str) -> None:
    print(msg)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")

if not os.path.isdir(NEW):
    log("✗ 没有可用的新构建（build/gui/OfficePack-GUI 不存在），请先运行 build_exe.cmd")
    sys.exit(1)

log("等待 OfficePack-GUI 关闭…（最多等 30 分钟）")
deadline = time.time() + 1800
while time.time() < deadline and alive():
    time.sleep(2)

if alive():
    log("✗ 超时：窗口仍未关闭，未替换。关掉后重新运行本脚本即可。")
    sys.exit(1)

try:
    if os.path.isdir(PARK):
        shutil.rmtree(PARK, ignore_errors=True)
    if os.path.isdir(OLD):
        os.rename(OLD, PARK)
    os.rename(NEW, OLD)
    log("✓ 已更新为最新版本（含完成提示与输出路径）")
except Exception as exc:  # noqa: BLE001
    log("✗ 替换失败：%s" % exc)
    sys.exit(1)

# 旧版本目录尽量删掉，失败就留给用户手动删
if os.path.isdir(PARK):
    try:
        shutil.rmtree(PARK)
        log("✓ 旧版本已清理")
    except Exception:
        log("提示：旧版本残留在 %s，可手动删除" % PARK)
