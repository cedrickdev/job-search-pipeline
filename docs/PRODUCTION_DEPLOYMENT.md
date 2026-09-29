# Production deployment (§53–58)

This document is the operator's contract for running the V2 platform in production: how a
deployment declares itself (§53), what configuration each feature requires, how the session cookie
is secured (§54), how secrets reach a container (§55), the target service composition (§56), the
image model (§57), and why migrations stay an explicit step (§58).

Development is forgiving by design — an unset database URL becomes a loopback dev database, an unset
Redis URL becomes the local dev Redis, an unset public origin becomes `http://localhost`. That is
exactly what a developer wants and exactly what a production deployment must never get. So a
deployment **declares itself** and the application **fails closed** rather than ever falling back to
a development credential.

## Design principles

- **Fail closed, never fall back (§53).** In production the app refuses to start until critical
  configuration is present and is not a development default. It never silently uses a dev
  credential — that is how a live service ends up writing to a throwaway database or minting cookies
  a browser will not return over HTTPS.
- **One deployment, one fix.** `validate_production_readiness` collects *every* problem and raises
  once, so an operator corrects one deployment rather than rediscovering the next missing secret on
  the next boot.
- **Names, never values (§41).** A misconfiguration message names only the offending environment
  variable — never its value — so nothing secret (not even a dev default an operator pasted into the
  wrong slot) is ever echoed into a log or a crash report.
- **Conditional on the feature (§53).** Billing secrets are required only when billing is enabled;
  the credential-encryption master key only when that feature is enabled. The always-on essentials —
  a real database, a real Redis, an https origin, a Secure cookie — are required unconditionally.
- **Secrets never enter an image (§55).** Configuration reaches a container through the environment
  or the orchestrator's secret store; no `.env` and no secret is ever baked into an image layer.

## Deployment mode

`JOBSEARCH_ENV` selects the posture. Unset (or `development`) is the forgiving default; `production`
is fail-closed. An unrecognised value fails closed rather than guessing.

```
JOBSEARCH_ENV=production
```

The gate runs at process start: `server.app.create_app` calls `validate_production_readiness()` as
its first act, and so does the worker entrypoint (`python -m backend.app.cli.run_worker`). Outside
production mode the call returns immediately, so development and the whole test suite are never
gated. In production, a missing essential raises `ProductionConfigError` before a single resource is
built — the process never binds a port on a broken configuration.

## Required configuration

The essentials are validated unconditionally; the rest only when their feature is switched on.

| Variable | When required | Rejected value |
| --- | --- | --- |
| `JOBSEARCH_DATABASE_URL` | always | unset, or the local dev database URL |
| `JOBSEARCH_REDIS_URL` | always | unset, or the local dev Redis URL |
| `JOBSEARCH_PUBLIC_BASE_URL` | always | unset, the dev origin, or a non-`https://` origin |
| `JOBSEARCH_AUTH_COOKIE_SECURE` | always | an explicit false-y value (§54) |
| `JOBSEARCH_STRIPE_SECRET_KEY` | `JOBSEARCH_BILLING_ENABLED=true` | unset |
| `JOBSEARCH_STRIPE_WEBHOOK_SECRET` | `JOBSEARCH_BILLING_ENABLED=true` | unset |
| `JOBSEARCH_LLM_SECRET_KEY` | `JOBSEARCH_LLM_CREDENTIAL_ENCRYPTION_ENABLED=true` | unset |

`JOBSEARCH_REDIS_URL` resolves most-specific-first (`JOBSEARCH_REDIS_URL` then `REDIS_URL`), the same
order the task queue and the rate limiter use, so validation asks "is a real Redis configured?" the
same way the app reads it. `.env.example` documents every name with its dev default.

## The session cookie (§54)

