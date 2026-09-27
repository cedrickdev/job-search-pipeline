# tests/test_v2_usage.py
"""The usage ledger, pinned: append-only, idempotent, and never a fabricated zero (§5-9).

`UsageEvent` is the authoritative record a per-period quota sums against, so these tests hold
what makes it trustworthy: the idempotency key is a pure function of the consumption (so
re-metering one source collapses), the event id derives from that key, a quantity is strictly
positive (an unmeasurable call writes no event rather than a zero), the key must match the
event's own fields, and a `UsagePeriod` is a self-describing half-open window.
"""
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.app.domain.entitlement import EntitlementKey
from backend.app.domain.identifiers import usage_event_id
from backend.app.domain.usage import (
    UsageSourceType,
    build_usage_idempotency_key,
    calendar_month_period,
)
from tests.v2_builders import RUN, a_usage_event


def test_the_idempotency_key_is_a_pure_function_of_the_consumption() -> None:
    key = build_usage_idempotency_key(
        entitlement_key=EntitlementKey.LLM_TOKENS,
        source_type=UsageSourceType.LLM_RUN, source_id=str(RUN))
    assert key == build_usage_idempotency_key(
        entitlement_key=EntitlementKey.LLM_TOKENS,
        source_type=UsageSourceType.LLM_RUN, source_id=str(RUN))


def test_two_events_for_one_source_share_an_id_so_re_metering_collapses() -> None:
    """The whole idempotent-metering story: same source → same key → same primary key (§9)."""
    first = a_usage_event()
    again = a_usage_event()  # same defaults: same source, same key
    assert first.id == again.id
    assert first.idempotency_key == again.idempotency_key


def test_two_distinct_sources_are_two_events() -> None:
    one = a_usage_event(source_id="application-a")
    two = a_usage_event(source_id="application-b")
    assert one.id != two.id
    assert one.idempotency_key != two.idempotency_key


def test_a_usage_event_id_derives_from_its_idempotency_key() -> None:
    event = a_usage_event()
    assert event.id == usage_event_id(event.idempotency_key)


def test_quantity_must_be_strictly_positive() -> None:
    """An unmeasurable call writes no event at all rather than a fabricated zero (§5)."""
    with pytest.raises(ValidationError):
        a_usage_event(quantity=0)
    with pytest.raises(ValidationError):
        a_usage_event(quantity=-1)


def test_a_mismatched_idempotency_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="does not match the event's"):
        a_usage_event(idempotency_key="LLM_TOKENS:LLM_RUN:not-the-real-source")


def test_a_token_event_carries_its_measured_count_as_quantity() -> None:
    event = a_usage_event(
        entitlement_key=EntitlementKey.LLM_TOKENS,
        source_type=UsageSourceType.LLM_RUN, source_id=str(RUN), quantity=4096)
    assert event.entitlement_key is EntitlementKey.LLM_TOKENS
    assert event.quantity == 4096


def test_the_source_type_vocabulary_is_closed() -> None:
    assert {s.value for s in UsageSourceType} == {
        "LLM_RUN", "DOCUMENT_VERSION", "APPLICATION_SUBMISSION",
        "INTERVIEW_SESSION", "CAREER_RECOMMENDATION"}


def test_the_calendar_month_period_is_a_labelled_half_open_window() -> None:
    period = calendar_month_period(datetime(2026, 3, 15, 12, 0, tzinfo=UTC))
    assert period.label == "2026-03"
    assert period.start == datetime(2026, 3, 1, tzinfo=UTC)
    assert period.end == datetime(2026, 4, 1, tzinfo=UTC)
    assert period.contains(datetime(2026, 3, 1, tzinfo=UTC))
    assert period.contains(datetime(2026, 3, 31, 23, 59, tzinfo=UTC))
    # Half-open: the first instant of April belongs to the next period, not this one.
    assert not period.contains(datetime(2026, 4, 1, tzinfo=UTC))


def test_the_december_month_rolls_over_the_year() -> None:
    period = calendar_month_period(datetime(2026, 12, 20, tzinfo=UTC))
    assert period.label == "2026-12"
    assert period.end == datetime(2027, 1, 1, tzinfo=UTC)


def test_a_usage_period_must_run_forward() -> None:
    from backend.app.domain.usage import UsagePeriod
    with pytest.raises(ValidationError, match="end must follow its start"):
        UsagePeriod(start=datetime(2026, 3, 1, tzinfo=UTC),
                    end=datetime(2026, 3, 1, tzinfo=UTC), label="2026-03")
