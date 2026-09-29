# Rate limiting (§49–52)

This document is the operator's contract for how the V2 API rate-limits its abuse-prone endpoints:
the fixed-window algorithm (§49), the identity a request is counted by (§51), the safe 429 it
answers with (§52), and the property that makes all of it *additive* — it can only ever add
refusals on top of the existing authentication protections, never remove one (§50).

The layer is **store-neutral above the seam**. The whole thing is one algorithm expressed once and
implemented twice: against Redis in production (horizontal-safe across web workers) and in memory
for tests and local dev. Nothing upstream of the `RateLimiter` protocol knows which store it holds.

## Design principles

- **Additive, never a replacement (§50).** Rate limiting sits *beside* the Phase 4 authentication
  protections (Argon2 hashing, enumeration-resistant login, the DB-backed temporary account
  lockout, server sessions, CSRF, `Secure` cookies, absolute session expiry). It removes none of
  them. The limiter can only turn an allowed request into a refused one, never a refused one into
  an allowed one — which is exactly what lets it fail *open* (below) without reopening a protection.
- **Enforced as a dependency, never as a body-buffering middleware.** The limit is a FastAPI
  dependency the route names, so it runs after auth resolution and before the handler, and it never
  reads the request body — which is what keeps it safe in front of the chat SSE stream. A middleware
  that buffered the body to inspect it would break streaming.
