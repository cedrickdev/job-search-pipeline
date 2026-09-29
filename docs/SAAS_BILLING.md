# SaaS billing (§10–18)

This document is the contract for how the V2 platform sells subscriptions: the provider-neutral
boundary a billing adapter implements (§10–11), how an inbound webhook is trusted and applied
exactly once (§12–14), the subscription lifecycle and its capability-only downgrade (§15–16), the
billing API (§17), and the `/billing` frontend (§18). It is the first link of the Phase 16 spine —
a commercial entitlement — and it feeds [usage and quotas](./USAGE_AND_QUOTAS.md), never the other
way round.

The one invariant this whole subsystem serves: **billing decides what an account *may* do
commercially, and nothing else.** A paid plan can raise a commercial ceiling; it can never weaken a
safety brake — an `ApplicationPolicy`, an eligibility verdict, a human approval, a truth or evidence
guard, a CAPTCHA/MFA wall. Effective permission is `domain permission AND user policy AND commercial
entitlement`, an AND that entitlement can only *restrict*.

## Design principles

- **Provider-neutral by construction (§10).** The webhook service and the billing API hold a
  `BillingProvider` port (`backend/app/billing/provider.py`), never a Stripe client. Every provider
  specific — the signature scheme, the JSON shape, the status vocabulary, the HTTP surface — stays
  inside the adapter (`backend/app/billing/stripe_provider.py`). Swapping providers is wiring a
  different adapter at bootstrap, exactly as the LLM registry is wired; the domain never changes.
- **The provider proposes, the platform records (§12).** A checkout or a portal action happens on
  the provider's hosted page; the platform only mints the redirect link and later *learns* the
  result as a webhook. The database — a subscription row and an append-only webhook-event ledger —
  is the source of truth, not any live call back to the provider.
- **Verify before you trust a byte (§12–13).** `verify_webhook` checks the signature and the
  timestamp's freshness against an injected `now` *before* parsing the body. An unverified payload
  is never normalized, never applied, never logged.
- **Exactly once, and never out of order (§14).** Each verified event is recorded under its
  provider event id; a replay collapses onto the same ledger row and changes nothing. A stale event
  (one describing a subscription state older than the row already holds) is acknowledged and
  ignored, so redelivery and out-of-order delivery are both safe.
- **Downgrade is capability-only (§16).** Losing a paid subscription lowers ceilings back to the
  free tier — it never deletes data and never touches a safety brake. Access is *derived* from the
  subscription's state as of an instant, not stored as a grant that must be revoked.
- **Names and states, never secrets (§41).** No provider payload, signing secret, customer id or
  card detail is ever logged or returned to a client. Errors name a stable `BillingErrorCode`, not a
  provider message.

## The provider boundary (§10–11)

`BillingProvider` (`backend/app/billing/provider.py`, `@runtime_checkable`) is a three-method port:

| Method | Direction | Contract |
| --- | --- | --- |
| `provider_key` | — | the stable key stamped on every subscription and event this adapter owns (e.g. `stripe`) |
| `verify_webhook(payload, headers, now)` | in | verify the signature and freshness, then normalize to a `NormalizedWebhookEvent`; raise before trusting the body |
| `open_checkout(plan, client_user_id, success_url, cancel_url, customer_id?)` | out | mint a provider-hosted checkout, returning a `CheckoutSession` redirect |
| `open_portal(customer_id, return_url)` | out | mint the provider's billing portal link, returning a `PortalSession` redirect |

No method reads a clock — `verify_webhook` takes the `now` it checks a signature's freshness
against — the convention every V2 boundary keeps, so a test injects the instant instead of patching
time. `open_checkout` attaches `client_user_id` so the first webhook can be attributed to the
account, and reuses a known `customer_id` so a returning subscriber is never duplicated
provider-side. A failed call out raises `BillingError(PROVIDER_UNAVAILABLE)`; the platform never
strands the caller on a raw provider exception.

The `internal` provider (`INTERNAL_BILLING_PROVIDER = "internal"`) is the no-network default: it
satisfies the same port for development and the test suite, so nothing in the default configuration
contacts Stripe. Enabling real billing is setting `JOBSEARCH_BILLING_ENABLED=true` and supplying the
two Stripe secrets (`docs/PRODUCTION_DEPLOYMENT.md` §53); with billing off, the plan catalogue still
resolves and every account simply sits on the free tier.

## The plan catalogue (§15)

Plans are server-authoritative and seeded from `backend/app/billing/catalogue.py` — the frontend
never defines a price, a quota or an entitlement (§61). Three plans ship:

| Slug | Name | Price | Interval |
| --- | --- | --- | --- |
| `free` | Free | — | — |
| `pro` | Pro | 1900 ¢ / mo | `MONTHLY` |
| `scale` | Scale | 4900 ¢ / mo | `MONTHLY` |

