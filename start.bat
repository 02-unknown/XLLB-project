@echo off
rem ============================================================
rem  Xiaolongluo - One-Click Start (graphical launcher)
rem  Version is read from version.txt (nothing hardcoded here).
rem  Uses pythonw.exe to start launcher_gui.py, so no console window is
rem  kept. Services are started from the graphical launcher page
rem  (Lite / Standard), then the same window shows the chat UI.
rem  Tips: double-click 启动.vbs for a completely window-less start;
rem        run "venv\Scripts\python.exe launcher.py" for the console version.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

rem 版本号统一从 version.txt 读取（要改版本号只改这一个文件）
set "APPVER="
if exist "version.txt" for /f "usebackq delims=" %%v in ("version.txt") do if not defined APPVER set "APPVER=%%v"
if not defined APPVER set "APPVER=unknown"

title Xiaolongluo %APPVER% - graphical launcher

set "PYW=venv\Scripts\pythonw.exe"
if not exist "%PYW%" (
  echo [ERROR] Virtual environment "venv" not found.
  echo         Please run setup\install.bat first to complete the installation.
  pause
  exit /b 1
)

echo [One-Click Start] Xiaolongluo %APPVER% - opening the graphical launcher...
echo (This window closes by itself; logs are in runtime\logs\launcher_gui.log)
start "" "%PYW%" "launcher_gui.py"
exit /b 0
