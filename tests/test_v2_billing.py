"""The billing layer over fakes — the resolver, the metering reservation, the seed, the invariant.

Four things this file proves without a database (the concurrency the reservation buys against a
real second worker is proved in `test_v2_persistence_usage_concurrency.py`):

- the entitlement resolver turns an account into its effective plan and window, and a lapsed or
  past-due subscription is a *capability-only* downgrade to the free tier (§16);
- `MeteringService.authorize` is the commercial clause of the effective-permission AND — it grants
  or refuses room, and refusing raises `QUOTA_EXCEEDED`, never a safety verdict (§4, §8);
- `MeteringService.record` is append-only and honest: an unknown consumption writes no event (§5);
- **the spine's hard rule**: a plan's quota may *raise* what an account may do commercially but can
  never widen an `ApplicationPolicy` — the last test holds a 100/month plan against a 2/day policy
  and shows the policy still wins (§4).
"""
import pytest

from backend.app.billing.catalogue import (
    DEMO_PLAN_PRICES,
    FREE_PLAN_SLUG,
    PlanPrice,
    seed_plan_catalogue,
)
from backend.app.billing.entitlements import EntitlementResolver
from backend.app.billing.errors import BillingError, BillingErrorCode
from backend.app.cli.seed_plans import format_seed_report, plan_pricing_from_env
from backend.app.domain.entitlement import BillingInterval, EntitlementKey
from backend.app.domain.subscription import SubscriptionStatus
from backend.app.billing.metering import MeteringService
from backend.app.domain.usage import UsageSourceType, calendar_month_period
from tests.v2_builders import (
    FREE_PLAN,
    LATER,
    NOW,
    PRO_PLAN,
    RUN,
    USER,
    a_plan,
    a_policy,
    a_subscription,
    a_usage_event,
    an_entitlement,
)
from tests.v2_fakes import (
    FakePlanRepository,
    FakeSubscriptionRepository,
    FakeUsageEventRepository,
)

pytestmark = pytest.mark.asyncio


def _a_free_plan(*entitlements):
    """The free tier as a fixture: no price, its own entitlements, the resolver's fallback slug."""
    return a_plan(
        slug=FREE_PLAN_SLUG, price_amount_cents=None, currency=None, billing_interval=None,
        external_price_id=None,
        entitlements=entitlements or (an_entitlement(
            key=EntitlementKey.APPLICATION_SUBMISSIONS, limit=5),))


async def _resolver_over(*plans, subscriptions=()):
    """An `EntitlementResolver` over fakes seeded with `plans` and `subscriptions`."""
    plan_repo, sub_repo = FakePlanRepository(), FakeSubscriptionRepository()
    for plan in plans:
        await plan_repo.upsert(plan)
    for subscription in subscriptions:
        await sub_repo.upsert(subscription)
    return EntitlementResolver(plan_repo, sub_repo), plan_repo, sub_repo


# -- the resolver: which plan, which window, and the capability-only downgrade -----------------

async def test_resolve_falls_back_to_free_without_a_subscription():
    """No subscription resolves to the free plan and the calendar-month window (§16)."""
    resolver, _, _ = await _resolver_over(_a_free_plan())
    resolved = await resolver.resolve(USER, as_of=NOW)
    assert resolved.plan.slug == FREE_PLAN_SLUG
    assert resolved.is_paid is False
    assert resolved.period == calendar_month_period(NOW)


async def test_resolve_returns_the_paid_plan_while_the_subscription_grants():
    """An active subscription resolves to its plan and meters against its own window (§14)."""
    resolver, _, _ = await _resolver_over(
        _a_free_plan(), a_plan(slug="pro"),
        subscriptions=(a_subscription(plan=PRO_PLAN, status=SubscriptionStatus.ACTIVE),))
    resolved = await resolver.resolve(USER, as_of=NOW)
    assert resolved.plan.id == PRO_PLAN
    assert resolved.is_paid is True
    assert resolved.period.label == NOW.date().isoformat()
    assert resolved.period.start == NOW