- **The limiter is a leaf; the store is an adapter.** The rule, the category, the decision and the
  algorithm live in `backend/app/ratelimit/limiter.py` and know nothing about HTTP or Redis. The
  429 lives in the API layer. The Redis client stays behind the `RateLimiter` protocol in
  `backend/app/infrastructure/ratelimit`, imported lazily, so the default test suite never imports
  `redis` (the same discipline the task queue's `RedisNotifyingDispatcher` follows).
- **Server-authoritative windows.** Every window is sized by `RateLimitSettings` in
  `backend/app/core/settings.py` from environment variables with safe built-in defaults. The
  frontend never defines a limit.

## The fixed-window algorithm (§49)

A request is counted into a bucket keyed `namespace:identity:window_start`, where

```
window_start = epoch − (epoch mod window_seconds)
```

snaps the clock down to the start of the current window. Each `check` increments the bucket's count
and compares it to the rule:

- the event that takes the count to exactly `max_events` is **allowed**;
- the next event (`max_events + 1`) is **refused**;
- a refusal reports `retry_after = max(1, window_start + window_seconds − epoch)` — the seconds
  until this window ends, floored at 1 so a client is never told to retry immediately into the same
  closed window.

Keying the bucket on `window_start` is what makes the counter safe to share across processes. In
production the bucket is a Redis key `ratelimit:<namespace>:<identity>:<window_start>`; `INCR`
returns one running total no matter which web worker a request lands on (§49), and an `EXPIRE` set
**only on the first event** of a window (never refreshed) lets the key — and the count — reset
itself when the window ends. Setting the TTL only once is deliberate: refreshing it on every
increment would turn a fixed window into a rolling one that never resets.

The in-memory limiter (`InMemoryRateLimiter`) mirrors this exactly — same key, same snap — and
prunes expired windows on every check so a long-lived dev process cannot accumulate a bucket per
elapsed window forever. It is single-process by construction, which is precisely why production
uses Redis.

## Rate-limit identity (§51)

Who a request is counted as depends on whether it is authenticated:

- **Authenticated endpoints** are counted by **user id** (`limit_by_user`). One account's exhausted
  bucket never spills onto another's, and the count follows the user regardless of their IP.
- **Anonymous endpoints** (register, login) are counted by **network identity** (`limit_by_ip`) —
  there is no user yet.

The network identity is resolved by `client_ip` with an explicit **proxy-trust** setting:

- With `trusted_proxy_count = 0` (**the default**), the `X-Forwarded-For` header is **not trusted
  at all** and the socket peer is the identity. A forwarded header is otherwise attacker-controlled:
  trusting it by default would let a client mint a fresh bucket per request by varying the header,
  defeating the limit.
- With `trusted_proxy_count = N > 0`, the real client is read as the **Nth-from-last** entry of
  `X-Forwarded-For` — the entry the outermost trusted proxy appended. Set this to the exact number
  of proxies that append to the header (e.g. `1` behind a single load balancer). If the header is
  shorter than `N` or absent, `client_ip` falls back to the socket peer.

A spent window is **never a permanent ban** (§51): it reopens the moment the clock passes
`window_start + window_seconds`. IP is used only to *count within a window*, never to blacklist.

## Categories and default windows

Each category is its own settings field **and** its own Redis key namespace, so exhausting one
never locks out another (exhausting document generation must not block sign-in). Application
submission is deliberately **absent**: it already carries Phase 12 `ApplicationPolicy` limits, and
§49 says to preserve those rather than stack a second, coarser brake over them.

| Category | Identity | Default window | Env prefix |
| --- | --- | --- | --- |
| `register` | IP | 20 / hour | `JOBSEARCH_RATE_LIMIT_REGISTER_*` |
| `login` | IP | 60 / 5 min | `JOBSEARCH_RATE_LIMIT_LOGIN_*` |
| `reauth` | user | 10 / hour | `JOBSEARCH_RATE_LIMIT_REAUTH_*` |
| `chat` | user | 30 / min | `JOBSEARCH_RATE_LIMIT_CHAT_*` |
| `document` | user | 20 / hour | `JOBSEARCH_RATE_LIMIT_DOCUMENT_*` |
| `export` | user | 5 / hour | `JOBSEARCH_RATE_LIMIT_EXPORT_*` |
| `checkout` | user | 10 / hour | `JOBSEARCH_RATE_LIMIT_CHECKOUT_*` |

`reauth` covers the re-auth-sensitive account deletion; `login` and `register` guard the anonymous
auth endpoints; `chat`, `document`, `export` and `checkout` guard the per-user LLM, document,
export and billing-checkout surfaces. The billing **webhook** is *not* limited: it is session-less
and provider-driven (§64), and a limit on it would drop legitimate provider retries.

## Safe 429 behaviour (§52)

A refusal is a typed `RateLimited` error the API renders as:

```
HTTP/1.1 429 Too Many Requests
Retry-After: <seconds>

{"error": "rate_limited",
 "detail": "too many requests; please retry after a short wait",
 "retry_after": <seconds>}
```

The `Retry-After` header and the `retry_after` body field carry the same integer. The body is **one
fixed sentence** for every category and every identity: it never names the email, the account, the
category, or which rule tripped, so a 429 leaks nothing an attacker could enumerate with — not
whether an email exists, not another account's subscription internals, nothing.
## Fail-open on a Redis fault (§50)

If Redis is unreachable, the limiter **allows** the request and logs a warning naming only the
error *type* — never the URL (it can carry a password) and never the identity being counted (§41).
A rate-store availability blip must never become an outage of sign-in or generation.

This is safe precisely because rate limiting is additive: the Phase 4 DB-backed account lockout
still bounds password guessing per account with **no Redis in the path**, so a fail-open limiter
cannot reopen the protection §50 says to keep.

## Configuration

Every variable is optional; the defaults protect a deployment with no configuration. See
`.env.example` for the annotated block.

| Variable | Default | Meaning |
| --- | --- | --- |
| `JOBSEARCH_RATE_LIMIT_ENABLED` | `true` | Master switch. False-y turns the limiter off entirely (it is never consulted). |
| `JOBSEARCH_REDIS_URL` / `REDIS_URL` | local dev URL | The Redis the window counters live in, shared with the task queue. `JOBSEARCH_REDIS_URL` wins. Treated as a secret — kept off every `repr` and every log. |
| `JOBSEARCH_RATE_LIMIT_TRUSTED_PROXY_COUNT` | `0` | Reverse proxies trusted in front of the app; `0` means `X-Forwarded-For` is ignored. Bounded `0..16`. |
| `JOBSEARCH_RATE_LIMIT_<CATEGORY>_MAX_EVENTS` | per table | Events allowed per window for that category (`≥ 1`). |
| `JOBSEARCH_RATE_LIMIT_<CATEGORY>_WINDOW_SECONDS` | per table | Window length in seconds for that category (`≥ 1`). |

Disabling the limiter (`JOBSEARCH_RATE_LIMIT_ENABLED=false`, or the test default) keeps every rule
*valid* — the switch is checked in the dependency, so a disabled limiter is simply never consulted
and no Redis connection is opened.

## Testing

The default test harness runs with rate limiting **disabled** (`RateLimitSettings.disabled()`) and
an `InMemoryRateLimiter`, so the suite's many repeated calls never spuriously 429. Tests that
exercise the limit enable a single tiny window explicitly and drive the shared in-memory limiter
with a hand-moved clock — no Redis, no live network, per the project's testing rule.
`tests/test_v2_rate_limiting.py` covers the algorithm, the identity resolution, the settings, the
429 contract, per-identity and per-category isolation, window reopening, the ignored forwarded
header, the unlimited webhook, and the §50 property that a generous login limit still lets the DB
lockout fire first.
