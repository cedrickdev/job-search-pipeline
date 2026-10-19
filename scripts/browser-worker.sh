#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Missing .venv/bin/python. Install the project dependencies first." >&2
  exit 1
fi

exec .venv/bin/python -m backend.app.cli.run_worker --lane browser "$@"