async def test_resolve_downgrades_a_past_due_subscription_to_free():
    """A failed payment degrades to the free tier — capability only, nothing deleted (§16)."""
    resolver, _, _ = await _resolver_over(
        _a_free_plan(), a_plan(slug="pro"),
        subscriptions=(a_subscription(plan=PRO_PLAN, status=SubscriptionStatus.PAST_DUE),))
    resolved = await resolver.resolve(USER, as_of=NOW)
    assert resolved.plan.slug == FREE_PLAN_SLUG
    assert resolved.is_paid is False


async def test_resolve_downgrades_an_expired_subscription_to_free():
    """A CANCEL_AT_PERIOD_END subscription loses its plan the instant its window passes (§14)."""
    resolver, _, _ = await _resolver_over(
        _a_free_plan(), a_plan(slug="pro"),
        subscriptions=(a_subscription(
            plan=PRO_PLAN, status=SubscriptionStatus.CANCEL_AT_PERIOD_END,
            cancel_at_period_end=True,
            current_period_start=NOW, current_period_end=LATER),))
    # Well past the window's end: it no longer grants the paid plan.
    resolved = await resolver.resolve(USER, as_of=LATER)
    assert resolved.plan.slug == FREE_PLAN_SLUG


async def test_resolve_falls_back_to_free_when_the_plan_row_vanished():
    """A granting subscription whose plan is unseeded never strands the account above free."""
    resolver, _, _ = await _resolver_over(
        _a_free_plan(),  # note: no pro plan seeded
        subscriptions=(a_subscription(plan=PRO_PLAN, status=SubscriptionStatus.ACTIVE),))
    resolved = await resolver.resolve(USER, as_of=NOW)
    assert resolved.plan.slug == FREE_PLAN_SLUG


async def test_resolve_raises_when_even_the_free_plan_is_unseeded():
    """An unseeded catalogue is a loud deployment refusal, never a silent entitlement."""
    resolver, _, _ = await _resolver_over()  # empty catalogue
    with pytest.raises(BillingError) as caught:
        await resolver.resolve(USER, as_of=NOW)
    assert caught.value.code is BillingErrorCode.PLAN_CATALOGUE_MISSING


# -- the metering reservation: authorize (the commercial clause) and record (the ledger) -------

async def _metering_over(*plans, subscriptions=(), usage_events=()):
    """A `MeteringService` and its usage ledger, over fakes seeded as given."""
    resolver, plan_repo, sub_repo = await _resolver_over(*plans, subscriptions=subscriptions)
    usage_repo = FakeUsageEventRepository()
    for event in usage_events:
        await usage_repo.add(event)
    return MeteringService(resolver, usage_repo), usage_repo, resolver


async def test_authorize_permits_a_metered_action_within_room():
    """Room the plan paid for is granted, and the resolved window is returned to `record`."""
    metering, _, _ = await _metering_over(_a_free_plan())  # 5 submissions, none used
    resolved = await metering.authorize(
        USER, EntitlementKey.APPLICATION_SUBMISSIONS, quantity=1, as_of=NOW)
    assert resolved.plan.slug == FREE_PLAN_SLUG
    assert resolved.period.label == calendar_month_period(NOW).label


async def test_authorize_refuses_when_the_period_is_exhausted():
    """A window at its ceiling refuses with `QUOTA_EXCEEDED`, an upgrade prompt not a retry (§8)."""
    metering, _, _ = await _metering_over(
        _a_free_plan(an_entitlement(key=EntitlementKey.APPLICATION_SUBMISSIONS, limit=1)),
        usage_events=(a_usage_event(),))  # one submission already this period
    with pytest.raises(BillingError) as caught:
        await metering.authorize(
            USER, EntitlementKey.APPLICATION_SUBMISSIONS, quantity=1, as_of=NOW)
    assert caught.value.code is BillingErrorCode.QUOTA_EXCEEDED


async def test_authorize_refuses_a_capability_the_plan_never_grants():
    """A key absent from the plan is the most restrictive answer — no allowance, refused (§2)."""
    metering, _, _ = await _metering_over(
        _a_free_plan(an_entitlement(key=EntitlementKey.LLM_TOKENS, limit=50_000)))
    with pytest.raises(BillingError) as caught:
        await metering.authorize(
            USER, EntitlementKey.APPLICATION_SUBMISSIONS, quantity=1, as_of=NOW)
    assert caught.value.code is BillingErrorCode.QUOTA_EXCEEDED
    assert "grants no" in caught.value.detail


