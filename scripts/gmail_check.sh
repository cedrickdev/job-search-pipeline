#!/usr/bin/env bash
# Send pending recruiter follow-up emails via Gmail SMTP.
# Usage: ./scripts/gmail_check.sh [--list] [--dry-run]
#
# Requires env vars:
#   export GMAIL_USER=you@gmail.com
#   export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

source .venv/bin/activate
[ -f .env ] && export $(grep -v '^#' .env | xargs)
python -m pipeline.gmail_recruiter "$@"
