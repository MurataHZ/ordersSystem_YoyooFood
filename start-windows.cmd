@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 --version >nul 2>&1
if not errorlevel 1 (
  py -3 server.py --open
  pause
  exit /b
)
python --version >nul 2>&1
if not errorlevel 1 (
  python server.py --open
  pause
  exit /b
)
echo 请先安装 Python 3.9 或更新版本，再重新打开此文件。
echo 如果只想先看效果，可以直接双击 index.html，无需安装任何东西。
echo Python 安装时请勾选 Add python.exe to PATH。
pause
