@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Yoyoo Food Station - Remote Server
echo 正在准备 Yoyoo Food Station 远程版，请保持电脑联网。
echo 第一次会从官方网站下载运行组件；之后无需重复安装。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare-remote.ps1"
if errorlevel 1 (
  echo.
  echo 启动没有完成。请保留上面的错误信息，并参考 远程使用说明.txt。
)
echo.
pause
