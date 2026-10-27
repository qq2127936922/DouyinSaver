@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist ".venv\Scripts\pythonw.exe" (
  echo Python env not found.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" app.py
