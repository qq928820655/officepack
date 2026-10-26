"""GUI 入口（供 PyInstaller 打包使用）。

注意：这里刻意不 import multiprocessing —— 在 PyInstaller onefile 冻结环境下，
导入 multiprocessing 会让窗口迟迟不出现（实测日志都写不出来）。
本项目不需要多进程，直接去掉。
"""

import sys
import traceback


def _enable_dpi_awareness() -> None:
    """必须在创建任何窗口之前调用。

    不声明 DPI 感知时 Windows 会把整个窗口当成位图按缩放比拉伸：实测 125% 屏上
    exe 的窗口矩形是 1160x1130，但 PrintWindow 只画了左上角 928x904（正好 1/1.25）
    再放大填满 —— 字是虚的，而且 Tk 报的逻辑坐标与 ``SPI_GETWORKAREA`` 给的物理
    坐标差着一层缩放。之前那个「窗口底边压到任务栏下面 89px」的怪毛病就是这笔账。
    源码运行走的是 ``officepack.gui.main()``，那里早就调了，只有打包入口漏了。
    """
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_DPI_AWARE
        return
    except Exception:  # noqa: BLE001 - 老系统没有 shcore
        pass
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


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
    _enable_dpi_awareness()          # 任何窗口创建之前
    # 抓屏自检：主程序会拿这个入口再启动一个自己，专门试 DXGI 桌面复制。
    # 必须**在创建 Tk 之前**就分流，否则会闪出一个空窗口；驱动不支持时子进程
    # 会崩（访问违例），也只能崩在这里，不能把主窗口一起带走。
    if "--probe-grab" in sys.argv:
        # 注意别在 try 里直接 sys.exit()：SystemExit 是 BaseException，
        # 会被下面的兜底捕获，退出码就从 0 变成 2 了（实测踩过）。
        code = 2
        try:
            from officepack import screengrab

            code = int(screengrab._main(sys.argv[1:]) or 0)
        except BaseException:  # noqa: BLE001 - 子进程无论如何都不能弹错误框
            code = 2
        sys.exit(code)
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    try:
        code = _run_gui()
    except BaseException as exc:  # noqa: BLE001 - 打包后必须兜底提示
        _fatal(exc)
        code = 1
    sys.exit(code)
