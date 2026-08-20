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
if not exist "%PY%" set "PY=python"

echo [One-Click Start] Launching...
"%PY%" launcher.py

echo.
echo The program has exited. You can close this window.
pause
