@echo off
setlocal
cd /d "%~dp0"

set PY="C:/Python314/python.exe"
if not exist %PY% (
  echo [错误] 需要 C:/Python314/python.exe（含 tkinter 与 cryptography）
  exit /b 1
)

rem 正在运行的程序会占用 dist 目录，先关掉
taskkill /F /IM OfficePack-GUI.exe >nul 2>&1
taskkill /F /IM officepack.exe >nul 2>&1

rem 旧产物目录删不掉时（被占用 / 被安全软件保护），PyInstaller 仍会打印
rem "Build complete"，但产物其实是旧的。所以先改名挪开，保证这次一定是新的。
%PY% -c "import os; os.makedirs('dist_prev', exist_ok=True); [os.rename(os.path.join('dist', n), os.path.join('dist_prev', n)) for n in ('OfficePack-GUI', 'officepack') if os.path.isdir(os.path.join('dist', n)) and not os.path.exists(os.path.join('dist_prev', n))]"

echo ============================================
echo  构建目录版（推荐：启动 1~3 秒）
echo ============================================

echo [1/2] 图形界面 -^> dist\OfficePack-GUI\ ...
%PY% -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name OfficePack-GUI --paths . --collect-all tkinterdnd2 ^
  --distpath dist --workpath build\gui --specpath build ^
  scripts\entry_gui.py
if errorlevel 1 goto fail

echo [2/2] 命令行 -^> dist\officepack\ ...
%PY% -m PyInstaller --noconfirm --clean --onedir --console ^
  --name officepack --paths . ^
  --distpath dist --workpath build\cli --specpath build ^
  scripts\entry_cli.py
if errorlevel 1 goto fail

echo.
echo 完成。dist 目录：
dir /b dist
echo.
echo 注意：启动.bat 不会被 PyInstaller 保留，如已丢失请重新放入 dist\OfficePack-GUI\
echo 提示：如需单文件版（拷贝方便，但每次启动要解压 17MB，实测 20~45 秒），
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
  --distpath dist --workpath build\gui_one --specpath build ^
  scripts\entry_gui.py
if errorlevel 1 goto fail
%PY% -m PyInstaller --noconfirm --onefile --console ^
  --name "officepack-便携单文件（启动慢）" --paths . ^
  --distpath dist --workpath build\cli_one --specpath build ^
  scripts\entry_cli.py
if errorlevel 1 goto fail
echo 单文件版构建完成。
exit /b 0

:fail
echo.
echo [失败] 构建过程出错，请查看上面的日志。
exit /b 1
