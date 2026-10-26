"""修复「资源管理器里所有文件都显示白纸图标」。

诊断结论（本机实测）：

    Shell API（SHGetFileInfo）对 OfficePack-GUI.exe 返回的主色是 #1d4ed8，
    正是我们图标的蓝；.bat 返回 cmd 图标、.zip 返回 360zip 图标、.txt 返回
    记事本图标 —— 全都正常。

意思是：文件本身、扩展名关联、图标资源 DLL、缓存内容都没坏，
坏的只是 **Explorer 进程的图标索引**（iconcache_idx.db）没跟上，
于是一律画成白纸。

修法：停掉 Explorer → 删掉图标缓存（纯缓存，系统会自动重建）→ 拉起 Explorer。
影响：桌面会闪一下，开着的资源管理器窗口会关掉；运行中的其他程序不受影响。
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import time


def cache_dir() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", ""),
                        "Microsoft", "Windows", "Explorer")


def collect_targets(include_thumbs: bool) -> list[str]:
    base = cache_dir()
    out = glob.glob(os.path.join(base, "iconcache_*.db"))
    old = os.path.join(os.environ.get("LOCALAPPDATA", ""), "IconCache.db")
    if os.path.exists(old):
        out.append(old)
    if include_thumbs:
        out += glob.glob(os.path.join(base, "thumbcache_*.db"))
    return out


def explorer_pids() -> list[int]:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq explorer.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        return []
    pids = []
    for line in out.splitlines():
        parts = line.strip().strip('"').split('","')
        if len(parts) >= 2 and parts[0].lower() == "explorer.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def main() -> int:
    assume_yes = "--yes" in sys.argv
    targets = collect_targets(include_thumbs=True)
    total = sum(os.path.getsize(p) for p in targets if os.path.exists(p))

    print("将要清理：")
    print("  · 图标缓存 %d 个" % len([p for p in targets if "iconcache" in p.lower()]))
    print("  · 缩略图缓存 %d 个" % len([p for p in targets if "thumbcache" in p.lower()]))
    print("  · 合计 %.1f MB" % (total / 1e6))
    print()
    print("影响：桌面会闪一下、资源管理器窗口会关掉；其他程序不受影响。")
    print("这些都是纯缓存，Explorer 会自动重建，不会丢任何数据。")
    if not assume_yes:
        try:
            if input("\n继续？(y/N) ").strip().lower() != "y":
                print("已取消。")
                return 1
        except EOFError:
            print("没有交互输入，已取消（可加 --yes 直接执行）")
            return 1

    pids = explorer_pids()
    print("\n· 结束 Explorer（PID %s）…" % (pids or "未找到"))
    subprocess.run(["taskkill", "/F", "/IM", "explorer.exe"],
                   capture_output=True)
    time.sleep(2.0)

    removed = failed = 0
    freed = 0
    for path in targets:
        try:
            size = os.path.getsize(path)
            os.remove(path)
            removed += 1
            freed += size
        except FileNotFoundError:
            pass
        except OSError:
            failed += 1
    print("· 删除缓存：成功 %d 个 / 失败 %d 个，释放 %.1f MB"
          % (removed, failed, freed / 1e6))

    print("· 重新拉起 Explorer…")
    try:
        subprocess.Popen(["explorer.exe"])
    except Exception as exc:  # noqa: BLE001
        print("  启动失败：%s（请手动按 Ctrl+Shift+Esc → 文件 → 运行新任务 → explorer）" % exc)
        return 2

    print("\n完成。等 5~10 秒让 Explorer 重建缓存，然后打开 release 目录按 F5。")
    print("如果还是白纸，再按一次（刚开机时缓存重建要更久）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
