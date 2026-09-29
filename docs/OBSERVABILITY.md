# Observability (§41–44)

This document is the operator's contract for the V2 API's observability surface: the structured
logs it emits (§41), the Prometheus metrics it exposes (§42), the health probes a scheduler reads
(§43), and the alert conditions a monitoring system should watch (§44).

It is deliberately **vendor-neutral**. Nothing here requires a proprietary APM or alerting product:
the logs are line-delimited JSON any aggregator can index, the metrics are the Prometheus 0.0.4 text
exposition any scraper understands, and the alert rules below are plain PromQL. Swapping Grafana for
another dashboard, or Alertmanager for another router, changes none of the application code.

## Design principles

- **No new dependencies.** The metrics layer (`backend/app/observability/metrics.py`) renders the
  exposition format itself — there is no `prometheus_client`, and logging uses the stdlib, not
  `structlog`. One less supply-chain surface, and the exact output is under test.
- **Per-application registry, never a process global.** `install_observability` builds one
  `AppMetrics` bundle and stores it on `app.state`, so two `create_app()`s in one test process keep
  independent counts. Metrics are instance state, not a module singleton.
- **Derivation over bespoke counters.** Anything that can be read off an existing series is not
  given its own. Quota denials and billing-webhook failures are cells of `http_requests_total`
  (see *Alerting* below), and LLM health reuses the Phase 11 telemetry table — a view, not a copy.
- **No high-cardinality labels.** Every label key is declared once in `AppMetrics`; a sample that
  supplies a different set is a programming error the registry refuses at write time. HTTP series
  are labelled by the *route template* (`/items/{item_id}`), never the raw path, and never by a user
  id, opportunity id, or correlation id. A scanner probing a thousand bad paths collapses onto one
  `route="__unmatched__"` bucket rather than minting a thousand series.

## Endpoints

`install_observability` mounts three routes at the **root**, not under `/api/v2` — they are platform
plumbing, not product API, and carry no session cookie and no CSRF token.

| Route | Purpose | Touches |
| --- | --- | --- |
| `GET /health/live` | Liveness: is the process up? | Nothing external (§43) |
| `GET /health/ready` | Readiness: can it serve? | The database only (§43) |
| `GET /metrics` | Prometheus text exposition | The database at scrape time (§42) |

> **Security — network-restrict these routes.** They are unauthenticated by design (a Kubernetes
> probe and a Prometheus scraper have no cookie) and expose only aggregate, non-identifying numbers
> plus a fixed liveness string — never a user's data and never a secret. A deployment **must** fence
> them at the ingress / network layer so only the scheduler and the monitoring system reach them.
> Nothing here is safe to expose to the public internet.

### Liveness vs readiness (§43)

These are **distinct** on purpose:

- **`/health/live`** answers *"is this process up?"* and touches no dependency — no database, no LLM
  provider, no job board. It is what a liveness probe reads to decide whether to **restart** the
  container, so making it depend on anything external would turn a transient database blip into a
  restart storm. It always returns `200 {"status": "alive"}`.
- **`/health/ready`** answers *"can this process serve?"* and checks the one dependency an API
  request genuinely needs: the database (a read-only `SELECT 1`). It returns
  `200 {"status": "ready", ...}` when the database answers and `503 {"status": "unavailable", ...}`
  when it does not, so a load balancer stops routing to a pod that cannot reach its store.
- A **third-party outage is deliberately not a readiness failure.** An LLM provider or a job board
  being down degrades *that capability*, not the whole API, so readiness ignores them. Their health
  is visible through metrics (`llm_runs{status="FAILED"}`, task lanes), not through the probe that
  gates traffic.

## Metric set (§42)

Two families, one registry. **Push** metrics are written by the ASGI middleware on every request;
**pull** gauges are refreshed by `MetricsCollector` at scrape time from the durable tables.

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `http_requests_total` | counter | `method`, `route`, `status` | Requests handled, by route template and response status |
| `http_request_duration_seconds` | histogram | `method`, `route` | Request latency; buckets + `_sum`/`_count` |
| `http_requests_in_flight` | gauge | — | Requests currently being handled |
| `db_up` | gauge | — | `1` if the database answered the last scrape's ping, else `0` |
| `task_runs` | gauge | `lane`, `status` | Background task runs in each state, per lane |
| `task_stale_leases` | gauge | — | RUNNING tasks whose worker lease has lapsed (worker-health) |
| `llm_runs` | gauge | `status` | LLM telemetry runs by terminal / in-flight status |

`/metrics` **stays up even when the database is down**: it reports `db_up 0` and keeps the last-known
pull gauges rather than returning 500, because the alert that fires on a database outage reads this
very endpoint (§44). The in-memory HTTP series are always rendered.

## Structured logging (§41)

