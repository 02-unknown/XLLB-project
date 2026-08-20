@echo off
rem ============================================================
rem  小笼洛包 1.0 - One-Click Installer
rem  Runs setup/install.py with a system Python (creates venv
rem  and installs all missing dependencies automatically).
rem This version was refactored by bilibili@我叫清少（UID:478929333）
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY (
  echo [ERROR] Python not found. Please install Python 3.9+ and add it to PATH.
  pause
  exit /b 1
)

echo [Install] Running one-click installer...
"%PY%" install.py
echo.
echo Installer finished. You can close this window.
pause
