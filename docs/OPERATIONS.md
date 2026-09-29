# Operations runbook (§32–58)

This is the operator's runbook for running the V2 platform: the service topology and how to start
it, the background workers and their lanes (§32–40), the explicit migration step (§58), the backup
schedule and recovery drill (§45–48), what to monitor and alert on (§41–44), and the rate-limiting
knobs (§49–52). It is the day-two companion to [production deployment](./PRODUCTION_DEPLOYMENT.md),
which covers day-one configuration and the fail-closed startup gate.

Each subsystem has a deep doc; this file is the index and the checklist. Follow the cross-references
for the full contract:

- [Production deployment](./PRODUCTION_DEPLOYMENT.md) — configuration, the startup gate, images
- [Backup and recovery](./BACKUP_RECOVERY.md) — the dump/restore contract
- [Observability](./OBSERVABILITY.md) — health, metrics, correlation ids, alerting
- [Rate limiting](./RATE_LIMITING.md) — the abuse-protection windows
- [Data lifecycle](./DATA_LIFECYCLE.md) — retention, export, deletion
- [SaaS billing](./SAAS_BILLING.md) and [usage and quotas](./USAGE_AND_QUOTAS.md) — the spine

## Service topology

`docker-compose.yml` is the development composition and the shape a production orchestrator mirrors
(§56). The long-running services are the database, Redis, the API and the two workers; the one-shot
jobs run in the same image and environment as the application.

| Service | Role | Profile |
| --- | --- | --- |
| `postgres` | PostgreSQL 17 + PostGIS, the persistence target | core |
| `redis` | rate-limit counters and the task subsystem's queue signalling | core |
| `api` | the FastAPI application (JSON API + built SPA) | core |
| `frontend` | the Nuxt dev server (dev only) | core |
| `worker` | the general lane: discovery, export, retention | `workers` |
| `browser-worker` | the browser lane: Playwright submissions only | `workers` |
| `migrate` | `alembic upgrade head`, the explicit schema step | `tools` |
| `import-v1` | the V1 SQLite importer | `tools` |
| `backup` | write a PostgreSQL dump | `tools` |
| `restore-check` | restore-drill the newest dump | `tools` |

```
docker compose up -d                        # db + Redis + API + frontend
docker compose --profile workers up -d      # add the general and browser workers
docker compose run --rm migrate             # alembic upgrade head (run once per deploy)
docker compose run --rm backup              # write a dump
docker compose run --rm restore-check       # restore-drill the newest dump
```

## Background workers and lanes (§32–40)

Work is a durable, DB-as-truth queue: the PostgreSQL task table is the source of truth and Redis
only signals that work is ready. Delivery is at-least-once with leases, retry and a dead-letter
lane, so a worker that dies mid-task never loses or double-commits it — handlers are idempotent
(§40). Two **separate** lanes, run as **separate containers**, so a wedged browser submission can
never consume a general-lane worker (§35):

```
python -m backend.app.cli.run_worker --lane general    # discovery, export, retention
python -m backend.app.cli.run_worker --lane browser    # Playwright application submissions
python -m backend.app.cli.run_worker --lane general --once   # drain ready work and exit
```

- **`general`** — discovery, account export and the retention sweep. Runs on the lean `runtime`
  image; scale it horizontally, each instance leases distinct work.
- **`browser`** — Playwright application submissions only, on the Playwright image. Its concurrency
  defaults to 1 so a stuck submission cannot starve the lane; scale by adding containers, not
  threads.
- **`--once`** drains the currently-ready work and exits — the mode a scheduled `RETENTION_SWEEP`
  cron uses when you would rather run the sweep on a timer than a resident worker.

A task that exhausts its retries lands in the dead-letter lane rather than blocking the queue.
Inspect and requeue dead-lettered work from the task table; a rising dead-letter count is an alert
(below).

## Migrations stay explicit (§58)

The schema is **never** migrated on API or worker startup — a service that migrates on boot migrates
on every scale-up and every restart loop, which is a race and a surprise. `alembic upgrade head` is
a deliberate step run **once** before rolling the new API and workers:

```
docker compose run --rm migrate     # or the equivalent one-shot Job in your orchestrator
```

