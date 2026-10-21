@echo off
setlocal
cd /d "%~dp0"

set PY="C:/Python314/python.exe"
if not exist %PY% (
  echo [错误] 需要 C:/Python314/python.exe，且装好 tkinter、cryptography、Pillow、qrcode、zxing-cpp
  exit /b 1
)

rem 先把上次的产物挪走：运行中的 exe 会占用文件，安全策略也会拦住批量删除，
rem 这时 PyInstaller 照样打印 "Build complete"，但产物其实是旧的。
rem 注意必须「总是挪走，重名加序号」—— 老写法只在 dist_prev 里没有同名目录时才挪，
rem 跑过几轮之后就永远跳过，于是每次 COLLECT 都卡在删 1300 个文件上。
taskkill /F /IM OfficePack-GUI.exe >nul 2>&1
taskkill /F /IM officepack.exe >nul 2>&1
%PY% scripts\reset_dist.py
if errorlevel 1 goto fail

rem numpy / lxml / scipy 等是 PyInstaller 静态分析误带进来的（PIL 里有条件 import），
rem 运行时用不到，排掉能省 34MB（numpy.libs 的 OpenBLAS 就有 21MB）。
set EXCLUDES=--exclude-module numpy --exclude-module lxml --exclude-module scipy --exclude-module pandas --exclude-module matplotlib

echo ============================================
echo  构建目录版（推荐，启动 1~3 秒）
echo ============================================

echo [1/3] 图形界面 -^> dist\OfficePack-GUI\ ...
%PY% -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name OfficePack-GUI --paths . --collect-all tkinterdnd2 ^
  --collect-all qrcode --collect-all zxingcpp --collect-all PIL ^
  %EXCLUDES% ^
  --distpath dist --workpath build\gui --specpath build ^
  scripts\entry_gui.py
if errorlevel 1 goto fail

echo [2/3] 命令行 -^> dist\officepack\ ...
%PY% -m PyInstaller --noconfirm --clean --onedir --console ^
  --name officepack --paths . ^
  --collect-all qrcode --collect-all zxingcpp ^
  %EXCLUDES% ^
  --distpath dist --workpath build\cli --specpath build ^
  scripts\entry_cli.py
if errorlevel 1 goto fail

rem 瘦身：PyInstaller 的 --collect-all PIL 会把 AVIF 编解码器（7.7MB）、FreeType、
rem tkinterdnd2 的 12 个平台二进制全带进来。删完必须跑 verify_release.py 验证。
echo [3/3] 瘦身 -^> 去掉用不到的编解码器 / 字体 / 时区库 ...
%PY% scripts\trim_dist.py
if errorlevel 1 goto fail

echo.
echo 完成。dist 目录：
dir /b dist
echo.
echo 注意：启动.bat 不会被 PyInstaller 打包，每次重建都会丢失，请重新放回 dist\OfficePack-GUI\
echo 便携单文件版（每次启动要解压，实测 20~45 秒）：
echo 功能验证：执行 scripts\verify_release.py（会真的跑 exe，含 GUI 启窗检查）
echo       执行：build_exe.cmd portable
echo.
if /i "%~1"=="portable" goto portable
exit /b 0

:portable
echo ============================================
echo  构建单文件便携版
echo ============================================
%PY% -m PyInstaller --noconfirm --onefile --windowed ^
  --name "OfficePack-GUI-便携单文件（启动慢）" --paths . --collect-all tkinterdnd2 ^
  --collect-all qrcode --collect-all zxingcpp --collect-all PIL ^
  %EXCLUDES% ^
  --distpath dist --workpath build\gui_one --specpath build ^
  scripts\entry_gui.py
if errorlevel 1 goto fail
%PY% -m PyInstaller --noconfirm --onefile --console ^
  --name "officepack-便携单文件（启动慢）" --paths . ^
  --collect-all qrcode --collect-all zxingcpp ^
  %EXCLUDES% ^
  --distpath dist --workpath build\cli_one --specpath build ^
  scripts\entry_cli.py
if errorlevel 1 goto fail
echo 单文件版构建完成。
exit /b 0

:fail
echo.
echo [失败] 构建过程出错，请查看上方日志。
exit /b 1
