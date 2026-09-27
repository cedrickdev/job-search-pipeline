"""`Subscription` — one account's live commercial relationship with a billing provider (§10-16).

A subscription is the link between a `User` and the `Plan` they are paying for, kept in a
shape that is *provider-neutral* and *webhook-authoritative*. The platform never trusts a
browser's word about what an account is subscribed to; the truth arrives as a signature-verified
webhook the billing adapter normalizes, and this model is that normalized truth. Its id is
derived from the provider's own subscription handle (`subscription_id`), so every event about
one subscription lands on one row.

Four rules shape it, and each protects the spine's core invariant that billing may raise a
commercial quota but never weaken a safety rule (§4):

- **Internal states are normalized, never a provider's raw string (§14).** `SubscriptionStatus`
  is closed to the five the platform reasons about — trialing, active, past-due, cancel-at-
  period-end, canceled — so a provider inventing a status fails to normalize rather than
  smuggling an unknown state the access check would not know how to weigh.
- **Access is derived, downgrade is capability-only (§16).** `grants_plan_entitlements` decides
  whether the plan's entitlements currently apply. A lapsed or past-due subscription simply
  stops granting the *paid* allowances — the account falls back to the free tier's ceilings —
  and **nothing here ever deletes data or touches an `ApplicationPolicy`**: losing a paid quota
  narrows what an account may do, it never makes the platform less safe.
- **Out-of-order events are ignored, never applied backwards (§15).** Providers redeliver and
  reorder webhooks. `provider_event_at` and `provider_event_sequence` record the provenance of
  the last event applied, and `supersedes` answers whether an incoming event is newer, so a
  stale redelivery of an older state cannot roll a subscription back.
- **Nothing here reads a clock.** `grants_plan_entitlements` and `is_expired` take the instant
  to compare against, like every time-dependent domain method, so a service cannot disagree
  with the model about what "now" is and expiry stays testable without freezing time.

Pure domain values: `backend.app.domain` imports the standard library and Pydantic only
(docs/ARCHITECTURE.md §1). The billing adapter (Phase 16 §11-13) maps a provider's objects onto
this model; this module knows nothing of Stripe or any provider SDK.
"""
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from backend.app.domain.base import DomainModel, NonEmptyStr, UtcDatetime
from backend.app.domain.identifiers import PlanId, SubscriptionId, UserId

# The billing provider key stamped on a subscription that no external provider backs — the
# free tier, or a plan an operator granted directly. Kept as a constant so the "is this a real
# provider subscription" check reads one name rather than a scattered literal.
INTERNAL_BILLING_PROVIDER = "internal"


class SubscriptionStatus(StrEnum):
    """The normalized internal lifecycle of a subscription — the closed set from §14.

    A provider's own vocabulary (Stripe alone has a dozen statuses) is collapsed by the billing
    adapter into exactly the five states the platform reasons about, so the access check never
    meets a status it does not understand. A member a provider invents fails to normalize rather
    than silently granting or denying access.

    - `TRIALING` — a trial is running; the plan's entitlements apply (§14);
    - `ACTIVE` — paid and current; entitlements apply;
    - `PAST_DUE` — a payment failed and the provider is retrying; access degrades to the free
      tier per the grace rule (§16), data is never touched;
    - `CANCEL_AT_PERIOD_END` — canceled but paid through the current period; entitlements apply
      until `current_period_end`, then it lapses;
    - `CANCELED` — ended; the plan's entitlements no longer apply and the account falls back to
      the free tier (§16).
    """

    TRIALING = "TRIALING"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    CANCEL_AT_PERIOD_END = "CANCEL_AT_PERIOD_END"
    CANCELED = "CANCELED"


# The statuses under which a subscription's plan entitlements apply, subject to the period
# window. `PAST_DUE` is deliberately absent: a failed payment degrades to the free tier per the
# grace rule (§16), and whether a short operator-configured grace re-grants it is a service
# decision, not the model's. `CANCELED` is absent because it has ended. Stated once so the
# access check reads one set.
_ACCESS_GRANTING_STATUSES: frozenset[SubscriptionStatus] = frozenset({
    SubscriptionStatus.TRIALING,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.CANCEL_AT_PERIOD_END,
})


