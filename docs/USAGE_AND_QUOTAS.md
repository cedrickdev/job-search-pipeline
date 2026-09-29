# Usage and quotas (§3–9, §60–61)

This document is the contract for the middle and end of the Phase 16 spine: how a commercial
entitlement becomes a server-side quota check (§3–6), how a metered action is reserved race-safely
and then recorded as an authoritative usage event (§5–9), and how the frontend surfaces all of this
without ever holding paywall authority (§60–61). It sits downstream of
[SaaS billing](./SAAS_BILLING.md), which decides *which* plan applies, and upstream of nothing —
metering is the last link before the existing domain service runs.

The spine, end to end: **commercial entitlement → server-side quota check → existing domain service
→ authoritative usage event.** The quota check answers exactly one clause of the effective-permission
AND (§4) — it can refuse a metered action for lack of commercial room, and it can record that one
happened, and nothing else. Raising a plan's quota widens what an account may do commercially; it can
never weaken a safety brake.

## Design principles

- **The server is authoritative (§3).** Entitlement ceilings, the current billing window and the
  running total all live server-side and are read at the point of action. The frontend never defines
  a quota and never enforces one (§61) — it can only display what the server reports.
- **A closed set of keys (§3).** Everything metered is one of six `EntitlementKey`s; there is no
  open-ended "feature" string. A capability with no allowance on a plan resolves to `None` — the
  most restrictive answer — not to zero-that-might-mean-unlimited (§2).
- **Two shapes, never confused (§6).** A key is either a live `CONCURRENT` gauge or a `PER_PERIOD`
  meter, read from `entitlement_measure` so the metering service and the quota check can never
  disagree about which question a key asks.
- **Reserve under a lock, record after the fact (§5, §8).** `authorize` takes a transaction-scoped
  advisory lock and reads the period sum under it, so two workers racing the last unit cannot both
  see room. `record` writes one append-only `UsageEvent` for what was *actually* consumed, in the
  same unit of work.
- **The unknown is never fabricated (§5).** A run whose provider reported no token count meters
  nothing — never a fabricated `0`. A `UsageEvent` always stands for real, measured, positive
  consumption.
- **Idempotent metering (§7, §9).** Re-metering the same source collapses onto one ledger row by a
  derived idempotency key, so an at-least-once worker retry never double-counts.

## The entitlement keys (§3)

`EntitlementKey` is a closed set of six, each with a fixed `EntitlementMeasure`:

| Key | Measure | Meters |
| --- | --- | --- |
| `ACTIVE_SEARCH_PROFILES` | `CONCURRENT` | search profiles active *right now* |
| `LLM_TOKENS` | `PER_PERIOD` | tokens spent this billing window |
| `DOCUMENT_GENERATIONS` | `PER_PERIOD` | résumé/cover-letter renders this window |
| `APPLICATION_SUBMISSIONS` | `PER_PERIOD` | application submissions this window |
| `INTERVIEW_SESSIONS` | `PER_PERIOD` | interview simulator sessions this window |
| `RECOMMENDATION_GENERATIONS` | `PER_PERIOD` | career recommendations generated this window |

`ACTIVE_SEARCH_PROFILES` is the only `CONCURRENT` gauge; the other five are `PER_PERIOD` meters.

## The two measures (§6)

The measure decides how a quota is checked, because "how many profiles are active *right now*" and
"how many tokens have I spent *this window*" are different questions:

- **`CONCURRENT`** — checked against a count of currently-active resources. Creating one that would
  exceed the ceiling is refused; deleting one frees room again. It is a gauge, not a running total,
  and it never resets on a schedule. Checked with `authorize_concurrent` against a live count.
- **`PER_PERIOD`** — checked against the sum of usage events in the current billing window. It
  resets each period and is never freed within one. Checked with `authorize`, which sums the window
  under the lock.

## The billing window (§6)

The window a `PER_PERIOD` meter sums against is resolved per account as of an instant
(`backend/app/billing/entitlements.py`):

- A **paid, granting** subscription meters against its own `[current_period_start,
  current_period_end)`, labelled by its start date so two consecutive renewals never share a sum.
