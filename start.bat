@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Missing .venv\Scripts\python.exe
    echo Create the Python virtualenv first:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -e .
    echo   .venv\Scripts\playwright install chromium
    pause
    exit /b 1
)

if not exist "webapp\node_modules" (
    echo Missing webapp\node_modules. Run:
    echo   cd webapp
    echo   npm install
    pause
    exit /b 1
)

echo Building frontend...
cd webapp
call npm run build
if errorlevel 1 (
    echo Frontend build failed.
    pause
    exit /b 1
)
cd ..

echo Starting app at http://127.0.0.1:8765
echo Press Ctrl+C to stop.

.venv\Scripts\python.exe -m uvicorn --factory server.app:create_app --host 127.0.0.1 --port 8765
