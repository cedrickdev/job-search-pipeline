#!/bin/zsh
set -u
# Resolve the project root from this script's own location, like every other
# script here: a hardcoded absolute path breaks the moment the repo is moved
# or cloned by anyone else.
cd "$(dirname "$0")/.." || exit 1
( cd frontend && npm run generate ) || exit 1
URL="http://127.0.0.1:8765"
( sleep 1 && open "$URL" ) &
exec .venv/bin/python -m uvicorn --factory server.app:create_app --host 127.0.0.1 --port 8765
