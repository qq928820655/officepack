"""GUI 入口（供 PyInstaller 打包使用）。

注意：这里刻意不 import multiprocessing —— 在 PyInstaller onefile 冻结环境下，
导入 multiprocessing 会让窗口迟迟不出现（实测日志都写不出来）。
本项目不需要多进程，直接去掉。
"""

import sys
import traceback


def _fatal(exc: BaseException) -> None:
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "OfficePack 启动失败",
            "%s\n\n请把下面的信息反馈给开发者：\n\n%s" % (exc, detail[-1500:]),
        )
        root.destroy()
    except Exception:
        pass


def _selftest() -> int:
    """诊断模式：确认打包后 GUI 相关依赖是否都可用。"""
    lines = []
    ok = True
    try:
        import tkinter

        lines.append("tkinter      OK  (Tcl/Tk %s)" % tkinter.TkVersion)
    except Exception as exc:
        ok = False
        lines.append("tkinter      FAIL %s" % exc)
    try:
        from officepack import gui

        lines.append("tkinterdnd2  %s" % ("OK  （支持拖拽）" if gui._HAS_DND
                                          else "FAIL（拖拽不可用，按钮选择仍可用）"))
        ok = ok and gui._HAS_DND
    except Exception as exc:
        ok = False
        lines.append("officepack.gui FAIL %s" % exc)
    try:
        import cryptography

        lines.append("cryptography OK  (v%s)" % cryptography.__version__)
    except Exception as exc:
        ok = False
        lines.append("cryptography FAIL %s" % exc)

    text = "运行方式: %s\n%s" % (
        "独立 exe（无需 Python）" if getattr(sys, "frozen", False) else "源码运行",
        "\n".join(lines),
    )
    try:  # 有控制台时打印，无控制台时静默
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            (messagebox.showinfo if ok else messagebox.showwarning)(
                "OfficePack 自检" + ("通过" if ok else "异常"), text
            )
            root.destroy()
        except Exception:
            pass
    return 0 if ok else 1


def _run_gui() -> int:
    """构建窗口并进入事件循环。"""
    from officepack import gui

    has_dnd = gui._HAS_DND
    if has_dnd:
        try:
            from tkinterdnd2 import TkinterDnD

            root = TkinterDnD.Tk()
        except Exception:  # pragma: no cover - 拖拽库异常时退回普通窗口
            import tkinter as tk

            root = tk.Tk()
            has_dnd = False
    else:
        import tkinter as tk

        root = tk.Tk()
    gui.App(root, has_dnd)
    root.mainloop()
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    try:
        code = _run_gui()
    except BaseException as exc:  # noqa: BLE001 - 打包后必须兜底提示
        _fatal(exc)
        code = 1
    sys.exit(code)
