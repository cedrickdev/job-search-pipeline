#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Missing .venv\Scripts\python.exe"
    Write-Host "Create the virtualenv first:"
    Write-Host "  python -m venv .venv"
    Write-Host "  .venv\Scripts\pip install -e ."
    Write-Host "  .venv\Scripts\playwright install chromium"
    Read-Host "Press Enter to exit"
    exit 1
}

if (-not (Test-Path "webapp\node_modules")) {
    Write-Host "Missing webapp\node_modules. Run:"
    Write-Host "  cd webapp; npm install"
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "Building frontend..."
Push-Location webapp
npm run build
if ($LASTEXITCODE -ne 0) {
    Write-Host "Frontend build failed."
    Read-Host "Press Enter to exit"
    exit 1
}
Pop-Location

Write-Host "Starting app at http://127.0.0.1:8765"
Write-Host "Press Ctrl+C to stop."

& .venv\Scripts\python.exe -m uvicorn --factory server.app:create_app --host 127.0.0.1 --port 8765
