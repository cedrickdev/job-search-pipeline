"""The race-safe quota reservation and the append-only ledger write — the spine's two ends (§5-9).

A metered domain action is book-ended here. Before it runs, `authorize` asks the *commercial*
question — is there room the plan paid for — under an advisory lock, so two workers racing the
last unit of a period's allowance cannot both read the budget as free. After it runs, `record`
writes one append-only, idempotent `UsageEvent` for what was actually consumed. In between, the
existing domain service runs every one of its safety gates: `authorize` answers one clause of the
effective-permission AND (§4), never the whole verdict, so raising a plan's quota can widen what
an account *may* do commercially but can never weaken a safety brake.

Two rules the module keeps, both load-bearing:

- **The reservation is atomic only while its transaction holds the lock.** `authorize` takes a
  transaction-scoped advisory lock and reads the period sum under it; the caller then runs the
  action and calls `record`, all in the *same* unit of work, so the lock is held from the check
  through the write to the commit that releases it. A second worker blocks on the lock until then
  and sees the consumption the first committed. This mirrors the Phase 12 submission-budget
  reservation exactly, in its own lock namespace (§49-51).
- **The unknown is never fabricated (§5).** `record` writes no event for a non-positive or
  unknown (`None`) quantity — an LLM run whose provider reported no token count meters nothing
  rather than a fabricated `0`. A `UsageEvent` therefore always stands for real, measured, positive
  consumption, and re-metering the same source collapses onto one row by the id the idempotency
  key derives (§7, §9).
"""
from datetime import datetime

from backend.app.billing.entitlements import EntitlementResolver, ResolvedEntitlements
from backend.app.billing.errors import BillingError, BillingErrorCode
from backend.app.domain.entitlement import (
    Entitlement,
    EntitlementKey,
    EntitlementMeasure,
    entitlement_measure,
)
from backend.app.domain.identifiers import UserId, usage_event_id
from backend.app.domain.usage import (
    UsageEvent,
    UsagePeriod,
    UsageSourceType,
    build_usage_idempotency_key,
)
from backend.app.repositories.contracts import UsageEventRepository


class MeteringService:
    """Reserve a per-period quota race-safely, then record the measured consumption (§5-9).

    Holds the entitlement resolver (which plan and which window apply to an account as of an
    instant) and the append-only usage ledger. No clock in the constructor — every method takes
    the instant it needs — the convention every V2 service keeps. The service has no authority
    over any domain state: it can refuse a metered action for lack of commercial room and it can
    record that one happened, and nothing else.
    """

    def __init__(self, resolver: EntitlementResolver, usage: UsageEventRepository) -> None:
        self._resolver = resolver
        self._usage = usage

    async def authorize(self, user_id: UserId, key: EntitlementKey, *, quantity: int,
                        as_of: datetime) -> ResolvedEntitlements:
        """Reserve commercial room for `quantity` more of a per-period `key`, or raise (§8).

        Takes the transaction-scoped usage-budget lock, resolves the account's effective plan and
        billing window as of `as_of`, sums what the window has already consumed, and raises
        `QUOTA_EXCEEDED` when there is no room. Returns the resolved entitlements so the caller can
        `record` against the same window's label. Must be called at the start of the same unit of
        work that runs the action and calls `record`, so the lock it takes serializes the whole
        count → decide → write against another worker of the same account. Only for per-period
        keys — a concurrent gauge (active search profiles) is checked with `authorize_concurrent`.
        """
        if entitlement_measure(key) is not EntitlementMeasure.PER_PERIOD:
            raise ValueError(
                f"{key.value} is a concurrent gauge, not a per-period meter; check it with "
                "authorize_concurrent against a live count")
        await self._usage.lock_usage_budget(user_id)
        resolved = await self._resolver.resolve(user_id, as_of=as_of)
        used = await self._usage.sum_for_period(user_id, key, resolved.period.label)
        self._require_room(resolved.entitlement_for(key), key,
                           already_used=used, quantity=quantity, period=resolved.period)
        return resolved

    def authorize_concurrent(self, resolved: ResolvedEntitlements, key: EntitlementKey, *,
                             active_count: int, quantity: int = 1) -> None:
        """Check a concurrent gauge (active search profiles) against a live count, or raise (§6).

        The gauge shape: the caller owns the live count of currently-active resources — a domain
        object that queried it could not be tested deterministically — and this weighs `quantity`
        more against the plan's ceiling. Pure and synchronous: it reserves nothing itself (creating
        the resource does, and the next count sees it), so a resource-creation path calls this
        under its own lock exactly as `authorize` does for a meter. Raises `QUOTA_EXCEEDED` when
        the live count leaves no room.
        """
        if entitlement_measure(key) is not EntitlementMeasure.CONCURRENT:
            raise ValueError(
                f"{key.value} is a per-period meter, not a concurrent gauge; reserve it with "
                "authorize")
        self._require_room(resolved.entitlement_for(key), key,
                           already_used=active_count, quantity=quantity, period=None)

    def _require_room(self, entitlement: Entitlement | None, key: EntitlementKey, *,
                      already_used: int, quantity: int, period: UsagePeriod | None) -> None:
        """Raise `QUOTA_EXCEEDED` unless `quantity` more units fit — the one refusal point.

        A `None` entitlement means the plan grants this capability no allowance at all, the most
        restrictive answer (§2): any positive quantity is refused. Otherwise the domain's
        `quota_permits` decides, and the detail names the key, the ceiling, what is used and (for a
        meter) the window — all fixed, domain-owned facts, never a secret.
        """
        if quantity <= 0:
            return
        window = "" if period is None else f" in period {period.label}"
        if entitlement is None:
            raise BillingError(
                BillingErrorCode.QUOTA_EXCEEDED,
                f"your plan grants no {key.value} allowance; an upgrade is required")
        if not entitlement.quota_permits(already_used=already_used, quantity=quantity):
            raise BillingError(
                BillingErrorCode.QUOTA_EXCEEDED,
                f"your plan's {key.value} allowance of {entitlement.limit} is exhausted "
                f"({already_used} used{window}); an upgrade is required")

    async def record(self, user_id: UserId, key: EntitlementKey, *,
                     source_type: UsageSourceType, source_id: str, quantity: int | None,
                     occurred_at: datetime, billing_period: str,
                     detail: str | None = None) -> UsageEvent | None:
        """Write one append-only, idempotent usage event for measured consumption, or nothing (§5).

        A non-positive or unknown (`None`) quantity writes *no* event — the honest record of an
        unmeasurable call, never a fabricated `0`. Otherwise the idempotency key and id derive from
        `(key, source_type, source_id)`, so re-metering the same source collapses onto the one row
        the ledger already holds rather than double-charging. Call inside the unit of work
        `authorize` locked, so the event this writes is what the next worker's sum sees.
        """
        if quantity is None or quantity <= 0:
            return None
        idempotency_key = build_usage_idempotency_key(
            entitlement_key=key, source_type=source_type, source_id=source_id)
        event = UsageEvent(
            id=usage_event_id(idempotency_key), user_id=user_id, entitlement_key=key,
            quantity=quantity, source_type=source_type, source_id=source_id,
            occurred_at=occurred_at, billing_period=billing_period,
            idempotency_key=idempotency_key, detail=detail)
        return await self._usage.add(event)
