# tests/test_v2_llm_recorder.py
"""What the telemetry recorder writes for a routed call, and what it never writes.

The recorder wraps `LLMRouter.route` and turns each call into one `LLMRun`: a success
carries the winning provider and its measured usage; a failure carries the typed code
and is re-raised, not swallowed; a fallback carries the key it fell back from; a call
refused before any provider ran is not an LLM call and gets no run. These tests pin
each, driving the router with `FakeProvider`s so the behaviour under test is the
platform's, and a controllable monotonic clock so latency is asserted, not guessed.
"""
from datetime import UTC, datetime

import pytest

from backend.app.llm.contracts import LLMMessage, LLMRequest, TaskPurpose, TokenUsage
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.recorder import LLMTelemetryRecorder
from backend.app.llm.registry import LLMProviderRegistry
from backend.app.llm.router import LLMRouter, NoProviderAvailable, PrivacyClass, RoutingPolicy
from backend.app.llm.telemetry import LLMRunStatus
from tests.v2_builders import CONNECTION, USER, an_llm_connection
from tests.v2_fakes import FakeLLMRunRepository
from tests.v2_llm import FakeProvider

pytestmark = pytest.mark.asyncio

_EXTERNAL = RoutingPolicy(privacy=PrivacyClass.EXTERNAL_ALLOWED)
_FALLBACK = RoutingPolicy(privacy=PrivacyClass.EXTERNAL_ALLOWED, allow_fallback=True)


def _request(**overrides) -> LLMRequest:
    fields = {"messages": (LLMMessage.user("hi"),)}
    fields.update(overrides)
    return LLMRequest(**fields)


def _router(*providers: FakeProvider) -> LLMRouter:
    registry = LLMProviderRegistry()
    registry.register_all(providers)
    return LLMRouter(registry)


class _FakeMonotonic:
    """A monotonic clock a test advances by hand, so latency is deterministic."""

    def __init__(self, *, step: float = 0.25) -> None:
        self._now = 1000.0
        self._step = step

    def __call__(self) -> float:
        value = self._now
        self._now += self._step
        return value


def _clock() -> datetime:
    return datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


async def test_a_successful_call_records_a_succeeded_run():
    provider = FakeProvider(
        provider_key="conn_gateway", text="answer",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15,
                         cost_usd=0.01))
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs, clock=_clock,
                                    monotonic=_FakeMonotonic())
    await recorder.route(_router(provider), _request(purpose=TaskPurpose.RESUME_TAILORING),
                         _EXTERNAL, user_id=USER)
    (run,) = runs.runs.values()
    assert run.status is LLMRunStatus.SUCCEEDED
    assert run.provider_key == "conn_gateway"
    assert run.user_id == USER
    assert run.purpose is TaskPurpose.RESUME_TAILORING
    assert (run.prompt_tokens, run.completion_tokens, run.total_tokens) == (10, 5, 15)
    assert run.cost_usd == 0.01
    assert run.failure_code is None


async def test_latency_is_measured_on_the_monotonic_clock():
    provider = FakeProvider(provider_key="conn_gateway")
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs, clock=_clock,
                                    monotonic=_FakeMonotonic(step=0.25))
    await recorder.route(_router(provider), _request(), _EXTERNAL, user_id=USER)
    (run,) = runs.runs.values()
    # start=1000.0, end reading=1000.25 → 250 ms, from the monotonic source, not the
    # frozen wall clock (which would give 0).
    assert run.latency_ms == 250


async def test_the_serving_connection_gives_the_run_its_identity():
    """A provider backed by a stored connection stamps its id and type on the run."""
    connection = an_llm_connection()  # OPENAI_COMPATIBLE, key conn_<hex>
    from backend.app.llm.factory import connection_provider_key
    provider = FakeProvider(provider_key=connection_provider_key(connection))
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs, connections=[connection],
                                    clock=_clock, monotonic=_FakeMonotonic())
    await recorder.route(_router(provider), _request(), _EXTERNAL, user_id=USER)
    (run,) = runs.runs.values()
    assert run.connection_id == CONNECTION
    assert run.provider_type == connection.provider_type


async def test_a_provider_with_no_backing_connection_records_nulls():
    provider = FakeProvider(provider_key="claude_code")
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs, clock=_clock,
                                    monotonic=_FakeMonotonic())
    await recorder.route(_router(provider), _request(), _EXTERNAL, user_id=USER)
    (run,) = runs.runs.values()
    assert run.connection_id is None
    assert run.provider_type is None
    assert run.provider_key == "claude_code"


