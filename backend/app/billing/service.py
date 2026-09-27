"""`BillingService` — the read side of the commercial surface, and the two hosted-session opens.

The webhook (`BillingWebhookService`) is the *only* thing that changes what an account is
subscribed to; this service never writes a subscription or a usage event. It answers the four
questions a `/billing` screen asks — what plans are for sale, what am I on, how much have I used,
and let me pay or manage — and it opens the two provider-hosted sessions that do the paying. It is
one more reader of the entitlement resolver and the metering ledger, never a second authority over
them (docs/IMPLEMENTATION_PLAN.md §Phase 16, the spine's read side).

Three things it is careful about:

- **Usage is reported through the same two shapes the metering layer enforces (§6).** A
  `PER_PERIOD` key is summed against the resolved billing window; the `CONCURRENT` gauge
  (`ACTIVE_SEARCH_PROFILES`) is a *live count* of active search profiles, never a period sum — the
  same split `MeteringService` keeps, read from `entitlement_measure` so the two can never
  disagree.
- **A checkout is opened only for a plan that can actually be bought.** The free tier, a retired
  (`is_active=False`) plan and any plan carrying no provider price are refused here with a typed
  error the API maps to a clear status, rather than handed to the provider to fail opaquely.
- **The redirect URLs are the server's, never the client's (§18).** `open_checkout` and
  `open_portal` take their success/cancel/return URLs from `SiteSettings`, so a request names only
  a plan slug and can never smuggle an attacker's origin into a provider redirect.
"""
from dataclasses import dataclass
from datetime import datetime

from backend.app.billing.entitlements import EntitlementResolver
from backend.app.billing.provider import BillingProvider, CheckoutSession, PortalSession
from backend.app.core.settings import SiteSettings
from backend.app.domain.entitlement import (
    EntitlementKey,
    EntitlementMeasure,
    Plan,
    entitlement_measure,
)
from backend.app.domain.identifiers import UserId
from backend.app.domain.subscription import SubscriptionStatus
from backend.app.domain.usage import UsagePeriod
from backend.app.repositories.contracts import (
    PlanRepository,
    SearchProfileRepository,
    SubscriptionRepository,
    UsageEventRepository,
)


