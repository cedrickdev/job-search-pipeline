#!/bin/zsh
set -u

cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/python" ]; then
  echo "Missing .venv/bin/python. Create the Python virtualenv and install dependencies first."
  exit 1
fi

if [ ! -d "webapp/node_modules" ]; then
  echo "Missing webapp/node_modules. Run: cd webapp && npm install"
  exit 1
fi

echo "Building frontend..."
( cd webapp && npm run build ) || exit 1

URL="http://127.0.0.1:8765"
echo "Starting full app at $URL"
echo "Press Ctrl+C to stop."

exec .venv/bin/python -m uvicorn --factory server.app:create_app --host 127.0.0.1 --port 8765