Production logs are consumed by machines before people: a scraper ships them to an aggregator that
indexes by field, so each log line is **one flat JSON object**, not a sentence
(`backend/app/observability/logging.py`). Every line carries a closed base — `timestamp` (ISO-8601
UTC), `level`, `logger`, `message` — plus the `correlation_id` in flight and any structured extras
the caller attached. An exception is rendered to an `error` / `error_message` / `stack` triple, so a
traceback never breaks one-object-per-line parsing.

**A secret never reaches the log.** Two defences enforce §41's non-negotiable rule:

1. The formatter only ever serialises the closed base plus explicitly-attached extras — a request or
   response body, a CV, an answer, a cookie is **never** one of them, so they cannot ride along by
   default.
2. Every emitted value passes through a redactor keyed on the *name* of the field. A field whose
   lower-cased, hyphen-normalised name contains a secret marker — `password`, `secret`, `token`,
   `cookie`, `api_key`, `authorization`, `session`, `csrf`, `credential`, `ciphertext`,
   `private_key`, `signature`, `webhook_secret` — is replaced by `[REDACTED]`, at every nesting
   depth. So `X-Api-Key`, `db_password`, and a `password` two levels down in a dict all redact.

This means **no CV contents, passwords, API keys, cookies, session/CSRF tokens, browser
credentials, application answers, or billing webhook secrets are ever logged**, even when a caller
attaches a dict without thinking. `configure_logging` installs the formatter on the root logger
idempotently, so a reload or a second `create_app` never doubles every line.

### Correlation id

The middleware binds a correlation id to a `ContextVar` for the lifetime of each request, and the
formatter attaches it to every log line emitted while it is in flight. An inbound
`X-Correlation-ID` or `X-Request-ID` header is honoured (decoded defensively, capped at 128 chars);
otherwise a fresh id is minted. It is echoed back on the response as `X-Correlation-ID`, so a client
error report and the server logs share one handle.

## Alerting (§44)

The conditions below are expressed in plain PromQL. They assume the scraper reaches `/metrics`
(see the network-restriction note above) and that a router such as Alertmanager — or any equivalent
— delivers the notifications. No proprietary vendor is required.

### Database down

```promql
# The primary database did not answer the last scrape. `/metrics` stays up and reports db_up 0,
# which is exactly why this alert can be evaluated during the outage.
db_up == 0
```

### Worker / queue health

```promql
# Workers are dying mid-task: RUNNING leases are lapsing without being renewed or completed.
task_stale_leases > 0

# The dead-letter cell is growing — tasks have exhausted their retries.
sum(task_runs{status="DEAD_LETTERED"}) > 0

# A lane is backing up: queued work is accumulating faster than it drains.
sum by (lane) (task_runs{status="QUEUED"}) > 100
```

### LLM provider degradation

```promql
# A provider failure spike, read off the reused Phase 11 telemetry — not a job-board/LLM readiness
# failure (those degrade a capability, not the whole API), but a signal worth paging on.
sum(rate(llm_runs{status=~"FAILED|TIMEOUT"}[5m])) > 0
```

### HTTP error rate and latency

```promql
# Server errors as a fraction of all requests over 5 minutes.
sum(rate(http_requests_total{status=~"5.."}[5m]))
  / sum(rate(http_requests_total[5m])) > 0.05

# p95 request latency, by route template.
histogram_quantile(
  0.95, sum by (le, route) (rate(http_request_duration_seconds_bucket[5m]))) > 1
```

### Derived series — no bespoke counter

§42 calls for "quota denials" and "billing webhook failures" as observable conditions. Neither gets
its own metric: both fall out of `http_requests_total`, so there is one source of truth, not two.

```promql
# Quota denials: the API returns 402 when a commercial quota is exhausted, so a denial is simply a
# 402 cell. A rising rate means users are hitting their plan limits.
sum(rate(http_requests_total{status="402"}[5m])) > 0

# Billing webhook failures: the provider webhook is the one write route, at
# POST /api/v2/billing/webhook. A non-2xx there means we rejected (bad signature) or failed to apply
# a provider event — subscriptions may be drifting from the provider's truth.
sum(rate(
  http_requests_total{route="/api/v2/billing/webhook", status!~"2.."}[5m])) > 0
```

## Where the pieces live

- `backend/app/observability/logging.py` — the JSON formatter, redactor, and `configure_logging`.
- `backend/app/observability/metrics.py` — the dependency-free registry and exposition renderer.
- `backend/app/observability/app_metrics.py` — `AppMetrics`, the closed declared metric set.
- `backend/app/observability/middleware.py` — the pure-ASGI correlation-id + HTTP-metrics middleware.
- `backend/app/observability/collectors.py` — `MetricsCollector`, the scrape-time pull-gauge refresh.
- `backend/app/observability/health.py` — the `/health/live`, `/health/ready`, `/metrics` routes.
- `backend/app/observability/__init__.py` — `install_observability`, the single wiring call.



