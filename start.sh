#!/bin/zsh
set -u

cd "$(dirname "$0")" || exit 1

# Reuse the local Compose stack instead of silently starting an unconfigured
# second API on 8766 when Docker already owns 8765.
if [ -z "${JOBSEARCH_PORT:-}" ] && command -v docker >/dev/null 2>&1; then
  running_services="$(docker compose ps --services --status running 2>/dev/null)"
  if [[ $'\n'"$running_services"$'\n' == *$'\napi\n'* ]] &&
     [[ $'\n'"$running_services"$'\n' == *$'\nfrontend\n'* ]]; then
    echo "L’application Docker est déjà démarrée : http://127.0.0.1:3000"
    echo "Ouvrez cette adresse pour utiliser le terminal intégré."
    exit 0
  fi
fi

# WeasyPrint loads Pango/GLib through ctypes. On Apple Silicon Homebrew keeps
# those libraries under /opt/homebrew/lib, which is not always in Python's
# dynamic-loader search path. Make the local launcher self-contained while
# preserving an explicitly configured path.
if [ -d "/opt/homebrew/lib" ]; then
  if [ -n "${DYLD_FALLBACK_LIBRARY_PATH:-}" ]; then
    export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib:${DYLD_FALLBACK_LIBRARY_PATH}"
  else
    export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib"
  fi
fi

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

HOST="${JOBSEARCH_HOST:-127.0.0.1}"
DEFAULT_PORT=8765
PORT="${JOBSEARCH_PORT:-$DEFAULT_PORT}"

port_is_busy() {
  local candidate="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$candidate" -sTCP:LISTEN >/dev/null 2>&1
    return $?
  fi
  .venv/bin/python - "$HOST" "$candidate" <<'PY'
import socket
import sys

host, raw_port = sys.argv[1:]
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    probe.settimeout(0.2)
    sys.exit(0 if probe.connect_ex((host, int(raw_port))) == 0 else 1)
PY
}

# An explicit JOBSEARCH_PORT is an operator choice: fail with a useful message if
# it is occupied. With the default port, automatically move to the first free port
# in a small bounded range so a Docker API or another local service cannot make the
# full local workflow fail before Uvicorn starts. Existing processes are never killed.
if port_is_busy "$PORT"; then
  if [ -n "${JOBSEARCH_PORT:-}" ]; then
    echo "Port $PORT is already in use. Choose another port with JOBSEARCH_PORT=<port>."
    exit 3
  fi
  original_port="$PORT"
  attempts=0
  while port_is_busy "$PORT" && [ "$attempts" -lt 20 ]; do
    PORT=$((PORT + 1))
    attempts=$((attempts + 1))
  done
  if port_is_busy "$PORT"; then
    echo "No free local port found between $original_port and $PORT."
    exit 3
  fi
  echo "Port $original_port is already in use; using port $PORT instead."
fi

URL="http://$HOST:$PORT"
echo "Starting full app at $URL"
echo "Press Ctrl+C to stop."

exec .venv/bin/python -m uvicorn --factory server.app:create_app --host "$HOST" --port "$PORT"
