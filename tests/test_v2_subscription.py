# tests/test_v2_subscription.py
"""The subscription lifecycle, pinned: access is derived, downgrade is capability-only (§10-16).

`Subscription` is the normalized, webhook-authoritative link between an account and its plan, so
these tests hold the rules that keep billing from ever weakening safety: the internal state set
is closed and normalized, the plan's entitlements apply only under an access-granting status
inside the paid window, a lapsed/past-due subscription simply stops granting (never deletes
anything), and a stale or out-of-order provider event never rolls the subscription backwards.
"""
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.app.domain.subscription import (
    INTERNAL_BILLING_PROVIDER,
    SubscriptionStatus,
)
from tests.v2_builders import NOW, a_subscription

WITHIN = datetime(2026, 3, 15, 9, 30, tzinfo=UTC)      # inside the default [NOW, 2026-03-31)
AFTER_PERIOD = datetime(2026, 4, 1, 9, 30, tzinfo=UTC)  # past current_period_end


def test_the_status_vocabulary_is_the_five_normalized_states() -> None:
    assert {s.value for s in SubscriptionStatus} == {
        "TRIALING", "ACTIVE", "PAST_DUE", "CANCEL_AT_PERIOD_END", "CANCELED"}


@pytest.mark.parametrize("status", [
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.TRIALING,
])
def test_an_active_or_trialing_subscription_grants_entitlements_within_its_window(status) -> None:
    subscription = a_subscription(status=status)
    assert subscription.grants_plan_entitlements(WITHIN)


@pytest.mark.parametrize("status", [
    SubscriptionStatus.PAST_DUE,
    SubscriptionStatus.CANCELED,
])
def test_a_past_due_or_canceled_subscription_grants_nothing(status) -> None:
    """A capability downgrade to the free tier — never a data deletion (§16)."""
    subscription = a_subscription(status=status)
    assert not subscription.grants_plan_entitlements(WITHIN)


def test_cancel_at_period_end_keeps_access_until_the_period_ends() -> None:
    subscription = a_subscription(
        status=SubscriptionStatus.CANCEL_AT_PERIOD_END, cancel_at_period_end=True)
    assert subscription.grants_plan_entitlements(WITHIN)
    assert not subscription.grants_plan_entitlements(AFTER_PERIOD)


def test_cancel_at_period_end_must_carry_the_flag() -> None:
    with pytest.raises(ValidationError, match="must carry cancel_at_period_end=True"):
        a_subscription(status=SubscriptionStatus.CANCEL_AT_PERIOD_END,
                       cancel_at_period_end=False)


def test_an_active_subscription_loses_access_once_the_period_elapses() -> None:
    """Expiry is derived from the window, so no background job has to flip the status."""
    subscription = a_subscription(status=SubscriptionStatus.ACTIVE)
    assert subscription.grants_plan_entitlements(WITHIN)
    assert subscription.is_expired(AFTER_PERIOD)
    assert not subscription.grants_plan_entitlements(AFTER_PERIOD)


def test_an_unbounded_internal_subscription_never_expires() -> None:
    """The free tier has no provider window, so it grants its plan indefinitely."""
    free = a_subscription(
        provider=INTERNAL_BILLING_PROVIDER, external_customer_id=None,
        external_subscription_id=None, current_period_start=None,
        current_period_end=None, provider_event_at=None, provider_event_sequence=None,
        status=SubscriptionStatus.ACTIVE)
    assert not free.is_provider_backed
    assert not free.is_expired(AFTER_PERIOD)
    assert free.grants_plan_entitlements(AFTER_PERIOD)


def test_the_billing_window_is_both_or_neither() -> None:
    with pytest.raises(ValidationError, match="both-or-neither"):
        a_subscription(current_period_start=NOW, current_period_end=None)


def test_the_billing_window_must_run_forward() -> None:
    with pytest.raises(ValidationError, match="current_period_end must follow"):
        a_subscription(current_period_start=NOW, current_period_end=NOW)


def test_a_later_event_supersedes_the_recorded_one() -> None:
    subscription = a_subscription(provider_event_at=NOW, provider_event_sequence=5)
    assert subscription.supersedes(event_at=NOW + timedelta(seconds=1), event_sequence=1)


def test_an_earlier_event_never_supersedes() -> None:
    """The out-of-order guard: a stale redelivery of an older state is ignored (§15)."""
    subscription = a_subscription(provider_event_at=NOW, provider_event_sequence=5)
    assert not subscription.supersedes(
        event_at=NOW - timedelta(seconds=1), event_sequence=99)


def test_same_instant_breaks_the_tie_on_a_strictly_greater_sequence() -> None:
    subscription = a_subscription(provider_event_at=NOW, provider_event_sequence=5)
    assert subscription.supersedes(event_at=NOW, event_sequence=6)
    assert not subscription.supersedes(event_at=NOW, event_sequence=5)
    assert not subscription.supersedes(event_at=NOW, event_sequence=4)


def test_a_none_sequence_at_an_equal_instant_does_not_supersede() -> None:
    """Without a tie-break the platform keeps what it has rather than risking a backward write."""
    subscription = a_subscription(provider_event_at=NOW, provider_event_sequence=5)
    assert not subscription.supersedes(event_at=NOW, event_sequence=None)


def test_a_brand_new_subscription_is_superseded_by_anything() -> None:
    """No recorded provenance means the first event always applies."""
    fresh = a_subscription(provider_event_at=None, provider_event_sequence=None)
    assert fresh.supersedes(event_at=NOW, event_sequence=None)
    assert fresh.supersedes(event_at=NOW - timedelta(days=365), event_sequence=0)