`BillingInterval` is `MONTHLY` or `YEARLY`. Each plan carries the `Entitlement` ceilings for the
closed set of `EntitlementKey`s — see [usage and quotas](./USAGE_AND_QUOTAS.md) for what those keys
mean and how they are enforced. `FREE_PLAN_SLUG = "free"` is the fallback an account resolves to
whenever no paid subscription is currently granting.

## Webhook security and idempotency (§12–14)

`POST /billing/webhook` is the one endpoint that mutates subscription state, and it trusts nothing
until the adapter has verified the payload:

1. **Verify (§12–13).** `verify_webhook` checks the signature and timestamp against `now` using the
   adapter's own scheme and shared secret. A bad signature or a stale timestamp raises
   `BillingError(WEBHOOK_SIGNATURE_INVALID)` *before* the body is parsed; a verified-but-unparseable
   body (not JSON, a missing field, a status outside the closed set) raises `WEBHOOK_MALFORMED`.
   Verification is local — the adapter never calls back to the provider.
2. **Record once (§14).** The verified `NormalizedWebhookEvent` is written to an append-only ledger
   keyed on the provider event id. A redelivery of the same id finds the row already present and
   applies nothing — at-most-once effect over at-least-once delivery.
3. **Ignore the stale (§14).** An event describing a subscription transition older than the row
   already reflects is acknowledged (`200`) and dropped, so out-of-order delivery cannot roll a
   subscription backwards. The endpoint always answers `200` once a payload is verified, because a
   non-2xx tells the provider to redeliver — which is only useful for a transient fault, never for a
   business decision the platform has already made.

The four cases the tests pin (§64): a valid new-subscription event applies; a replay of it is a
no-op; an out-of-order/stale event is ignored; an invalid-signature event is rejected without
mutating anything.

## Subscription lifecycle (§15–16)

`SubscriptionStatus` is a closed set, and access is *derived* from it as of an instant —
`grants_plan_entitlements(as_of)` on the domain model answers the clock question, never a stored
grant:

| Status | Grants the paid plan? | Meaning |
| --- | --- | --- |
| `TRIALING` | yes | in trial, full paid entitlements |
| `ACTIVE` | yes | paid and current |
| `CANCEL_AT_PERIOD_END` | yes, until the period ends | cancelled but still inside the paid window |
| `PAST_DUE` | no | payment failed — degrades to the free tier, data untouched |
| `CANCELED` | no | ended — degrades to the free tier |

The three entitlement-bearing states are `TRIALING`, `ACTIVE` and `CANCEL_AT_PERIOD_END`. When none
grants (or the subscription's plan row has vanished, or there is no subscription at all) the account
resolves to `FREE_PLAN_SLUG`. Losing a paid plan is therefore a ceiling change and nothing more:
no row is deleted, no `ApplicationPolicy` is touched, no safety brake moves.

## The billing API (§17)

All routes live under the `/billing` prefix and are session-authenticated and owner-scoped — a
caller only ever sees and acts on their own subscription:

| Method + path | Purpose |
| --- | --- |
| `GET /billing/plans` | the server-authoritative catalogue (prices, intervals, entitlements) |
| `GET /billing/subscription` | the current account's subscription overview and effective plan |
| `GET /billing/usage` | the usage snapshot per key against the current window's ceilings |
| `POST /billing/checkout` | mint a checkout redirect for a chosen plan |
| `POST /billing/portal` | mint the provider portal redirect for an existing customer |
| `POST /billing/webhook` | the provider's callback — the only state-mutating route (above) |

`checkout` and `portal` are rate limited under the `checkout` category (`docs/RATE_LIMITING.md`).
A response carries a redirect URL and the account's own state — never a provider payload, a customer
id the client did not already own, or any secret.

## The `/billing` frontend (§18, §61)

The `/billing` screen renders what the API returns and holds **no pricing, quota or entitlement
authority of its own** (§61). It reads `GET /plans`, `/subscription` and `/usage`, shows the
account's plan and how close each meter is to its ceiling (§60), and links out to checkout and the
portal. It never decides whether an action is allowed — the server does, at the point the action is
metered. A paywall the frontend draws is a hint; the enforceable answer is the `QUOTA_EXCEEDED` the
server returns. See [usage and quotas](./USAGE_AND_QUOTAS.md) for the quota UX contract.

## Security callouts

- **No secret ever leaves the boundary (§41).** Signing secrets, the Stripe secret key, customer
  ids and card data are never logged, never returned to a client, never placed in an error message.
  A backup, an export and an error report all carry the *fact* of a subscription, never a provider
  credential.
- **The webhook is the only writer.** No client route mutates subscription state directly; a client
  can only mint a redirect and then observe the result the verified webhook recorded.
- **Enumeration-safe (§52).** Billing errors never reveal another account's subscription internals
  or whether a customer exists; a caller learns only about their own account.

