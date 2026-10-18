@echo off
setlocal
cd /d "%~dp0"
uv sync --locked
if errorlevel 1 exit /b 1
pushd frontend
call npm ci
if errorlevel 1 exit /b 1
call npm run build
if errorlevel 1 exit /b 1
popd
uv run playwright install chromium
if errorlevel 1 exit /b 1
echo Setup complete. Run start.cmd, then open http://127.0.0.1:48321
