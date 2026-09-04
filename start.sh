#!/bin/zsh
set -u

cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/python" ]; then
  echo "Missing .venv/bin/python. Create the Python virtualenv and install dependencies first."
  exit 1
fi

if [ ! -d "frontend/node_modules" ]; then
  echo "Missing frontend/node_modules. Run: cd frontend && npm ci"
  exit 1
fi

# `generate`, not `build`: the Nuxt app is `ssr: false`, so `nuxt build` produces a
# Nitro server and no index.html, while FastAPI serves plain files. `generate`
# prerenders into frontend/.output/public — the directory pipeline.paths.FRONTEND_DIST
# points at and server/app.py mounts.
echo "Building frontend..."
( cd frontend && npm run generate ) || exit 1

URL="http://127.0.0.1:8765"
echo "Starting full app at $URL"
echo "Press Ctrl+C to stop."

exec .venv/bin/python -m uvicorn --factory server.app:create_app --host 127.0.0.1 --port 8765