async def test_authorize_permits_an_unlimited_allowance():
    """`limit=None` is a real granted value — unlimited — distinct from a large number."""
    metering, _, _ = await _metering_over(
        _a_free_plan(an_entitlement(key=EntitlementKey.APPLICATION_SUBMISSIONS, limit=None)))
    resolved = await metering.authorize(
        USER, EntitlementKey.APPLICATION_SUBMISSIONS, quantity=10_000, as_of=NOW)
    assert resolved.entitlement_for(EntitlementKey.APPLICATION_SUBMISSIONS).limit is None


async def test_authorize_rejects_a_concurrent_gauge():
    """A concurrent gauge is checked against a live count, never metered against a sum."""
    metering, _, _ = await _metering_over(_a_free_plan())
    with pytest.raises(ValueError):
        await metering.authorize(
            USER, EntitlementKey.ACTIVE_SEARCH_PROFILES, quantity=1, as_of=NOW)


async def test_authorize_is_a_noop_for_a_non_positive_quantity():
    """A zero-quantity check reserves nothing and never refuses, even at the ceiling."""
    metering, _, _ = await _metering_over(
        _a_free_plan(an_entitlement(key=EntitlementKey.APPLICATION_SUBMISSIONS, limit=1)),
        usage_events=(a_usage_event(),))  # exhausted
    resolved = await metering.authorize(
        USER, EntitlementKey.APPLICATION_SUBMISSIONS, quantity=0, as_of=NOW)
    assert resolved.plan.slug == FREE_PLAN_SLUG


async def test_authorize_concurrent_permits_then_refuses_against_the_live_count():
    """The gauge weighs `quantity` more against the live count, refusing when it overflows (§6)."""
    _, _, resolver = await _metering_over(
        _a_free_plan(an_entitlement(key=EntitlementKey.ACTIVE_SEARCH_PROFILES, limit=1)))
    metering = MeteringService(resolver, FakeUsageEventRepository())
    resolved = await resolver.resolve(USER, as_of=NOW)
    # Room for the first active profile, none for a second.
    metering.authorize_concurrent(
        resolved, EntitlementKey.ACTIVE_SEARCH_PROFILES, active_count=0)
    with pytest.raises(BillingError) as caught:
        metering.authorize_concurrent(
            resolved, EntitlementKey.ACTIVE_SEARCH_PROFILES, active_count=1)
    assert caught.value.code is BillingErrorCode.QUOTA_EXCEEDED


async def test_authorize_concurrent_rejects_a_per_period_meter():
    """A per-period meter is reserved with `authorize`, never checked as a gauge."""
    _, _, resolver = await _metering_over(_a_free_plan())
    metering = MeteringService(resolver, FakeUsageEventRepository())
    resolved = await resolver.resolve(USER, as_of=NOW)
    with pytest.raises(ValueError):
        metering.authorize_concurrent(
            resolved, EntitlementKey.APPLICATION_SUBMISSIONS, active_count=0)


async def test_record_writes_one_event_for_measured_consumption():
    """A positive, measured quantity becomes one append-only ledger row the next sum sees (§5)."""
    metering, usage, _ = await _metering_over(_a_free_plan())
    event = await metering.record(
        USER, EntitlementKey.LLM_TOKENS, source_type=UsageSourceType.LLM_RUN,
        source_id=str(RUN), quantity=1500, occurred_at=NOW, billing_period="2026-03")
    assert event is not None and event.quantity == 1500
    assert await usage.sum_for_period(USER, EntitlementKey.LLM_TOKENS, "2026-03") == 1500


@pytest.mark.parametrize("quantity", [None, 0, -3])
async def test_record_writes_nothing_for_an_unmeasurable_quantity(quantity):
    """An unknown or non-positive consumption writes no event — never a fabricated `0` (§5)."""
    metering, usage, _ = await _metering_over(_a_free_plan())
    event = await metering.record(
        USER, EntitlementKey.LLM_TOKENS, source_type=UsageSourceType.LLM_RUN,
        source_id=str(RUN), quantity=quantity, occurred_at=NOW, billing_period="2026-03")
    assert event is None
    assert await usage.sum_for_period(USER, EntitlementKey.LLM_TOKENS, "2026-03") == 0


