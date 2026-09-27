"""`SubscriptionEvent` — the append-only ledger of billing webhooks already processed (§13).

A billing provider redelivers and reorders its webhooks; the platform must apply each event's
effect *exactly once* and never backwards. Two guards make that true, and this model is the
second of them. The first is `Subscription` itself: its id derives from the provider's own
subscription handle, so every event about one subscription lands on one row, and `supersedes`
rejects a stale redelivery. This ledger is the belt to that model's braces — one row per
*event* (its id derived from the provider's event id, `subscription_event_id`), written after
the event is handled, so a redelivery of the same event id collides on the primary key and is
recognised as already-seen rather than applied a second time.

The record is deliberately an *audit fact*, not an authority: it says "this event arrived, and
here is what the platform did with it", and it holds no power to change a subscription or a
quota. Writing it is the last step of handling a webhook, never the thing that grants access.

`outcome` records which of three things handling did, so an operator reading the ledger can tell
a real state change from a no-op without re-deriving it:

- `APPLIED` — the event was newer than what was recorded and moved the subscription forward;
- `SUPERSEDED` — a newer state was already stored, so the out-of-order guard ignored this one;
- `IGNORED` — a well-formed event the platform does not act on (a type it maps to no state, or
  one it cannot attribute to an account) — recorded so the redelivery is still a no-op.

Pure domain values: `backend.app.domain` imports the standard library and Pydantic only
(docs/ARCHITECTURE.md §1). The billing adapter normalizes a provider's webhook into the values a
service turns into one of these; this module knows nothing of Stripe or any provider SDK.
"""
from enum import StrEnum

from backend.app.domain.base import DomainModel, NonEmptyStr, UtcDatetime
from backend.app.domain.identifiers import SubscriptionEventId, SubscriptionId, UserId


class SubscriptionEventOutcome(StrEnum):
    """What handling a billing webhook did with it — the closed vocabulary the ledger records.

    A member a caller invents fails to parse rather than recording an untraceable outcome, the
    discipline every closed domain vocabulary keeps.

    - `APPLIED` — the event postdated the recorded provenance and updated the subscription;
    - `SUPERSEDED` — a newer event was already applied, so `Subscription.supersedes` rejected
      this one and nothing was written backwards (§15);
    - `IGNORED` — a well-formed event the platform does not act on: a provider event type that
      maps to no subscription state, or one that cannot be attributed to an account. Recorded so
      a redelivery is still recognised and remains a no-op.
    """

    APPLIED = "APPLIED"
    SUPERSEDED = "SUPERSEDED"
    IGNORED = "IGNORED"


class SubscriptionEvent(DomainModel):
    """One processed billing webhook, recorded once so its redelivery is a no-op (§13).

    Its id derives from the provider's own event id (`subscription_event_id`), so a redelivered
    event computes the same id and collides on the primary key rather than being handled twice —
    the load-bearing half of webhook idempotency, guarded a second time by `UNIQUE (provider,
    external_event_id)`. `provider` and `external_event_id` are the provider's handles; `event_type`
    is the provider's raw type string kept for audit (never interpreted for access). `user_id` and
    `subscription_id` are the account and subscription the event touched *when they could be
    resolved* — both optional, because an event the platform cannot attribute is recorded as
    `IGNORED` rather than dropped, so its redelivery is still a no-op. `event_at` is the provider's
    provenance instant (what the out-of-order guard compares), `received_at` is when the platform
    handled it, and `detail` is a short, secret-free note (an unmapped price id, a missing
    attribution) an operator can read.

    Append-only like the usage ledger: a record is written once when the event is handled and
    never mutated. It carries no write authority over anything — reading it changes nothing, and
    a subscription's state is owned by `Subscription`, never by this audit fact.
    """

    id: SubscriptionEventId
    provider: NonEmptyStr
    external_event_id: NonEmptyStr
    event_type: NonEmptyStr
    outcome: SubscriptionEventOutcome
    user_id: UserId | None = None
    subscription_id: SubscriptionId | None = None
    event_at: UtcDatetime
    received_at: UtcDatetime
    detail: NonEmptyStr | None = None