class Subscription(DomainModel):
    """One account's normalized, webhook-authoritative subscription to a plan (§10-16).

    User-owned like every entity, read `WHERE user_id = ?`. It names the `plan_id` the account
    pays for, its normalized `status`, and the billing window (`current_period_start`/
    `current_period_end`) the per-period meters reset on. `provider` is the billing adapter key
    that owns it (`INTERNAL_BILLING_PROVIDER` for a free or operator-granted subscription);
    `external_customer_id` and `external_subscription_id` are the provider's opaque handles the
    adapter needs to open a portal or cancel — never interpreted here. `provider_event_at` and
    `provider_event_sequence` are the provenance of the last event applied, the pair `supersedes`
    uses to reject a stale redelivery (§15).

    The model holds no write authority over anything but its own state: it records what an
    account is subscribed to and lets `grants_plan_entitlements` derive whether the paid
    allowances currently apply. Turning "no longer paying" into "smaller quotas" is the
    entitlement resolver's job; this object never deletes data and never touches a policy (§16).
    """

    id: SubscriptionId
    user_id: UserId
    plan_id: PlanId
    status: SubscriptionStatus
    provider: NonEmptyStr = INTERNAL_BILLING_PROVIDER
    external_customer_id: NonEmptyStr | None = None
    external_subscription_id: NonEmptyStr | None = None
    current_period_start: UtcDatetime | None = None
    current_period_end: UtcDatetime | None = None
    cancel_at_period_end: bool = False
    provider_event_at: UtcDatetime | None = None
    provider_event_sequence: int | None = Field(default=None, ge=0)
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _window_and_timestamps_are_coherent(self) -> Self:
        start, end = self.current_period_start, self.current_period_end
        if (start is None) != (end is None):
            raise ValueError(
                "a Subscription billing window is both-or-neither: current_period_start and "
                "current_period_end are set together or both left unset")
        if start is not None and end is not None and end <= start:
            raise ValueError("Subscription current_period_end must follow current_period_start")
        if self.status is SubscriptionStatus.CANCEL_AT_PERIOD_END \
                and not self.cancel_at_period_end:
            raise ValueError(
                "a CANCEL_AT_PERIOD_END subscription must carry cancel_at_period_end=True")
        if self.updated_at < self.created_at:
            raise ValueError("Subscription updated_at must not precede created_at")
        return self

    @property
    def is_provider_backed(self) -> bool:
        """Whether an external billing provider backs this subscription (not the internal tier)."""
        return self.provider != INTERNAL_BILLING_PROVIDER

    def is_expired(self, as_of: datetime) -> bool:
        """Whether the current paid period has elapsed at `as_of`.

        An unbounded subscription (no window — the internal free tier) never expires. A bounded
        one is expired once `as_of` reaches `current_period_end`; the access check uses this so a
        `CANCEL_AT_PERIOD_END` subscription keeps its entitlements right up to the period end and
        loses them the instant it passes, without a background job having to flip the status.
        """
        return self.current_period_end is not None and as_of >= self.current_period_end

    def grants_plan_entitlements(self, as_of: datetime) -> bool:
        """Whether the plan's paid entitlements currently apply at `as_of` (§14, §16).

        `True` only when the status is one that grants access *and* the paid period has not
        elapsed. A `PAST_DUE` or `CANCELED` subscription returns `False` — the account keeps its
        data and its policy untouched but falls back to the free tier's ceilings, which is the
        capability-only downgrade §16 mandates. This answers commercial access alone; it is one
        clause of the effective-permission AND, never the safety verdict.
        """
        if self.status not in _ACCESS_GRANTING_STATUSES:
            return False
        return not self.is_expired(as_of)

    def supersedes(self, *, event_at: datetime, event_sequence: int | None) -> bool:
        """Whether an incoming provider event is newer than the last one applied (§15).

        The out-of-order guard: providers redeliver and reorder webhooks, so before applying an
        event's state a service asks whether it actually postdates what is already recorded. An
        event with a later `event_at` supersedes; one at the same instant supersedes only if its
        `event_sequence` is strictly greater (a provider's monotonic version breaks ties). A
        subscription with no recorded provenance (a brand-new row) is superseded by anything, so
        the first event always applies. A `None` incoming sequence at an equal instant does not
        supersede — without a tie-break the platform keeps what it has rather than risking a
        backward write.
        """
        if self.provider_event_at is None:
            return True
        if event_at > self.provider_event_at:
            return True
        if event_at < self.provider_event_at:
            return False
        # Same instant: only a strictly greater provider sequence breaks the tie forward.
        if event_sequence is None:
            return False
        if self.provider_event_sequence is None:
            return True
        return event_sequence > self.provider_event_sequence
