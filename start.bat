@echo off
rem ============================================================
rem  Smart Voice Assistant - One-Click Start
rem  1) starts Ollama + GPT-SoVITS API (config: launcher_config.json)
rem  2) preloads Whisper
rem  3) opens the Web UI in your browser
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

set "PY=venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Virtual environment "venv" not found.
  echo         Please run setup\install.bat first to complete the installation.
  pause
  exit /b 1
)

echo [One-Click Start] Launching...
"%PY%" launcher.py
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo The program has exited normally.
) else (
  echo [ERROR] The program exited with code %RC%. Please check the messages above.
)
pause
exit /b %RC%
