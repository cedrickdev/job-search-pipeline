"""The entitlement resolver — an account, as of an instant, becomes its effective plan (§14-16).

The middle of the spine's first link. A service that is about to meter a capability needs two
facts: *which* `Plan`'s entitlements currently apply to this account, and *which billing window*
a per-period meter sums against. Both are derived here, and both honour the one rule §16 makes
non-negotiable: losing a paid subscription is a **capability-only** downgrade. A `PAST_DUE`,
`CANCELED` or expired subscription simply stops granting the paid plan and the account falls back
to the free tier's ceilings — nothing here deletes data, and nothing here touches an
`ApplicationPolicy` or any other safety brake.

The resolution is deliberately small and total:

- **The effective plan** is the current subscription's plan *while* `grants_plan_entitlements`
  holds as of `as_of` (the domain answers the clock question, never this service); otherwise the
  free plan, read by its stable slug. A granting subscription whose plan row has vanished also
  falls back to free rather than failing — a missing catalogue plan must never strand an account
  above the free tier.
- **The billing window** is the subscription's own `[current_period_start, current_period_end)`
  while it grants a *bounded* paid period, labelled by its start date so consecutive renewals
  never share a sum; otherwise the calendar month `as_of` falls in, which is exactly what the
  free tier meters against (§6).

If even the free plan is absent the resolver raises `PLAN_CATALOGUE_MISSING` — a loud refusal,
never a silently invented entitlement — because a deployment that has not seeded its catalogue is
misconfigured, not a caller sending a bad request.
"""
from dataclasses import dataclass
from datetime import datetime

from backend.app.billing.catalogue import FREE_PLAN_SLUG
from backend.app.billing.errors import BillingError, BillingErrorCode
from backend.app.domain.entitlement import Entitlement, EntitlementKey, Plan
from backend.app.domain.identifiers import UserId
from backend.app.domain.subscription import Subscription
from backend.app.domain.usage import UsagePeriod, calendar_month_period
from backend.app.repositories.contracts import PlanRepository, SubscriptionRepository


@dataclass(frozen=True)
class ResolvedEntitlements:
    """One account's effective commercial permission as of an instant — plan, window, provenance.

    A value object, not a domain model: it is the application layer's answer to "what did this
    account pay for, right now, and which window does its usage count in". `plan` is the effective
    `Plan` (the paid one while it grants, the free tier otherwise); `period` is the window a
    per-period meter sums against; `is_paid` records whether a provider-backed subscription is
    currently granting, so a surface can tell "on the free tier" from "paying" without re-deriving
    it. `entitlement_for` is the one thing a quota check reads — the ceiling for a key, or `None`
    when the plan grants that capability no allowance at all (the most restrictive answer, §2).
    """

    plan: Plan
    period: UsagePeriod
    is_paid: bool

    def entitlement_for(self, key: EntitlementKey) -> Entitlement | None:
        """The effective ceiling for `key`, or `None` when the plan grants it no allowance."""
        return self.plan.entitlement_for(key)


class EntitlementResolver:
    """Resolve an account into its effective plan and billing window as of an instant (§14-16).

    Holds the shared plan catalogue and the account's subscription store. No clock in the
    constructor — every method takes `as_of`, so the access decision a request makes agrees with
    the rest of that request's timestamps and expiry stays testable without freezing time, the
    convention every V2 service keeps. `free_plan_slug` is injected so a test can point the
    fallback at a fixture plan, but defaults to the catalogue's canonical free tier.
    """

    def __init__(self, plans: PlanRepository, subscriptions: SubscriptionRepository, *,
                 free_plan_slug: str = FREE_PLAN_SLUG) -> None:
        self._plans = plans
        self._subscriptions = subscriptions
        self._free_plan_slug = free_plan_slug

    async def resolve(self, user_id: UserId, *, as_of: datetime) -> ResolvedEntitlements:
        """This account's effective plan and billing window, as of `as_of` (§14-16).

        The current subscription's plan while `grants_plan_entitlements(as_of)` holds — the domain
        owns the clock question, this service only reads its verdict — otherwise the free plan. A
        granting subscription whose plan row has vanished falls back to free too, so a missing
        catalogue entry never leaves an account stranded above the free tier. Raises
        `PLAN_CATALOGUE_MISSING` only when even the free plan is unseeded.
        """
        current = await self._subscriptions.get_current(user_id)
        if current is not None and current.grants_plan_entitlements(as_of):
            plan = await self._plans.get(current.plan_id)
            if plan is not None:
                return ResolvedEntitlements(
                    plan=plan, period=self._paid_period(current, as_of=as_of),
                    is_paid=current.is_provider_backed)
        return ResolvedEntitlements(
            plan=await self._free_plan(), period=calendar_month_period(as_of), is_paid=False)

    async def _free_plan(self) -> Plan:
        """The free-tier plan the fallback resolves to, or a loud refusal if it is unseeded."""
        free = await self._plans.get_by_slug(self._free_plan_slug)
        if free is None:
            raise BillingError(
                BillingErrorCode.PLAN_CATALOGUE_MISSING,
                f"the free-tier plan '{self._free_plan_slug}' is not seeded; the plan catalogue "
                "must be seeded before entitlements can be resolved")
        return free

    def _paid_period(self, subscription: Subscription, *, as_of: datetime) -> UsagePeriod:
        """The window a paid subscription's per-period meters sum against (§6, §14).

        Its own `[current_period_start, current_period_end)` while it carries a bounded window,
        labelled by the start date so consecutive renewals never fold into one sum. An unbounded
        granting subscription (an operator-granted internal one with no window) has no paid period,
        so it meters against the calendar month exactly as the free tier does.
        """
        start, end = subscription.current_period_start, subscription.current_period_end
        if start is None or end is None:
            return calendar_month_period(as_of)
        return UsagePeriod(start=start, end=end, label=start.date().isoformat())