`JOBSEARCH_AUTH_COOKIE_SECURE` defaults to `true` and **cookie security is never inferred from the
request scheme**. Production keeps the default; turning it off is a startup problem. Only a local
composition reached over `http://localhost` sets it false (see `docker-compose.yml`'s `api`), and it
is set there explicitly, knowingly, and never in a deployment a browser reaches over HTTPS. Because
the origin must be `https://`, the `Secure` cookie the app mints is one the browser will actually
return. See `docs/AUTHENTICATION.md` for the cookie's full policy.

## Secret handling (§55)

Secrets reach a container through its environment or the orchestrator's secret store — a Kubernetes
`Secret`, an ECS task-definition secret, a Compose `secrets:` entry backed by a file the repo never
holds. Nothing is ever baked into an image layer. `.dockerignore` excludes `.env`, `.env.*`, `data/`
and `cv/` from every build context, so a `COPY . .` cannot capture one, and no compose service mounts
`.env` or passes it through — Compose only interpolates it into non-secret values (ports, the dev
password). This file and `.env.example` document variable *names* and dev defaults only; real secrets
stay out of the repository.

## Service composition (§56)

`docker-compose.yml` is the development composition and the shape a production orchestrator mirrors.
The long-running services are the database, Redis, the API and (in production) the two workers; the
one-shot jobs run in the same image and environment as the application.

| Service | Role | Profile |
| --- | --- | --- |
| `postgres` | PostgreSQL 17 + PostGIS, the persistence target | — (core) |
| `redis` | the rate limiter's counters and the task subsystem's Redis (§34, §49) | — (core) |
| `api` | the FastAPI application (JSON API + built SPA) | — (core) |
| `frontend` | the Nuxt dev server (dev only; production is the SPA the API serves) | — (core) |
| `worker` | the general lane: discovery, export, retention (§34-35) | `workers` |
| `browser-worker` | the browser lane: Playwright submissions only (§35) | `workers` |
| `migrate` | `alembic upgrade head`, the explicit schema step (§58) | `tools` |
| `import-v1` | the V1 SQLite importer | `tools` |
| `backup` | write a PostgreSQL dump (§45) | `tools` |
| `restore-check` | restore-drill the newest dump into a scratch DB (§46) | `tools` |

Two profiles keep dev ergonomics: `docker compose up` starts db + Redis + API + frontend;
`docker compose --profile workers up` adds the workers; and the `tools` jobs run one-shot via
`docker compose run --rm <job>`, so starting the stack never migrates, backs up or imports as a side
effect. The two lanes are **separate containers** so a wedged browser submission can never consume a
general-lane worker (§35). Generated artifacts (exports, document renders, dumps) live on a shared
`app_var` volume so a file the worker writes is the file the API streams back.

## Image model (§57)

Three images, each carrying only what its job needs:

- **`docker/backend.Dockerfile`, `runtime` stage** — the default target for the API, the general
  worker, `migrate` and `import-v1`. WeasyPrint's shared libraries and nothing else: no database
  client, no browsers. Non-root (uid 10001).
- **`docker/backend.Dockerfile`, `tools` stage** — a superset built *only* by `backup` and
  `restore-check`, adding the PostgreSQL 17 client (`pg_dump`/`pg_restore`/`psql`) that matches the
  server. A `pg_dump` binary is therefore never shipped in the request-serving image.
- **`docker/browser-worker.Dockerfile`** — Microsoft's Playwright image plus the app, for the
  browser lane only. Browsers are large and belong solely on the lane that drives them; `playwright
  install chromium` re-fetches the browser matching the pinned `playwright` version. Non-root
  (`pwuser`).

All three are non-root, install runtime dependencies only (no pytest, no linters), and carry no
source secret. Health checks are defined in the composition rather than baked into the image, so the
same image serves under any orchestrator.

## Migrations stay explicit (§58)

The schema is **never** migrated on API or worker startup. A service that migrates on boot migrates
on every scale-up and every restart loop — a race and a surprise. `alembic upgrade head` is a
deliberate deployment step: `docker compose run --rm migrate`, or the equivalent one-shot Job in the
target orchestrator, run once before rolling the new API and workers. `/api/v2` answers `503` until
it has been run against a fresh database.

## Backup and recovery

`backup` and `restore-check` wrap `python -m backend.app.cli.run_backup` (`create` / `verify`); a
`verify` that exits `3` (NOT VERIFIED) is the signal a monitored recovery drill watches for. The
password reaches the libpq child through its environment, never a command line or any output. See
`docs/BACKUP_RECOVERY.md` for the full backup contract and `docs/OBSERVABILITY.md` for the health,
metrics and alerting surface a production deployment monitors.