`/api/v2` answers `503` until it has been run against a fresh database. Migrations are additive
(§62): a new revision adds tables/columns, it does not rewrite or drop what a running old version
still reads, so the migrate step can run before the new code rolls.

## Backup and recovery (§45–48)

`backup` and `restore-check` wrap `python -m backend.app.cli.run_backup`; the database password
reaches the libpq child through its environment, never a command line or any output.

| Command | Effect | Schedule |
| --- | --- | --- |
| `create` | timestamped custom-format dump under the artifact root (§45) | on your backup cadence (e.g. nightly) |
| `list` | managed dumps, newest first | ad hoc |
| `prune` | delete dumps older than `JOBSEARCH_BACKUP_RETENTION_DAYS` (§48) | after each `create` |
| `verify` | restore the newest dump into an isolated scratch DB, check its revision and record counts, then drop it (§46) | on your drill cadence |

**Exit codes:** `0` clean, `3` finished with a failure — a tool failed, or a `verify` came back
NOT VERIFIED (the restored schema is not at the expected revision). A `verify` exiting `3` is the
signal a monitored recovery drill watches for; wire it to an alert. See
[backup and recovery](./BACKUP_RECOVERY.md) for the RPO/RTO discussion and the full contract.

## Monitoring and alerting (§41–44)

Observability is mounted at the root by `install_observability` (`backend/app/observability/`):

| Endpoint | Answers | Touches |
| --- | --- | --- |
| `GET /health/live` | is this process up? | nothing external |
| `GET /health/ready` | can this process serve? | the database (the one dependency a request needs) |
| `GET /metrics` | Prometheus text exposition | refreshes pull gauges; database-down is a reading, not a crash |

Point a liveness probe at `/health/live` and a readiness probe at `/health/ready` so a database
outage takes an instance out of rotation without killing it. Every request carries a correlation id
(logged, never a secret or a body — §41). Alert on, at minimum:

- `/health/ready` failing (database unreachable) beyond the probe's grace,
- a rising dead-letter task count or a growing queue depth (workers wedged or under-scaled),
- a `verify` recovery drill exiting `3`,
- an elevated rate of `5xx` or of `QUOTA_EXCEEDED`/`429` (the latter can signal abuse or an
  under-provisioned limit).

Logs, metrics and error reports never carry a secret: no password hash, session/CSRF digest,
encrypted API-key ciphertext, provider credential, webhook secret or DB credential (§41). See
[observability](./OBSERVABILITY.md) for the metric catalogue and the logging contract.

## Rate limiting (§49–52)

Abuse-prone endpoints (register, login, re-auth-sensitive deletion, chat/LLM generation, document
generation, export generation, checkout) are rate limited with a horizontal-safe fixed window shared
across web workers via Redis. It is **additive** — it never weakens the DB-backed account lockout or
any other authentication protection — and it **fails open** on a Redis fault so a limiter outage
never locks everyone out (§50). Every knob is optional and defaults protect an unconfigured
deployment; tune per category in `.env.example` under "Rate limiting". Two operational cautions:

- **Trusted proxies (§51).** `JOBSEARCH_RATE_LIMIT_TRUSTED_PROXY_COUNT` defaults to `0` — the socket
  peer is the client identity and `X-Forwarded-For` is *not* trusted, because a forwarded header is
  otherwise attacker-controlled. Set it to the number of proxies that append to the header (e.g. `1`
  behind one load balancer) so the real client is read from the right position. Never over-count.
- **Never a permanent IP ban (§51).** The limiter throttles a window; it does not permanently ban a
  user by IP, and it never reveals whether an email exists or another account's internals (§52).

See [rate limiting](./RATE_LIMITING.md) for the window semantics and the full category table.

## Data lifecycle operations (§30–31)

The retention sweep (`RETENTION_SWEEP`, run on the general lane) purges *temporary* data past its
window — expired account-export archives, and any other bounded artifact — and **never** persistent
history. Windows are configurable (`JOBSEARCH_EXPORT_RETENTION_HOURS`, and the backup/queue windows
above); a non-positive window is rejected rather than making every artifact born already-expired.
Account export and deletion are user-initiated and owner-scoped; see
[data lifecycle](./DATA_LIFECYCLE.md) for the export/deletion contract. Note the platform provides
**GDPR-aware lifecycle controls**, not a claim of GDPR compliance (§29).

