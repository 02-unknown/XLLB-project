@echo off
rem ============================================================
rem  Xiaolongluo 1.3 - Environment Diagnostics
rem  Runs setup/diagnose.py to find out why the Web UI cannot
rem  be opened. Read-only: only checks and reports, changes nothing.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (if exist "gpt_sovits\runtime\python.exe" set "PY=gpt_sovits\runtime\python.exe")

if not defined PY (
  echo [ERROR] No usable Python found to run the diagnostics.
  echo         Install Python 3.9+ from https://www.python.org/downloads/
  pause
  exit /b 1
)

echo [Diagnose] Running environment checks...
"%PY%" setup\diagnose.py
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo [ERROR] Diagnostics exited with code %RC%.
pause
exit /b %RC%
