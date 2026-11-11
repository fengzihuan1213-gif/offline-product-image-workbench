@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run the installation steps in README.md first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -B server.py
if errorlevel 1 pause