async def test_a_failure_is_recorded_then_re_raised():
    error = LLMError(LLMFailureCode.PROVIDER_INTERNAL_ERROR)
    provider = FakeProvider(provider_key="conn_gateway", error=error)
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs, clock=_clock,
                                    monotonic=_FakeMonotonic())
    with pytest.raises(LLMError) as caught:
        await recorder.route(_router(provider), _request(), _EXTERNAL, user_id=USER)
    assert caught.value.code is LLMFailureCode.PROVIDER_INTERNAL_ERROR
    (run,) = runs.runs.values()
    assert run.status is LLMRunStatus.FAILED
    assert run.failure_code is LLMFailureCode.PROVIDER_INTERNAL_ERROR
    assert run.provider_key == "conn_gateway"


async def test_a_timeout_is_recorded_as_the_timeout_status():
    provider = FakeProvider(provider_key="conn_gateway",
                            error=LLMError(LLMFailureCode.PROVIDER_TIMEOUT))
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs, clock=_clock,
                                    monotonic=_FakeMonotonic())
    with pytest.raises(LLMError):
        await recorder.route(_router(provider), _request(), _EXTERNAL, user_id=USER)
    (run,) = runs.runs.values()
    assert run.status is LLMRunStatus.TIMEOUT
    assert run.failure_code is LLMFailureCode.PROVIDER_TIMEOUT


async def test_a_cancellation_is_recorded_without_a_failure_code():
    """A CANCELLED run is not a failure; the model refuses a code on it (§57)."""
    provider = FakeProvider(provider_key="conn_gateway",
                            error=LLMError(LLMFailureCode.PROVIDER_CANCELLED))
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs, clock=_clock,
                                    monotonic=_FakeMonotonic())
    with pytest.raises(LLMError):
        await recorder.route(_router(provider), _request(), _EXTERNAL, user_id=USER)
    (run,) = runs.runs.values()
    assert run.status is LLMRunStatus.CANCELLED
    assert run.failure_code is None


async def test_a_fallback_records_where_it_fell_back_from():
    primary = FakeProvider(
        provider_key="conn_primary", priority=10,
        error=LLMError(LLMFailureCode.PROVIDER_UNAVAILABLE))
    backup = FakeProvider(provider_key="conn_backup", priority=20, text="from backup")
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs, clock=_clock,
                                    monotonic=_FakeMonotonic())
    outcome = await recorder.route(_router(primary, backup), _request(),
                                   _FALLBACK, user_id=USER)
    assert outcome.provider_key == "conn_backup"
    (run,) = runs.runs.values()
    assert run.status is LLMRunStatus.SUCCEEDED
    assert run.provider_key == "conn_backup"
    assert run.fallback_from == "conn_primary"
    assert run.fallback_reason is LLMFailureCode.PROVIDER_UNAVAILABLE


async def test_a_refusal_with_no_eligible_provider_records_nothing():
    """No provider ran, so there is no call to attribute a run to."""
    # A LOCAL_ONLY policy over a single remote provider leaves nothing eligible.
    provider = FakeProvider(provider_key="conn_gateway", local=False)
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs, clock=_clock,
                                    monotonic=_FakeMonotonic())
    policy = RoutingPolicy(privacy=PrivacyClass.LOCAL_ONLY)
    with pytest.raises(NoProviderAvailable):
        await recorder.route(_router(provider), _request(), policy, user_id=USER)
    assert runs.runs == {}


async def test_an_unreported_usage_stays_null_on_the_run():
    provider = FakeProvider(provider_key="conn_gateway", usage=TokenUsage())
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs, clock=_clock,
                                    monotonic=_FakeMonotonic())
    await recorder.route(_router(provider), _request(), _EXTERNAL, user_id=USER)
    (run,) = runs.runs.values()
    assert run.prompt_tokens is None
    assert run.total_tokens is None
    assert run.cost_usd is None


async def test_a_probe_without_a_user_records_an_ownerless_run():
    provider = FakeProvider(provider_key="conn_gateway")
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs, clock=_clock,
                                    monotonic=_FakeMonotonic())
    await recorder.route(_router(provider), _request(), _EXTERNAL)
    (run,) = runs.runs.values()
    assert run.user_id is None
    assert await runs.list_for_user(USER) == ()
