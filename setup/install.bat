@echo off
rem ============================================================
rem  Xiaolongluo 1.3 - One-Click Installer
rem  Runs setup/install.py with a usable Python (project venv,
rem  system Python, or the bundled gpt_sovits runtime).
rem  Creates the venv and installs missing components.
rem  Safe to re-run any time to complete missing modules.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
set "PYCHECK="

rem Candidate 1: project virtualenv (preferred)
if exist "..\venv\Scripts\python.exe" set "PY=..\venv\Scripts\python.exe"
if defined PY call :check
if defined PY goto :found

rem Candidate 2: py launcher
where py >nul 2>nul && set "PY=py"
if defined PY call :check
if defined PY goto :found

rem Candidate 3: python on PATH
where python >nul 2>nul && set "PY=python"
if defined PY call :check
if defined PY goto :found

rem Candidate 4: bundled gpt_sovits runtime
if exist "..\gpt_sovits\runtime\python.exe" set "PY=..\gpt_sovits\runtime\python.exe"
if defined PY call :check
if defined PY goto :found

goto :no_python

:check
rem Validate that the candidate really runs Python.
rem This rejects the Microsoft Store placeholder alias,
rem which returns success but produces no output at all.
set "PYCHECK="
"%PY%" -c "import sys; print('PY_OK')" >"%TEMP%\xiaolongluo_pycheck.txt" 2>nul
set /p PYCHECK=<"%TEMP%\xiaolongluo_pycheck.txt"
del "%TEMP%\xiaolongluo_pycheck.txt" >nul 2>nul
if not "%PYCHECK%"=="PY_OK" set "PY="
set "PYCHECK="
exit /b 0

:found
echo [Install] Using Python: %PY%
echo [Install] Running one-click installer...
"%PY%" install.py
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo Installer finished OK.
) else (
  echo [ERROR] Installer failed with exit code %RC%. Please read the messages above.
)
pause
exit /b %RC%

:no_python
echo [ERROR] No usable Python found.
echo         Install Python 3.9+ from https://www.python.org/downloads/
echo         and check "Add python.exe to PATH", then re-run this installer.
echo         Note: the Microsoft Store placeholder does not count as Python.
pause
exit /b 1
