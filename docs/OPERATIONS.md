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
| `morning-scheduler` | evaluate Swiss V2 schedules and enqueue discovery | `workers` |
| `browser-worker` | the browser lane: application preparation and guarded submissions | `workers` |
| `migrate` | `alembic upgrade head`, the explicit schema step | `tools` |
| `seed-plans` | write/reconcile the plan catalogue, idempotently | `tools` |
| `import-v1` | the V1 SQLite importer | `tools` |
| `backup` | write a PostgreSQL dump | `tools` |
| `restore-check` | restore-drill the newest dump | `tools` |

```
docker compose up -d                        # db + Redis + API + frontend
docker compose --profile workers up -d      # add the general and browser workers
docker compose run --rm migrate             # 1. alembic upgrade head (once per deploy)
docker compose run --rm seed-plans          # 2. seed/reconcile the plan catalogue
docker compose run --rm backup              # write a dump
docker compose run --rm restore-check       # restore-drill the newest dump
```

The deployment order is **migrate → seed/reconcile plans → start services**: the schema exists,
then the catalogue the entitlement resolver reads exists, then the API and workers roll (see below).

## Background workers and lanes (§32–40)

### Swiss V2 scheduled and on-demand discovery

Settings stores a separate, per-account V2 schedule. `morning-scheduler` is
disabled by default until approved source access is configured and the operator
sets `JOBSEARCH_MORNING_SCHEDULER_ENABLED=true`. Once enabled it checks every
five minutes in `Europe/Zurich`, and enqueues one `MORNING` discovery
task per active Swiss saved search and local day. The queue's idempotency key
handles repeated ticks and both instants of a daylight-saving fallback. The
general `worker` must be running to collect offers; a queued state is not a
completed search, generated document, or candidature. Source health and warnings
are visible in `GET /api/v2/me/morning-runs/latest`; the map reads the persisted
catalog. The Settings “run now” action uses a separate `MANUAL` trigger and does
not enable the schedule.

Every V2 discovery entry point requires `JOBSEARCH_DISCOVERY_ALLOWED_SOURCES`,
a comma-separated operator allow-list of reviewed CH source keys. Empty by
default, it makes manual runs return HTTP 409 and scheduled runs enqueue
nothing. The general worker rechecks the list before a source request, even
for tasks queued before a configuration change. An unrestricted saved search
uses only allowed keys; an explicitly selected unapproved key is refused.
Setting this variable is not a permit: document source authorization, quotas,
storage conditions and a bounded non-submitting dry-run first. The current
review approves no live board; keep the list empty for real accounts.

Greenhouse and Lever use the separate V2 catalog
`config/ats_reviewed_boards.yaml`, which is empty by default. Do not copy the
legacy `config/companies.yaml` entries into it automatically. Before adding a
board, record its employer career-page provenance, applicable collection and
storage conditions, review date, Swiss posting evidence and a daily request
budget in `docs/SWISS_SOURCE_REVIEW_2026-10-12.md`. First deploy the V2 board
access gate and durable budget migration. Then add only reviewed `approved`
entries, restart both API and worker so they use the same catalog snapshot,
set `JOBSEARCH_DISCOVERY_ALLOWED_SOURCES=greenhouse,lever`, and use a bounded
non-submitting one-shot discovery run. Confirm source health, request counts and
actual CH locations before enabling the resident schedule. A listing feed's
approval does not enable Playwright form submissions; that is a separate gate.
The API loads and validates this file during startup; the worker and scheduler
load it when their process starts or tick begins. Editing the file while a
worker is running does not hot-reload its existing snapshot. Stop the worker,
restart API and worker with the reviewed catalog, then resume queued work; the
new worker rechecks source policy before fetching. Do not assume a queued task
carries approval from the time it was created.

For a disposable, migrated test database with synthetic accounts only:

```
python -m backend.app.cli.morning_scheduler --once --dry-run
python -m backend.app.cli.morning_scheduler --once
python -m backend.app.cli.run_worker --lane general --once
```

The dry-run rolls back queued work. On Compose, use the workers profile for
resident operation; a one-shot check is
`docker compose --profile workers run --rm morning-scheduler python -m backend.app.cli.morning_scheduler --once --dry-run`.
Compose's default volume names are fixed for the local application. A different
`-p` project name **alone does not isolate PostgreSQL**: it can mount the same
`jobsearch_postgres_data` volume and interrupt the running database. Before a
rehearsal, set a unique `JOBSEARCH_VOLUME_PREFIX` for **every** Compose command,
along with non-conflicting `POSTGRES_PORT`, `REDIS_PORT`, `API_PORT` and
`FRONTEND_PORT`; inspect `docker compose config --format json` and verify that
`volumes.postgres_data.name` is new before `up`. Never use `down -v` without
checking the resolved volume names. The prefix defaults to `jobsearch`, so
ordinary local operation keeps its existing volumes.
Do not point these rehearsal commands at the candidate's live database. No CH
jobboard in the current source-access review is approved for real automated
collection yet; fixtures prove orchestration only. LinkedIn remains disabled.

### Explicit Swiss catalog morning run (per-user CLI compatibility)

First create an active CH saved search for the candidate. The local command
below queues discovery for **one account's** eligible searches; it never
submits applications and never starts an implicit schedule. Use a disposable
test database for rehearsals. `--dry-run` counts source-approved searches while
rolling back queued tasks; omit it only when you intend to enqueue work:

