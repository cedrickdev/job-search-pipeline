"""The one typed refusal the billing layer raises — `BillingError` (§4, §54, §90).

Every service in this package refuses the same way the career loop and the interview service
do: a single exception carrying a stable `BillingErrorCode` and a secret-free `detail`, mapped
to an HTTP status once in `backend.app.api.errors` rather than in each route. One exception with
a closed code vocabulary — not a class per condition — because the code is what a client
branches on and the sentence is only for a human reading a log; keeping them together means the
status for "your plan has no room for this" is decided in exactly one table.

The disciplines the codes encode, all spine-critical:

- **Hitting a commercial ceiling is not a safety refusal, and not a transient one.**
  `QUOTA_EXCEEDED` is the *commercial* half of the effective-permission AND saying "there is no
  room the plan paid for" (§4, §8). It is deliberately distinct from an `ApplicationPolicy` rate
  limit (`APPLICATION_RATE_LIMITED`, 429, retryable after a short wait): a plan quota is not
  resolved by waiting a moment but by upgrading or by the period resetting, so it maps to 402
  Payment Required, the status that tells a surface to offer an upgrade rather than to retry.
  Raising this never means the action was unsafe — the domain gates are separate clauses that
  ran (or would run) regardless.
- **A missing catalogue is the operator's fault, never the caller's.** `PLAN_CATALOGUE_MISSING`
  is raised only when the free-tier plan the resolver falls back to has not been seeded, which
  is a deployment mistake, so it maps to 500 rather than blaming a well-formed request. It exists
  so a resolver never silently invents an entitlement when its catalogue is absent — it refuses
  loudly instead.
- **An inbound webhook is never trusted before it is verified (§12).** A subscription's state is
  webhook-authoritative, so a forged or replayed payload that changed it would be a direct
  attack on the commercial spine. `WEBHOOK_SIGNATURE_INVALID` rejects a payload whose signature
  or timestamp does not check out *before* it is parsed, and `WEBHOOK_MALFORMED` rejects a
  verified one the adapter cannot normalize onto the domain's closed vocabulary — both 400,
  because the platform, not the account, is refusing the request. A call *out* to the provider
  that fails is a `PROVIDER_UNAVAILABLE` (502): an upstream fault, distinct from a caller error.

`detail` is composed from ids, keys and fixed sentences the domain owns; it never carries a
provider's message, a candidate's words or a payload, so surfacing it echoes nothing sensitive.
"""
from enum import StrEnum


class BillingErrorCode(StrEnum):
    """The closed vocabulary of ways a billing service refuses (§4, §54, §90).

    Each maps to exactly one HTTP status in `backend.app.api.errors._BILLING_STATUS`. A member a
    provider or a client cannot invent — it is an enum, not a free string — so a new refusal is a
    member added here plus a row in that table, never an ad-hoc code at a call site.

    - `QUOTA_EXCEEDED` — the account's effective plan grants no room for this consumption in the
      current billing window (402); the commercial clause of the AND, never a safety verdict;
    - `PLAN_CATALOGUE_MISSING` — the free-tier plan the resolver falls back to is not seeded, a
      deployment fault the resolver refuses loudly rather than papering over (500).
    - `WEBHOOK_SIGNATURE_INVALID` — an inbound billing webhook failed signature or timestamp
      verification, so it is rejected unread (400); the platform never trusts an unsigned or
      replayed payload to change what an account is subscribed to (§12).
    - `WEBHOOK_MALFORMED` — a signature-verified webhook could not be normalized: a payload that
      is not JSON, is missing a field the platform needs, or names a status outside the closed
      set the domain understands (400). Distinct from a bad signature so a log tells a forged
      payload from a genuine one the adapter cannot map.
    - `PROVIDER_UNAVAILABLE` — a call *out* to the billing provider (opening a checkout or a
      portal session) failed or answered with an error (502); a transient upstream fault, not the
      caller's request being wrong.
    """

    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    PLAN_CATALOGUE_MISSING = "PLAN_CATALOGUE_MISSING"
    WEBHOOK_SIGNATURE_INVALID = "WEBHOOK_SIGNATURE_INVALID"
    WEBHOOK_MALFORMED = "WEBHOOK_MALFORMED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


class BillingError(Exception):
    """A typed, secret-free refusal from a billing service (§4, §54, §90).

    Carries the stable `code` a client branches on and a `detail` sentence for a human. The
    detail is composed by the service from the domain's own vocabulary — entitlement keys,
    billing-period labels, fixed phrases — never a provider message or user input, so
    `backend.app.api.errors` can surface it verbatim without leaking anything. The API decides
    the status from `code` alone.
    """

    def __init__(self, code: BillingErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
