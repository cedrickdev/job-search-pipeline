"""The commercial spine — entitlements, subscriptions and metering — as an application layer.

Phase 16 turns the platform into a SaaS, and this package is where its commercial pieces live
*without* any of them holding authority over the domain's safety rules. The one invariant the
whole package exists to protect is the spine:

    commercial entitlement → server-side quota check → existing domain service → authoritative
    usage event

Read left to right: a plan grants an `Entitlement` (a ceiling), a server-side quota check asks
whether there is commercial room, the *existing* domain service still runs every one of its
safety gates, and only a real, measured consumption writes an append-only `UsageEvent`. A paid
plan may **raise** a commercial quota; it may **never** weaken an `ApplicationPolicy`, an
eligibility gate, the human-approval brake, the truth/evidence guards, or a CAPTCHA/MFA
protection. The effective permission for any action is `domain permission AND user policy AND
commercial entitlement`, and everything here contributes only that last clause — a clause that
can restrict the conjunction, never widen it (§4).

The modules, in dependency order:

- `errors` — the one typed refusal a billing service raises, mapped to a status once in the API;
- `entitlements` — the resolver that turns an account into its effective `Plan` and billing
  window as of an instant (a current subscription's plan while it grants, the free tier
  otherwise — a capability-only downgrade that never touches data or policy);
- `metering` — the race-safe quota reservation (`authorize`) and the append-only, idempotent
  ledger write (`record`) that book-end a metered domain action;
- `catalogue` — the server-authoritative plan definitions and the idempotent seed that writes
  them; the frontend defines none of this and only ever reads it (§17, §61).
"""