class BillingServiceError(Exception):
    """Base for the billing service's own typed refusals — mapped to a status once, in the API.

    A sibling of `BillingError` rather than a member of its closed `BillingErrorCode`: these are
    the request-shaped refusals the read/checkout surface raises (a slug that names no plan, a plan
    that cannot be bought, a portal with no customer), and each maps to its own HTTP status in
    `install_v2_error_handlers`. Every one carries a secret-free `detail` sentence.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class PlanNotFound(BillingServiceError):
    """A checkout named a plan slug the catalogue does not carry (mapped to 404)."""


class PlanNotPurchasable(BillingServiceError):
    """A checkout named a plan that cannot be subscribed to — free, retired, or price-less (409)."""


class BillingCustomerMissing(BillingServiceError):
    """A portal was requested for an account with no provider-backed customer to manage (409)."""


@dataclass(frozen=True)
class SubscriptionOverview:
    """One account's commercial standing as of an instant — the `/billing` header, in one value.

    `plan` is the *effective* plan (the paid one while it grants, the free tier otherwise) and
    `is_paid` whether a provider-backed subscription is currently granting, both straight from the
    resolver. `status`, the window and `cancel_at_period_end` describe the account's live
    subscription row when it has one (a `PAST_DUE` or expired row is still shown, so a lapsed payer
    sees *why* they fell back to free); they are `None`/`False` for an account that never
    subscribed. `can_manage_billing` is whether there is a provider customer to open a portal for.
    `period` is the window a per-period meter currently sums against.
    """

    plan: Plan
    is_paid: bool
    period: UsagePeriod
    status: SubscriptionStatus | None
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    can_manage_billing: bool


@dataclass(frozen=True)
class UsageLine:
    """One entitlement's consumption against its ceiling — a single row of the usage panel.

    `limit` and `remaining` are `None` when the plan grants this capability without a ceiling
    (unlimited, a real granted value), which the surface renders as "unlimited" rather than a
    number. `used` is a real count either way — a period sum for a `PER_PERIOD` meter, a live count
    of active resources for the `CONCURRENT` gauge — so a client never has to know which shape a
    key is to render the row.
    """

    key: EntitlementKey
    measure: EntitlementMeasure
    limit: int | None
    used: int
    remaining: int | None


@dataclass(frozen=True)
class UsageSnapshot:
    """The whole usage panel: the billing window, the effective plan, and a line per entitlement."""

    period: UsagePeriod
    plan: Plan
    is_paid: bool
    lines: tuple[UsageLine, ...]


class BillingService:
    """The read/checkout surface over the plan catalogue, the resolver and the metering ledger.

    Holds the four repositories it reads (plans, subscriptions, the usage ledger, and the search
    profiles the concurrent gauge counts), the entitlement resolver that turns an account into its
    effective plan and window, the billing provider adapter that opens hosted sessions, and the
    site settings the redirect URLs are built from. No clock in the constructor — every method that
    needs one takes `as_of`, the convention every V2 service keeps.
    """

    def __init__(self, *, plans: PlanRepository, subscriptions: SubscriptionRepository,
                 usage: UsageEventRepository, searches: SearchProfileRepository,
                 resolver: EntitlementResolver, provider: BillingProvider,
                 site: SiteSettings) -> None:
        self._plans = plans
        self._subscriptions = subscriptions
        self._usage = usage
        self._searches = searches
        self._resolver = resolver
        self._provider = provider
        self._site = site

    async def list_plans(self) -> tuple[Plan, ...]:
        """The public pricing catalogue — active, public plans, cheapest first (§17, §61).

        `public_only` hides the internal free tier and any operator-only plan, and `is_active`
        drops a retired plan a new checkout may not pick, so this is exactly the set a pricing page
        may offer. The frontend defines none of it; it renders what this serves.
        """
        return await self._plans.list_active(public_only=True)

    async def subscription_overview(self, user_id: UserId, *,
                                    as_of: datetime) -> SubscriptionOverview:
        """This account's effective plan, live subscription state and billing window as of `as_of`.

        The plan and window come from the resolver (the paid plan while it grants, the free tier
        otherwise); the status and window fields come from the current subscription row when there
        is one, so a `PAST_DUE` account sees the lapsed row that explains its free-tier fallback. A
        portal can be opened only when the row is provider-backed and carries a customer handle.
        """
        resolved = await self._resolver.resolve(user_id, as_of=as_of)
        current = await self._subscriptions.get_current(user_id)
        can_manage = (current is not None and current.is_provider_backed
                      and current.external_customer_id is not None)
        return SubscriptionOverview(
            plan=resolved.plan,
            is_paid=resolved.is_paid,
            period=resolved.period,
            status=current.status if current is not None else None,
            current_period_start=current.current_period_start if current is not None else None,
            current_period_end=current.current_period_end if current is not None else None,
            cancel_at_period_end=current.cancel_at_period_end if current is not None else False,
            can_manage_billing=can_manage)

    async def usage_snapshot(self, user_id: UserId, *, as_of: datetime) -> UsageSnapshot:
        """The account's consumption against its effective plan's ceilings, in the current window.

        One line per entitlement the effective plan grants, ordered by key so the panel is stable.
        A per-period meter is summed against the resolved window's label; the concurrent gauge is a
        live count of active search profiles — the same split the metering layer enforces, so the
        numbers a user sees are the numbers a quota check reads.
        """
        resolved = await self._resolver.resolve(user_id, as_of=as_of)
        lines: list[UsageLine] = []
        for entitlement in sorted(resolved.plan.entitlements, key=lambda e: e.key.value):
            used = await self._used(user_id, entitlement.key, resolved.period)
            lines.append(UsageLine(
                key=entitlement.key, measure=entitlement.measure, limit=entitlement.limit,
                used=used, remaining=entitlement.remaining(used)))
        return UsageSnapshot(
            period=resolved.period, plan=resolved.plan, is_paid=resolved.is_paid,
            lines=tuple(lines))

    async def _used(self, user_id: UserId, key: EntitlementKey, period: UsagePeriod) -> int:
        """How much of `key` this account has used — a live count for a gauge, a period sum else."""
        if entitlement_measure(key) is EntitlementMeasure.CONCURRENT:
            active = await self._searches.list_for_user(user_id, active_only=True)
            return len(active)
        return await self._usage.sum_for_period(user_id, key, period.label)

    async def open_checkout(self, user_id: UserId, *, plan_slug: str) -> CheckoutSession:
        """Open a provider-hosted checkout for a purchasable plan, attributed to this account (§18).

        Refuses a slug that names no plan (`PlanNotFound`), and a plan that cannot be bought —
        the free tier, a retired plan, or one with no provider price (`PlanNotPurchasable`) —
        before the provider is called. An existing provider-backed subscription's customer handle
        is reused so an upgrade lands on the same billing customer. The success and cancel URLs are
        the server's, built from `SiteSettings`, never taken from the request.
        """
        plan = await self._plans.get_by_slug(plan_slug)
        if plan is None:
            raise PlanNotFound(f"no plan is registered under the slug '{plan_slug}'")
        if plan.is_free or not plan.is_active or plan.external_price_id is None:
            raise PlanNotPurchasable(
                f"the plan '{plan_slug}' cannot be checked out: it is the free tier, is retired, "
                "or carries no provider price")
        current = await self._subscriptions.get_current(user_id)
        customer_id = (current.external_customer_id
                       if current is not None and current.is_provider_backed else None)
        return await self._provider.open_checkout(
            plan=plan, client_user_id=user_id,
            success_url=self._site.checkout_success_url,
            cancel_url=self._site.checkout_cancel_url,
            customer_id=customer_id)

    async def open_portal(self, user_id: UserId) -> PortalSession:
        """Open the provider's billing portal for this account, or refuse if it has no customer.

        A portal manages an existing provider customer, so an account that never subscribed has
        nothing to manage — that is `BillingCustomerMissing`, not a provider error. The return URL
        is the server's `/billing` screen, never a client-supplied one (§18).
        """
        current = await self._subscriptions.get_current(user_id)
        if current is None or current.external_customer_id is None:
            raise BillingCustomerMissing(
                "this account has no billing customer to manage; subscribe to a plan first")
        return await self._provider.open_portal(
            customer_id=current.external_customer_id, return_url=self._site.portal_return_url)
