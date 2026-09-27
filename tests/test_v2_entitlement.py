# tests/test_v2_entitlement.py
"""The commercial permission layer, pinned: an entitlement restricts, it never expands (§2-4).

`Plan` and `Entitlement` are the "what did this account pay for" half of the spine, so these
tests hold what keeps them safe: only real capabilities may be metered (a closed key set), a
key's measure (gauge vs per-period meter) is intrinsic and total, `None` is unlimited and
distinct from an absent entitlement, the quota check answers commercial room and only that, and
a plan's price is all-or-nothing with no repeated key.
"""
import pytest
from pydantic import ValidationError

from backend.app.domain.entitlement import (
    BillingInterval,
    Entitlement,
    EntitlementKey,
    EntitlementMeasure,
    Plan,
    entitlement_measure,
)
from tests.v2_builders import NOW, a_plan, an_entitlement


def test_every_entitlement_key_has_a_measure() -> None:
    """`entitlement_measure` is total — a key added without a measure fails here, not silently."""
    for key in EntitlementKey:
        assert isinstance(entitlement_measure(key), EntitlementMeasure)


def test_active_search_profiles_is_the_only_concurrent_gauge() -> None:
    """The one live-count ceiling; every other key is a per-period meter (§6)."""
    assert entitlement_measure(EntitlementKey.ACTIVE_SEARCH_PROFILES) \
        is EntitlementMeasure.CONCURRENT
    per_period = [k for k in EntitlementKey
                  if entitlement_measure(k) is EntitlementMeasure.PER_PERIOD]
    assert EntitlementKey.ACTIVE_SEARCH_PROFILES not in per_period
    assert len(per_period) == len(EntitlementKey) - 1


def test_an_unlimited_entitlement_always_permits_and_reports_no_remaining() -> None:
    unlimited = an_entitlement(limit=None)
    assert unlimited.is_unlimited
    assert unlimited.remaining(already_used=10_000) is None
    assert unlimited.quota_permits(already_used=10_000, quantity=10_000)


def test_a_finite_entitlement_permits_up_to_its_limit_and_no_further() -> None:
    finite = an_entitlement(limit=5)
    assert finite.remaining(already_used=3) == 2
    assert finite.quota_permits(already_used=3, quantity=2)  # fills it exactly
    assert not finite.quota_permits(already_used=3, quantity=3)  # one past the ceiling


def test_remaining_never_goes_negative_when_usage_is_already_over_the_limit() -> None:
    """A limit lowered beneath prior usage reports zero room, never a negative one."""
    finite = an_entitlement(limit=2)
    assert finite.remaining(already_used=9) == 0
    assert not finite.quota_permits(already_used=9, quantity=1)


def test_a_non_positive_quantity_always_fits_even_at_a_full_limit() -> None:
    """A metered action that turns out to consume nothing is never refused for it (§8)."""
    full = an_entitlement(limit=5)
    assert full.quota_permits(already_used=5, quantity=0)
    assert full.quota_permits(already_used=5, quantity=-3)


def test_a_zero_limit_grants_none_of_a_capability() -> None:
    """`0` is a real value distinct from unlimited: it forbids the capability outright."""
    none_allowed = an_entitlement(limit=0)
    assert not none_allowed.is_unlimited
    assert none_allowed.remaining(already_used=0) == 0
    assert not none_allowed.quota_permits(already_used=0, quantity=1)


def test_a_plan_may_not_grant_the_same_key_twice() -> None:
    with pytest.raises(ValidationError, match="same EntitlementKey twice"):
        a_plan(entitlements=(
            an_entitlement(key=EntitlementKey.LLM_TOKENS, limit=1),
            an_entitlement(key=EntitlementKey.LLM_TOKENS, limit=2),
        ))


def test_a_plan_price_is_all_or_nothing() -> None:
    with pytest.raises(ValidationError, match="price is all-or-nothing"):
        a_plan(price_amount_cents=1900, currency=None)


def test_a_paid_plan_must_carry_a_billing_interval() -> None:
    with pytest.raises(ValidationError, match="must carry a billing_interval"):
        a_plan(price_amount_cents=1900, currency="CHF", billing_interval=None)


def test_a_free_plan_needs_no_price_and_reads_as_free() -> None:
    free = a_plan(slug="free", price_amount_cents=None, currency=None,
                  billing_interval=None, external_price_id=None,
                  entitlements=(an_entitlement(
                      key=EntitlementKey.APPLICATION_SUBMISSIONS, limit=2),))
    assert free.is_free
    assert free.billing_interval is None


def test_a_zero_priced_plan_reads_as_free_even_with_a_currency() -> None:
    free = a_plan(price_amount_cents=0, currency="CHF", billing_interval=None)
    assert free.is_free


def test_entitlement_for_returns_the_grant_or_none_for_an_ungranted_capability() -> None:
    """A `None` is the most restrictive answer — no allowance — never 'unlimited' (§2)."""
    plan = a_plan(entitlements=(an_entitlement(
        key=EntitlementKey.APPLICATION_SUBMISSIONS, limit=100),))
    assert plan.entitlement_for(EntitlementKey.APPLICATION_SUBMISSIONS).limit == 100
    assert plan.entitlement_for(EntitlementKey.INTERVIEW_SESSIONS) is None


def test_a_plan_updated_before_created_is_rejected() -> None:
    from datetime import timedelta
    with pytest.raises(ValidationError, match="updated_at must not precede created_at"):
        a_plan(created_at=NOW, updated_at=NOW - timedelta(days=1))


def test_billing_interval_is_a_closed_vocabulary() -> None:
    assert {i.value for i in BillingInterval} == {"MONTHLY", "YEARLY"}