async def test_record_is_idempotent_on_the_same_source():
    """Re-metering the same source collapses onto the one row rather than double-charging (§7)."""
    metering, usage, _ = await _metering_over(_a_free_plan())
    first = await metering.record(
        USER, EntitlementKey.LLM_TOKENS, source_type=UsageSourceType.LLM_RUN,
        source_id=str(RUN), quantity=1500, occurred_at=NOW, billing_period="2026-03")
    second = await metering.record(
        USER, EntitlementKey.LLM_TOKENS, source_type=UsageSourceType.LLM_RUN,
        source_id=str(RUN), quantity=1500, occurred_at=NOW, billing_period="2026-03")
    assert first.id == second.id
    assert await usage.sum_for_period(USER, EntitlementKey.LLM_TOKENS, "2026-03") == 1500


# -- the catalogue seed: the three tiers, idempotent, and the deployment's price handles -------

async def test_seed_writes_the_free_pro_and_scale_tiers():
    """A deployment writes exactly the three tiers; without configured pricing, none is priced.

    Pricing is deployment-configured, never baked into the catalogue (§17, §61): a seed with no
    `prices` still writes the three tiers and keeps each paid tier's monthly `billing_interval`,
    but leaves them *unpriced* — coherent, and simply not sellable until an operator supplies a
    price. No invented CHF amount appears in the catalogue definition.
    """
    plans = FakePlanRepository()
    written = await seed_plan_catalogue(plans, now=NOW)
    by_slug = {plan.slug: plan for plan in written}
    assert set(by_slug) == {"free", "pro", "scale"}
    assert by_slug["free"].is_free and by_slug["free"].price_amount_cents is None
    # A paid tier with no configured price is unpriced but keeps its billing cadence.
    assert by_slug["pro"].price_amount_cents is None
    assert by_slug["pro"].currency is None
    assert by_slug["pro"].billing_interval is BillingInterval.MONTHLY
    # `scale` leaves its heavy meters unlimited — a real granted value, not an omission.
    assert by_slug["scale"].entitlement_for(EntitlementKey.APPLICATION_SUBMISSIONS).limit is None


async def test_seed_applies_configured_prices_to_the_paid_tiers():
    """A deployment (or the demo set) supplies each paid tier's amount and currency at seed time.

    The one place a price enters the catalogue is `prices` at seed time; the free tier is never
    priced. `DEMO_PLAN_PRICES` is the explicitly non-production set fixtures use.
    """
    plans = FakePlanRepository()
    written = await seed_plan_catalogue(
        plans, now=NOW,
        prices={"pro": PlanPrice(amount_cents=2500, currency="EUR")})
    by_slug = {plan.slug: plan for plan in written}
    assert (by_slug["pro"].price_amount_cents, by_slug["pro"].currency) == (2500, "EUR")
    assert not by_slug["pro"].is_free
    assert by_slug["free"].price_amount_cents is None  # the free tier is never priced
    assert by_slug["scale"].price_amount_cents is None  # a tier absent from prices stays unpriced

    demo = {plan.slug: plan for plan in await seed_plan_catalogue(
        plans, now=LATER, prices=DEMO_PLAN_PRICES)}
    assert (demo["pro"].price_amount_cents, demo["pro"].currency) == (1900, "CHF")
    assert (demo["scale"].price_amount_cents, demo["scale"].currency) == (4900, "CHF")


async def test_seed_is_idempotent_and_preserves_created_at():
    """A re-seed lands on the one row per tier, keeping created_at and advancing updated_at."""
    plans = FakePlanRepository()
    await seed_plan_catalogue(plans, now=NOW)
    reseeded = await seed_plan_catalogue(plans, now=LATER)
    free = next(plan for plan in reseeded if plan.slug == "free")
    assert free.id == FREE_PLAN
    assert free.created_at == NOW  # original mint instant preserved
    assert free.updated_at == LATER  # reconciliation instant advanced
    assert len(await plans.list_active()) == 3  # not six — reconciled, not duplicated