- Everything else — the free tier, a `PAST_DUE` or `CANCELED` subscription, no subscription at all —
  meters against the **calendar month** the instant falls in (§6).

The resolver is total: a granting subscription whose plan row has vanished falls back to free rather
than failing, and a deployment that has not seeded even the free plan raises
`PLAN_CATALOGUE_MISSING` — a loud refusal, never a silently invented entitlement.

## The metering spine (§5–9)

A metered domain action is book-ended by `MeteringService`
(`backend/app/billing/metering.py`), and everything runs in one unit of work:

1. **`authorize(user_id, key, quantity, as_of)` (§8).** Takes the transaction-scoped usage-budget
   advisory lock, resolves the account's effective plan and window, sums what the window has already
   consumed, and raises `BillingError(QUOTA_EXCEEDED)` when there is no room for `quantity` more.
   Returns the `ResolvedEntitlements` so the caller records against the same window's label. The
   lock is held from this check, through the action, to the commit — so a second worker of the same
   account blocks until the first commits and then sees its consumption. This mirrors the Phase 12
   submission-budget reservation exactly, in its own lock namespace.
2. **The existing domain service runs.** Every eligibility gate, policy check, approval and evidence
   guard executes here, unchanged. `authorize` answered one clause of the AND; the domain answers
   the rest. A safety brake that refuses here refuses regardless of how much quota was reserved.
3. **`record(...)` (§5, §7).** Writes one append-only `UsageEvent` for the measured quantity — and
   writes *nothing* for a non-positive or unknown (`None`) quantity. The event id derives from the
   source via `build_usage_idempotency_key`, so re-metering the same source (a retried worker,
   `UsageSourceType.LLM_RUN`/`DOCUMENT_VERSION`/`APPLICATION_SUBMISSION`/`INTERVIEW_SESSION`/
   `CAREER_RECOMMENDATION`) collapses onto one row.

### LLM tokens reuse existing telemetry (§5)

`LLM_TOKENS` is not a new counter. It sums the token counts the Phase 11 `LLMRun` recorder already
captures per run (`UsageSourceType.LLM_RUN`), so metering reads the same authoritative telemetry the
provider layer records — and when a provider reports no count, the run meters nothing rather than a
fabricated zero. Unknown stays unknown, end to end.

## Quota UX (§60)

`GET /billing/usage` returns, per key, the current window's ceiling and the running total, so a
surface can show how close a meter is to its limit and warn *before* an action is refused. The
snapshot is derived server-side from the same resolver the enforcement path uses, so what the user
sees and what the server enforces are the same number — never a client-side estimate.

When an action is refused, the server raises `QUOTA_EXCEEDED`; the surface explains the ceiling and
points at `/billing` to raise it. A refusal names the *commercial* limit, never a safety brake — a
denied application submission that was blocked by an `ApplicationPolicy` is a policy refusal, not a
quota one, and the two are never conflated.

## No paywall authority in the frontend (§61)

The frontend renders meters and draws upgrade prompts, but it **decides nothing**. It holds no plan
prices, no quota values and no entitlement rules — all of those are read from the server. A disabled
button is a hint; the enforceable answer is the `QUOTA_EXCEEDED` the server returns at the point of
action. This is what keeps a tampered client from ever granting itself capability: the ceiling lives
where the action is metered, not where it is drawn.

## Security and correctness callouts

- **Race-safe by lock, not by hope (§8).** The advisory lock is the reason a concurrency test can
  fire N simultaneous requests at the last unit of a quota and see exactly one succeed.
- **Idempotent by derived key (§7, §9).** An at-least-once worker retry re-records the same source
  and changes nothing — the ledger stays a truthful count of real consumption.
- **Owner-scoped (§3).** Every quota check and every usage read is scoped to the acting account; a
  usage event of one user is never visible to, or countable against, another.
- **A brake is never widened (§2, §4).** Entitlement can only restrict. No plan, however expensive,
  turns off an eligibility gate, a human approval, a truth/evidence guard or a CAPTCHA/MFA wall.

