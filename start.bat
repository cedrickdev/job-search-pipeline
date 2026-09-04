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

if not exist "frontend\node_modules" (
    echo Missing frontend\node_modules. Run:
    echo   cd frontend
    echo   npm ci
    pause
    exit /b 1
)

rem `generate`, not `build`: the Nuxt app is ssr:false, so `nuxt build` leaves a
rem Nitro server and no index.html. `generate` prerenders into
rem frontend\.output\public, which is what FastAPI serves.
echo Building frontend...
cd frontend
call npm run generate
if errorlevel 1 (
    echo Frontend build failed.
    pause
    exit /b 1
)
cd ..

echo Starting app at http://127.0.0.1:8765
echo Press Ctrl+C to stop.

.venv\Scripts\python.exe -m uvicorn --factory server.app:create_app --host 127.0.0.1 --port 8765
