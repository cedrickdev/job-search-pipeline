# tests/test_v2_llm_token_reservation.py
"""LLM_TOKENS as a real reserve/finalize quota, over fakes (Fix #2: §5-9, §58).

`test_v2_entitlement_enforcement.py` proves an already-spent LLM period is refused before the
router runs. These tests pin the reserve/finalize *mechanics* the fix adds:

- **the reservation is a deterministic pre-call budget, not a flat one** — an estimate of the
  request's input plus its output bound, so an account without room for the *estimated* cost is
  refused before a provider is spent, rather than the old `quantity=1` that could authorize a call
  and only overshoot afterward;
- **the ledger records the measured actual, not the reservation** — an over-estimate releases its
  unused remainder simply by never being written, so the authoritative usage is what the provider
  reported, and the Phase 11 `LLMRun` telemetry stays the source of that measurement;
- **the unknown stays unknown (§58)** — a provider that reports no `total_tokens` meters nothing,
  never a fabricated zero, even though the call succeeded and its run was written.

The real-PostgreSQL race — two concurrent calls competing for the last of a token budget, where
the advisory lock must let exactly one through — lives in `test_v2_persistence_usage_concurrency.py`
alongside its submission-budget twin, because a cross-connection lock cannot be shown over fakes.
"""
import pytest

from backend.app.billing.catalogue import FREE_PLAN_SLUG
from backend.app.billing.entitlements import EntitlementResolver
from backend.app.billing.errors import BillingError, BillingErrorCode
from backend.app.billing.metering import MeteringService
from backend.app.domain.entitlement import EntitlementKey
from backend.app.domain.usage import UsageSourceType
from backend.app.llm.contracts import LLMMessage, TokenUsage
from backend.app.llm.recorder import (
    _DEFAULT_OUTPUT_TOKEN_RESERVATION,
    LLMTelemetryRecorder,
    _token_reservation,
)
from backend.app.llm.telemetry import LLMRunStatus
from tests.test_v2_llm_recorder import _EXTERNAL, _FakeMonotonic, _clock, _request, _router
from tests.v2_builders import USER, a_plan, a_usage_event, an_entitlement
from tests.v2_fakes import (
    FakeLLMRunRepository,
    FakePlanRepository,
    FakeSubscriptionRepository,
    FakeUsageEventRepository,
)
from tests.v2_llm import FakeProvider

pytestmark = pytest.mark.asyncio


def _metering_with_ledger(*entitlements, used=()):
    """A free-tier `MeteringService` plus the usage ledger it writes to, so a test can read it back.

    The exact shape of `_free_tier` in the enforcement tests, but it also returns the
    `FakeUsageEventRepository` — the reserve/finalize tests need to inspect *what* was recorded (the
    measured actual, never the reservation), which `_free_tier` keeps private.
    """
    plan = a_plan(
        slug=FREE_PLAN_SLUG, price_amount_cents=None, currency=None, billing_interval=None,
        external_price_id=None, entitlements=tuple(entitlements))
    plans = FakePlanRepository()
    plans.plans[plan.id] = plan
    usage = FakeUsageEventRepository()
    for event in used:
        usage.events[event.id] = event
    return MeteringService(EntitlementResolver(plans, FakeSubscriptionRepository()), usage), usage


def _recorder(metering, runs):
    return LLMTelemetryRecorder(
        runs=runs, clock=_clock, monotonic=_FakeMonotonic(), metering=metering)


# ------------------------------------------------------------------ the reservation estimate


async def test_the_reservation_is_the_input_estimate_plus_the_pinned_output_bound() -> None:
    """A deterministic budget: characters rounded up through the divisor, plus the output bound.

    12 characters of message content and 4 of system are 16 characters → four input tokens at the
    coarse four-chars-per-token divisor; the pinned `max_output_tokens` is added verbatim. The
    estimate is provider-neutral and never claims to be the exact input token count.
    """
    request = _request(
        messages=(LLMMessage.user("123456789012"),), system="abcd", max_output_tokens=250)
    assert _token_reservation(request) == 4 + 250


async def test_the_reservation_falls_back_to_the_default_output_budget() -> None:
    """With no `max_output_tokens`, the generous default output budget stands in for the bound."""
    request = _request(messages=(LLMMessage.user(""),))  # no content, no pinned output bound
    assert _token_reservation(request) == _DEFAULT_OUTPUT_TOKEN_RESERVATION


