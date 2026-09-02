#!/usr/bin/env bash
# Apply to all Approved jobs via Playwright.
# Usage: ./scripts/apply.sh [--headless] [--dry-run]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

source .venv/bin/activate
[ -f .env ] && export $(grep -v '^#' .env | xargs)
python -m pipeline.applier "$@"
