"""CLI 入口（供 PyInstaller 打包使用）。"""

import multiprocessing
import sys

from officepack.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
