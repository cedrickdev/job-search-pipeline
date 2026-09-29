#!/usr/bin/env bash
# Take, list, prune and verify PostgreSQL backups (Phase 16 §45-48).
#
# A thin wrapper around `backend.app.cli.run_backup`, which owns the credential-safety rule: the
# database password is read from DATABASE_URL, handed to the libpq child through its ENVIRONMENT,
# and never placed on a command line or in any output. Prefer this over calling pg_dump by hand.
#
# Usage:
#   scripts/backup.sh create [--label pre-migration]   # write a timestamped dump
#   scripts/backup.sh list                              # list managed dumps, newest first
#   scripts/backup.sh prune                             # delete dumps older than retention
#   scripts/backup.sh verify [--name jobsearch-....dump]  # restore into scratch and check (§46)
#
# Configuration (all optional; sensible defaults):
#   JOBSEARCH_BACKUP_ARTIFACT_ROOT   where dumps are written (default var/backups)
#   JOBSEARCH_BACKUP_RETENTION_DAYS  prune age in days (default 30)
#   JOBSEARCH_PG_DUMP_PATH / _PG_RESTORE_PATH / _PSQL_PATH  pin client binaries
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

source .venv/bin/activate
# Load .env if present, so DATABASE_URL and any JOBSEARCH_BACKUP_* overrides are in the environment.
[ -f .env ] && export $(grep -v '^#' .env | xargs)

exec python -m backend.app.cli.run_backup "$@"