```
python -m backend.app.cli.run_discovery --user-id <UUID> --dry-run
python -m backend.app.cli.run_discovery --user-id <UUID>
python -m backend.app.cli.run_worker --lane general --once
```

The general worker must run to execute queued discovery; the browser worker is
not needed for collection but is needed to prepare the resulting applications.
After `DOCUMENTS_RENDERED`, it consumes `APPLICATION_PREPARATION` and only an
exact reviewed form plus active policy can reach the submission queue. The
packaged form catalog is empty and live submission is off by default. The
Morning Run UI is read-only progress, not proof of delivery; the application
audit trail is authoritative. The default trigger is `MORNING`, with one run per
local Swiss calendar day coalesced for a saved search. The V2 scheduler above
now provides the built-in timer; this CLI remains an explicit per-user command.
Review run status, source health and warning codes in the map before
interpreting the result count. Partial coverage never means the Swiss market
has no matching vacancies. LinkedIn is disabled, jobs.ch is not integrated,
Jooble needs its API key, and ATS sources need employer board identifiers.
No live-source coverage or production-readiness guarantee follows from local
fixture tests.

Schema revision `0024` makes normalized URLs unique only for source records
without an external ID. Distinct source IDs may share a generic listing URL
without losing a role. A downgrade from `0024` to `0023` deliberately refuses
data containing such duplicates; clean them up explicitly before rollback.
The V1 synchronous fetch adapter stops its remaining request plan after an
orchestrator timeout and waits for any request already in flight before marking
the source unavailable. A single blocking HTTP request can therefore extend the
reported run beyond the nominal 20-second async timeout; monitor slow sources.

Work is a durable, DB-as-truth queue: the PostgreSQL task table is the source of truth and Redis
only signals that work is ready. Delivery is at-least-once with leases, retry and a dead-letter
lane, so a worker that dies mid-task never loses or double-commits it — handlers are idempotent
(§40). Two **separate** lanes, run as **separate containers**, so a wedged browser submission can
never consume a general-lane worker (§35):

```
python -m backend.app.cli.run_worker --lane general    # discovery, export, retention
python -m backend.app.cli.run_worker --lane browser    # application preparation and guarded submissions
python -m backend.app.cli.run_worker --lane general --once   # drain ready work and exit
```

- **`general`** — discovery, account export and the retention sweep. Runs on the lean `runtime`
  image; scale it horizontally, each instance leases distinct work.
- **`browser`** — committed application preparation and guarded Playwright submissions, on the Playwright image. Its concurrency
  defaults to 1 so a stuck submission cannot starve the lane; scale by adding containers, not
  threads.
- **`--once`** drains the currently-ready work and exits — the mode a scheduled `RETENTION_SWEEP`
  cron uses when you would rather run the sweep on a timer than a resident worker.

### Integrated Claude terminal cleanup

When the optional terminal profile is enabled, the general retention sweep includes one bounded
page of expired Claude terminal sessions. It asks the private manager to stop the labelled runtime
before it persists `EXPIRED`. A manager outage leaves the session retryable instead of claiming
cleanup that did not happen.

For an immediate operator pass, inspect first and then apply:

```bash
python -m backend.app.cli.reap_claude_terminals --dry-run
python -m backend.app.cli.reap_claude_terminals
```

The command reports counts only. It must never log prompts, terminal output, environment values,
host paths, runtime references or user identifiers. During key rotation, stop `terminal-manager`,
rotate the secret, restart it, then run the reaper and investigate every non-zero failure count.

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

## The plan catalogue is seeded explicitly (§2, §17)

`alembic upgrade head` creates the `plans` table but never its rows. The `free`/`pro`/`scale`
catalogue the entitlement resolver and the billing surface read is written by a **second explicit
step**, run after migrate and before the API/workers roll:

```
docker compose run --rm seed-plans      # python -m backend.app.cli.seed_plans
```

It is **never** run implicitly on startup — seeding is a deployment action with its own command, so
a running service never races a catalogue write and an operator decides when a pricing change takes
effect. Re-running is safe: the seed is idempotent (each tier keeps its `created_at` and reconciles
its entitlements), so the same command doubles as the **reconcile** step after any catalogue or
pricing change.

Pricing is deployment-configured, never baked into the image (§17). A paid tier's amount and its
provider-side checkout handle come from the environment — set them before this step:

- `JOBSEARCH_PLAN_PRICE_<SLUG>` — `"<amount_cents>:<currency>"`, e.g. `JOBSEARCH_PLAN_PRICE_PRO=1900:CHF`.
  A tier with no such variable is seeded **unpriced** (coherent, but not sellable until priced) —
  never an invented amount.
- `JOBSEARCH_PLAN_PRICE_ID_<SLUG>` — the opaque provider price handle a checkout needs, e.g.
  `JOBSEARCH_PLAN_PRICE_ID_PRO=price_…`. Absent → no checkout is offered for that tier.

See [.env.example](../.env.example) ("Plan pricing") for the block and [SaaS billing](./SAAS_BILLING.md)
for the catalogue's role. The dev composition's `seed-plans` command passes `--demo`, which seeds
non-production demo pricing instead of reading the environment; a production catalogue drops `--demo`.
The command exits `0` on a clean seed and `3` on a failure (nothing was written — investigate and
re-run; the seed is idempotent). Until the catalogue is seeded, the free tier is unresolved and the
billing surface has no plans to offer.

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