async def test_seed_applies_the_deployment_price_handles():
    """A provider price id is supplied at seed time, never baked into the catalogue (§17)."""
    plans = FakePlanRepository()
    written = await seed_plan_catalogue(
        plans, now=NOW, external_price_ids={"pro": "price_live_pro"})
    by_slug = {plan.slug: plan for plan in written}
    assert by_slug["pro"].external_price_id == "price_live_pro"
    assert by_slug["scale"].external_price_id is None  # absent → no handle, never fabricated


# -- the seed CLI's env parsing: pricing is read from the environment, never baked in (§17) -----

async def test_plan_pricing_from_env_reads_amounts_and_handles_per_slug():
    """`JOBSEARCH_PLAN_PRICE_<SLUG>` and `..._PRICE_ID_<SLUG>` map to amounts and handles."""
    external_price_ids, prices = plan_pricing_from_env({
        "JOBSEARCH_PLAN_PRICE_PRO": "1900:CHF",
        "JOBSEARCH_PLAN_PRICE_ID_PRO": "price_live_pro",
        "JOBSEARCH_PLAN_PRICE_SCALE": "4900:CHF",
        "UNRELATED": "ignored",
    })
    assert external_price_ids == {"pro": "price_live_pro"}
    assert prices == {
        "pro": PlanPrice(amount_cents=1900, currency="CHF"),
        "scale": PlanPrice(amount_cents=4900, currency="CHF"),
    }


async def test_plan_pricing_from_env_rejects_a_malformed_amount_naming_the_variable():
    """A price with no currency or a non-numeric amount is refused, naming the variable."""
    with pytest.raises(ValueError, match="JOBSEARCH_PLAN_PRICE_PRO"):
        plan_pricing_from_env({"JOBSEARCH_PLAN_PRICE_PRO": "1900"})
    with pytest.raises(ValueError, match="JOBSEARCH_PLAN_PRICE_PRO"):
        plan_pricing_from_env({"JOBSEARCH_PLAN_PRICE_PRO": "free:CHF"})


async def test_seed_report_is_a_data_free_tally_without_the_handle_value():
    """The CLI's report names each tier's price and whether it is sellable, never a handle value."""
    plans = FakePlanRepository()
    written = await seed_plan_catalogue(
        plans, now=NOW, prices=DEMO_PLAN_PRICES,
        external_price_ids={"pro": "price_live_pro"})
    report = format_seed_report(written)
    assert "1900 CHF" in report and "unpriced (not sellable)" in report
    assert "checkout handle set" in report and "no checkout handle" in report
    assert "price_live_pro" not in report  # the handle value never appears


# -- the spine's hard rule: a plan's quota can raise commercial room but never widen a brake ---

async def test_a_generous_plan_quota_never_widens_the_application_policy():
    """§4: effective permission is the AND of the plan's room *and* the policy's safety brake.

    A `scale`-sized plan grants 100 submissions a month and, with two used, has ample commercial
    room. But the account's own `ApplicationPolicy` caps the day at two, and two are already in —
    so the brake leaves no room. The metered action is refused because *both* clauses must permit,
    and the commercial quota can only restrict, never widen, the domain safety brake it is ANDed
    with. Raising the plan to 100/month changed the commercial clause and nothing else.
    """
    plan_entitlement = an_entitlement(
        key=EntitlementKey.APPLICATION_SUBMISSIONS, limit=100)
    day_capped_policy = a_policy(max_applications_per_day=2, max_applications_per_week=10)

    # The commercial clause: with two used against a ceiling of 100, there is room to spare.
    assert plan_entitlement.quota_permits(already_used=2, quantity=1) is True
    assert plan_entitlement.remaining(already_used=2) == 98

    # The safety brake: the policy's own day limit is spent, independent of what the plan sells.
    assert day_capped_policy.remaining_submissions(
        submitted_today=2, submitted_this_week=2) == 0

    # The effective verdict is the AND — the policy wins, exactly as §4 requires.
    commercially_permitted = plan_entitlement.quota_permits(already_used=2, quantity=1)
    policy_permits = day_capped_policy.remaining_submissions(2, 2) > 0
    assert (commercially_permitted and policy_permits) is False
