@echo off
chcp 65001 >nul
REM AutoDroid 设备接入助手启动器（Windows）
REM 首次使用：右键→编辑本文件，把 SERVER 和 TOKEN 换成你的平台地址与 API Token
setlocal

set "SERVER=http://192.168.1.10:8000"
set "TOKEN=adk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
set "NAME=%COMPUTERNAME%"

where python >nul 2>nul
if errorlevel 1 (
  echo [!] 未找到 python，请安装 Python 3.8+ 并勾选 "Add python.exe to PATH"
  echo     下载: https://www.python.org/downloads/
  pause
  exit /b 1
)

python "%~dp0device_agent.py" --server "%SERVER%" --token "%TOKEN%" --name "%NAME%"
pause
