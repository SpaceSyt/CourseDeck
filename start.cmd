@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.cmd first.
  exit /b 1
)
if not exist "frontend\dist\index.html" (
  echo Build the frontend first with setup.cmd.
  exit /b 1
)
echo CourseDeck: http://127.0.0.1:48321
echo Press Ctrl+C to stop. Your saved data will remain on this device.
".venv\Scripts\python.exe" -m coursedeck %*
