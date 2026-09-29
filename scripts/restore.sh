#!/usr/bin/env bash
# Disaster recovery: restore a dump into a target PostgreSQL database (Phase 16 §46).
#
# This is the DESTRUCTIVE path — it restores over whatever database libpq points at, so it is
# deliberately loud and refuses to run without an explicit acknowledgement. For a NON-destructive
# check that a dump is restorable, use `scripts/backup.sh verify`, which restores into a throwaway
# scratch database and never touches live data.
#
# The one credential (the database password) MUST travel in the environment as PGPASSWORD — never
# on the command line, where `ps` and shell history would expose it (§45). This script puts nothing
# but the dump path on pg_restore's argv.
#
# Usage:
#   PGHOST=... PGPORT=5432 PGUSER=... PGPASSWORD=... PGDATABASE=recovery_target \
#     RESTORE_I_UNDERSTAND=yes scripts/restore.sh path/to/jobsearch-....dump
#
# The target database (PGDATABASE) must already exist and should be EMPTY. Create it first with
# `createdb`, or restore into a fresh database and repoint the application at it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

DUMP="${1:-}"
PG_RESTORE="${JOBSEARCH_PG_RESTORE_PATH:-pg_restore}"

if [[ -z "$DUMP" || ! -f "$DUMP" ]]; then
  echo "error: pass an existing dump file as the first argument" >&2
  echo "usage: RESTORE_I_UNDERSTAND=yes PGDATABASE=target scripts/restore.sh <dump-file>" >&2
  exit 1
fi

if [[ -z "${PGDATABASE:-}" ]]; then
  echo "error: set PGDATABASE to the target database (it must already exist and be empty)" >&2
  exit 1
fi

if [[ "${RESTORE_I_UNDERSTAND:-}" != "yes" ]]; then
  echo "refusing: restore is destructive and will overwrite objects in database '$PGDATABASE'" >&2
  echo "on host '${PGHOST:-localhost}'. Re-run with RESTORE_I_UNDERSTAND=yes once you are sure." >&2
  echo "For a safe, non-destructive check instead, use: scripts/backup.sh verify" >&2
  exit 1
fi

echo "restoring '$DUMP' into database '$PGDATABASE' on host '${PGHOST:-localhost}'..." >&2
# --no-owner/--no-privileges: portable across clusters whose roles differ, matching the dump.
# The connection is entirely in the environment (PG* vars); only the dump path is on the argv.
exec "$PG_RESTORE" --no-owner --no-privileges --dbname "$PGDATABASE" "$DUMP"