# ------------------------------------------------------------------ reserve before the provider


async def test_a_call_is_refused_when_the_reservation_does_not_fit_the_remaining_budget() -> None:
    """49,900 of 50,000 used, a reservation of 500 → refused before the provider is ever asked.

    The remaining 100 tokens cannot cover the call's estimated 500, so `authorize` refuses with
    `QUOTA_EXCEEDED` ahead of the router — no provider is spent and no telemetry row is written.
    Under the old flat `quantity=1` this call would have been authorized (one unit fits) and only
    discovered its overshoot after spending the tokens; the reservation is what prevents that.
    """
    metering, usage = _metering_with_ledger(
        an_entitlement(key=EntitlementKey.LLM_TOKENS, limit=50_000),
        used=(a_usage_event(
            entitlement_key=EntitlementKey.LLM_TOKENS, source_type=UsageSourceType.LLM_RUN,
            source_id="seed", quantity=49_900),))
    provider = FakeProvider(provider_key="conn_gateway", usage=TokenUsage(total_tokens=10))
    runs = FakeLLMRunRepository()
    # empty content → 0 input tokens; max_output_tokens=500 → a reservation of exactly 500.
    request = _request(messages=(LLMMessage.user(""),), max_output_tokens=500)
    assert _token_reservation(request) == 500

    with pytest.raises(BillingError) as caught:
        await _recorder(metering, runs).route(_router(provider), request, _EXTERNAL, user_id=USER)
    assert caught.value.code is BillingErrorCode.QUOTA_EXCEEDED
    assert provider.calls == 0  # no provider was spent
    assert runs.runs == {}  # and no telemetry row was written
    # The pre-spent seed is the only event; the refused call wrote nothing.
    assert [e.quantity for e in usage.events.values()] == [49_900]


# ------------------------------------------------------------------ finalize the measured actual


async def test_the_ledger_records_the_measured_actual_not_the_reservation() -> None:
    """Reserve 1,000, spend 420 → the authoritative usage is 420; the unused 580 is released.

    The reservation only gates the call; what is *recorded* is the provider's measured
    `total_tokens`, so the ledger never carries the over-estimate. The written run carries the same
    measured total — the Phase 11 telemetry stays the source of the metered figure.
    """
    metering, usage = _metering_with_ledger(
        an_entitlement(key=EntitlementKey.LLM_TOKENS, limit=50_000))
    provider = FakeProvider(
        provider_key="conn_gateway", text="ok",
        usage=TokenUsage(prompt_tokens=100, completion_tokens=320, total_tokens=420))
    runs = FakeLLMRunRepository()
    request = _request(messages=(LLMMessage.user(""),), max_output_tokens=1000)
    assert _token_reservation(request) == 1000  # the reservation is 1,000 …

    outcome = await _recorder(metering, runs).route(
        _router(provider), request, _EXTERNAL, user_id=USER)

    (run,) = runs.runs.values()
    assert run.status is LLMRunStatus.SUCCEEDED
    assert run.total_tokens == 420  # … but the run carries the measured 420 …
    events = [e for e in usage.events.values()
              if e.entitlement_key is EntitlementKey.LLM_TOKENS]
    assert len(events) == 1  # … and exactly one event was written …
    assert events[0].quantity == 420  # … for the measured actual, not the 1,000 reserved.
    assert events[0].source_id == str(outcome.run_id)  # metered against the run it measured


async def test_an_unmeasured_call_meters_nothing_rather_than_a_fabricated_zero() -> None:
    """A provider that reports no `total_tokens` succeeds, is recorded as a run, but meters nothing.

    The unknown stays null (§58): the call routed, a SUCCEEDED run was written with an unknown token
    total, and *no* usage event was recorded — the honest record of an unmeasurable call, never a
    fabricated zero that would silently consume nothing yet clutter the ledger.
    """
    metering, usage = _metering_with_ledger(
        an_entitlement(key=EntitlementKey.LLM_TOKENS, limit=50_000))
    provider = FakeProvider(provider_key="conn_gateway", text="ok", usage=TokenUsage())
    runs = FakeLLMRunRepository()

    await _recorder(metering, runs).route(
        _router(provider), _request(), _EXTERNAL, user_id=USER)

    (run,) = runs.runs.values()
    assert run.status is LLMRunStatus.SUCCEEDED
    assert run.total_tokens is None  # the provider reported nothing …
    assert usage.events == {}  # … so nothing was metered, not a fabricated zero
