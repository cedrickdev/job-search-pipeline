# Backup & recovery

How the V2 platform takes a copy of its database, keeps that copy for a bounded
window, and — the part that makes a backup a *backup* rather than a hope —
**verifies** that the copy can actually be restored.

This document is the reference for Phase 16 §45–48 and §74. It states what is
backed up, what is deliberately *not*, how a dump is proven restorable without
touching live data, and the retention and recovery procedures an operator runs.

> **Engineering mechanism, not a certified DR plan.** This describes the tooling
> the platform ships and the guarantees it enforces in code. A deployment's own
> recovery objectives (RPO/RTO), off-site/immutable copies, and the schedule that
> drives these commands are operational choices layered on top — see
> [Recovery objectives](#recovery-objectives-rpo--rto) for the assumptions.

## One rule under everything: no credential ever leaves

The database password is the one secret a backup touches, and it never appears
where anything could capture it:

- **Never on a command line.** `pg_dump`/`pg_restore`/`psql` receive the
  connection through their *environment* (libpq's `PGPASSWORD`, `PGHOST`,
  `PGPORT`, `PGUSER`, `PGDATABASE`), so a process listing (`ps`), a shell history
  or a CI log shows only `host:port/db`. The one thing on `pg_restore`'s argv is
  the dump path. (`backend/app/backup/connection.py`, `tools.py`.)
- **Never in a report or an error.** A created dump is reported as a path, a size
  and an instant; a failure names its target by the redacted `host:port/db` and
  carries the tool's stderr — which cannot echo a password that was never on the
  line. (`backend/app/backup/errors.py`.)
- **Never committed.** Dumps are written under `var/backups/` (gitignored), and a
  dump is a full copy of the database — treat the directory as sensitive.

## What is backed up — and what is not

`pg_dump --format=custom` captures **the entire PostgreSQL database**: every
table, the schema, the `alembic_version` row, and the PostGIS objects revision
0001 installs. `--no-owner`/`--no-privileges` keep the dump portable to a cluster
whose roles differ.

A database dump does **not** include bytes that live *outside* PostgreSQL:

- rendered document PDFs under the document artifact root;
- account export archives under `var/account_exports/`;
- any other on-disk artifact directory.

Those are regenerable (a PDF re-renders from its stored source) or intentionally
temporary (an export expires — see [DATA_LIFECYCLE.md](DATA_LIFECYCLE.md)), so
the database is the thing whose loss is unrecoverable and the thing this backs
up. A deployment that stores artifacts on object storage backs those up with the
store's own mechanism; a deployment that keeps them on a local disk backs up that
disk separately.

## Taking a backup (§45)

```
scripts/backup.sh create [--label pre-migration]
```

Writes `var/backups/jobsearch-<UTC timestamp>[-<label>].dump`. The timestamp is
in the filename (not read from the filesystem mtime), so a dump copied to cold
storage still reports the day it was *taken*. An optional label
(alphanumeric/underscore) marks a dump — e.g. one taken right before a migration.

`scripts/backup.sh list` shows the managed dumps, newest first. Only files
matching the managed pattern are ever listed — a README or an operator's ad-hoc
copy in the directory is ignored, and (below) never pruned.

## Verifying a backup (§46) — the part that matters

> **A backup that has never been restored is not a verified backup.**

```
scripts/backup.sh verify [--name jobsearch-<...>.dump]
```

Verification restores the chosen dump (default: the newest) into an **isolated
scratch database** created beside the source — same server, a unique
`<db>_verify_<random>` name — then reads it back and drops it. It checks two
things:

1. **The restored schema is at the expected Alembic head.** A dump that predates
   a migration restores onto the wrong schema and is reported `NOT VERIFIED`. The
   expected head is resolved from the migration scripts at run time, not
   hardcoded.
2. **Representative records survived.** A dump that restored an empty `users`
   table restored nothing worth keeping; the row counts are reported.

Two guards make a verification incapable of harming live data:

- The scratch target can never equal the source (unique name), and
- the source is passed as a **protected** database to the very restore that runs,
  so the destructive-restore guard (below) would refuse it.

The scratch database is dropped in a `finally`, so a verification leaves nothing
behind even if the restore or a read fails.

**Exit code:** `verify` exits `0` when `VERIFIED`, `3` when `NOT VERIFIED` or on
any tool failure — the signal a scheduled recovery drill alerts on.

## Retention (§48)

```
scripts/backup.sh prune
```

Deletes managed dumps whose *filename instant* is older than
`JOBSEARCH_BACKUP_RETENTION_DAYS` (default 30, must be ≥ 1). Only recognised
dumps are ever candidates, so pruning can only remove backups this tooling wrote.
An off-site or cold-storage copy has its own, typically longer, policy — this
knob governs the local working set only.

## Restoring for real (disaster recovery)

`verify` proves a dump restorable; actual recovery restores into a live target
and is therefore **destructive**, so it is a separate, deliberately loud script:

```
PGHOST=... PGPORT=5432 PGUSER=... PGPASSWORD=... PGDATABASE=recovery_target \
  RESTORE_I_UNDERSTAND=yes scripts/restore.sh var/backups/jobsearch-<...>.dump
```

The target database must already exist and should be empty (create it with
`createdb`, or restore into a fresh database and repoint the application at it).
The password travels in `PGPASSWORD` (environment), never on the command line.
The script refuses to run without `RESTORE_I_UNDERSTAND=yes`, and points you at
`verify` for the safe, non-destructive check.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `JOBSEARCH_BACKUP_ARTIFACT_ROOT` | `var/backups` | Where dumps are written (relative → repo root). |
| `JOBSEARCH_BACKUP_RETENTION_DAYS` | `30` | Prune age in days (≥ 1). |
| `JOBSEARCH_PG_DUMP_PATH` | `pg_dump` | Pin the dump binary (client major ≥ server). |
| `JOBSEARCH_PG_RESTORE_PATH` | `pg_restore` | Pin the restore binary. |
| `JOBSEARCH_PSQL_PATH` | `psql` | Pin `psql` (scratch DB create/drop). |

The connection comes from `DATABASE_URL`/`JOBSEARCH_DATABASE_URL`, the same
resolution the application and migrations use — a backup can never target a
different database than the one running.

## Scheduling

These commands are stateless and idempotent, so any scheduler drives them: a cron
entry, a Kubernetes `CronJob`, or the platform's own worker. A representative
cadence:

- `create` — daily (and once with `--label` before every migration);
- `verify` — after each `create`, or at least daily, alerting on a non-zero exit;
- `prune` — daily, after `create`.

`verify` is the one that must be scheduled and alerted on: an unverified backup is
the failure mode this whole layer exists to prevent.

## Recovery objectives (RPO / RTO)

The tooling makes these *achievable*; the numbers are a deployment's to set.

- **RPO (how much data you can lose)** is bounded by the `create` interval — a
  daily backup means up to ~24h of data at risk. Shorten the interval, or add
  PostgreSQL WAL archiving/PITR (out of scope for this tooling), to tighten it.
- **RTO (how long recovery takes)** is dominated by `pg_restore` time on the
  target, which scales with database size. Rehearse `restore.sh` against a
  scratch host so the real RTO is measured, not guessed.

## Where the pieces live

| Concern | Module |
|---|---|
| DSN → libpq connection, redaction | `backend/app/backup/connection.py` |
| Typed, credential-free failures | `backend/app/backup/errors.py` |
| The libpq subprocess work | `backend/app/backup/tools.py` |
| Reading a restored database back | `backend/app/backup/inspector.py` |
| Orchestration + safety guards | `backend/app/backup/service.py` |
| Operator entrypoint | `backend/app/cli/run_backup.py` |
| Shell wrappers | `scripts/backup.sh`, `scripts/restore.sh` |
| Settings | `BackupSettings` in `backend/app/core/settings.py` |
