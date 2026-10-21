@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if not defined PY if exist "C:\Python310\python.exe" set "PY=C:\Python310\python.exe"
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY (
  echo [ERROR] Python 3.10+ not found. Please install Python with tkinter.
  pause
  exit /b 1
)

if "%~1"=="" (
  "%PY%" -m officepack gui
) else (
  "%PY%" -m officepack %*
)

if errorlevel 1 pause
