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
| `ANTHROPIC_API_KEY` + `JOBSEARCH_TERMINAL_MANAGER_TOKEN` | `JOBSEARCH_TERMINAL_ENABLED=true`, on **terminal-manager only** | unset |
| `JOBSEARCH_TERMINAL_ALLOWED_ORIGINS` | `JOBSEARCH_TERMINAL_ENABLED=true`, on the public API | unset, wildcard or non-origin URL |

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
password). The isolated terminal manager is the deliberate exception: it reads a dedicated ignored
`terminal.env` containing only its Claude gateway values, never the application's `.env`. This file,
`.env.example` and `terminal.env.example` document variable *names* and dev defaults only; real
secrets stay out of the repository.

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
| `browser-worker` | the browser lane: committed application preparation and guarded Playwright submissions (§35) | `workers` |
| `browser-tests` | mandatory local Chromium fixture checks; no live ATS | `tools` |
| `migrate` | `alembic upgrade head`, the explicit schema step (§58) | `tools` |
| `import-v1` | the V1 SQLite importer | `tools` |
| `backup` | write a PostgreSQL dump (§45) | `tools` |
| `restore-check` | restore-drill the newest dump into a scratch DB (§46) | `tools` |
| `terminal-manager` | private owner of the Docker socket and the deployment Claude key; starts isolated per-session runtimes | `terminal` |

Two profiles keep dev ergonomics: `docker compose up` starts db + Redis + API + frontend;
`docker compose --profile workers up` adds the workers; and the `tools` jobs run one-shot via
`docker compose run --rm <job>`, so starting the stack never migrates, backs up or imports as a side
effect. The two lanes are **separate containers** so a wedged browser submission can never consume a
general-lane worker (§35). Generated artifacts (exports, document renders, dumps) live on a shared
`app_var` volume so a file the worker writes is the file the API streams back.
The browser worker also mounts the API's `api_data` volume for the shared V1/V2 browser lock and
private, account-scoped V2 Chromium profiles, and `app_var` to read the exact rendered document versions it
uploads. Both images use uid/gid 10001 so these volumes remain writable without root.

### Integrated Claude terminal

The terminal feature is opt-in. Build the short-lived runtime image and start the
private manager profile only after placing the manager token and Anthropic key in the
deployment's secret store:

```bash
docker compose build claude-terminal-runtime terminal-manager
docker compose --profile terminal up -d terminal-manager
```

The public API must receive `JOBSEARCH_TERMINAL_ENABLED=true`, its manager URL/token,
and the exact browser origin allowlist. It must never receive `ANTHROPIC_API_KEY` or a
Docker socket. `terminal-manager` exposes no host port; only it mounts the Docker
socket. Its per-user/session runtime is non-root, read-only where possible, has no
Docker socket or incoming port, and runs only on the dedicated terminal egress network.
The VPS firewall must restrict that network's outbound traffic to Anthropic endpoints;
Compose network separation alone is not a domain allowlist. Stop the manager to halt
new sessions; use the terminal reaper to clean up active runtimes after an emergency
key rotation.

## Image model (§57)

Three images, each carrying only what its job needs:

- **`docker/backend.Dockerfile`, `runtime` stage** — the default target for the API, the general
  worker, `migrate` and `import-v1`. WeasyPrint's shared libraries and nothing else: no database
  client, no browsers. Non-root (uid 10001).
- **`docker/backend.Dockerfile`, `tools` stage** — a superset built *only* by `backup` and
  `restore-check`, adding the PostgreSQL 17 client (`pg_dump`/`pg_restore`/`psql`) that matches the
  server. A `pg_dump` binary is therefore never shipped in the request-serving image.
- **`docker/browser-worker.Dockerfile`** — Python Bookworm with the version-matched
  Playwright Chromium and system dependencies, for the browser lane only. The
  `browser-tests` target adds pytest for local fixture checks. Non-root (uid 10001).

All three are non-root, install runtime dependencies only (no pytest, no linters), and carry no
source secret. Health checks are defined in the composition rather than baked into the image, so the
same image serves under any orchestrator.

## Phase 12.1 browser activation boundary

For a complete local workflow, set `JOBSEARCH_BROWSER_LOCAL_PREPARATION_ENABLED=true`
on the host running the API. This enables only the reversible `PREPARE` step through
the isolated Playwright subprocess, so the UI can inspect a supported ATS form and
resolve its typed questions. It does not authorize a submission.

`JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED` defaults to `false` in settings, the
example environment and Compose. A disabled switch refuses new queue requests and
the worker checks it again before sending. The HTTP `/submit` route cannot bypass
the committed reservation for an approved browser application. Only CH opportunities
with an active supervised/autopilot policy can enter that lane; unknown forms,
authentication challenges and ambiguous confirmation require human review. Do not
turn the switch on solely because local fixtures pass: real ATS hosts, policy-pause
race handling and an operational security review remain unverified. To halt new
browser sends, keep or return the switch to `false` on API and browser-worker and
restart those services; investigate any in-flight or unknown attempts before
considering another send. Apply migration `rev_0021` before deploying this worker;
rollback requires stopping the worker first and reconciling any rows with the new
reason values before downgrading the database.

The supported local operator sequence is:

```bash
export JOBSEARCH_BROWSER_LOCAL_PREPARATION_ENABLED=true
export JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED=false
./start.sh

# In another terminal, drain queued browser tasks without sending them.
.venv/bin/python -m backend.app.cli.run_worker --lane browser --once
```

The fixture-only Playwright suite remains the safe proof of browser behavior:
`docker compose --profile tools run --rm browser-tests`. Real Swiss submissions
require a separate source-access review, an exact active Greenhouse or Lever form
capability in `JOBSEARCH_ATS_APPROVED_FORMS_PATH`, the explicit live switch, an
active CH policy and the daily/period quota. The packaged form catalog is empty.
Ashby support as a browser driver does not confer unattended authority. The
pause-versus-final-click race remains an activation blocker; leave the live switch
off until that boundary and each target employer's conditions are reviewed.
LinkedIn and jobs.ch remain unsupported for this path.

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
