# tests/test_v2_llm_recorder_stream.py
"""What the recorder writes when a call *streams* — one run, at the stream's end.

`LLMTelemetryRecorder.stream` returns a `RecordedStream` a caller async-iterates: it
forwards every event exactly as the provider produced it and, on that same pass, writes
exactly one `LLMRun` when the stream ends — SUCCEEDED on the terminal COMPLETED,
FAILED/TIMEOUT on a terminal ERROR, CANCELLED without a failure code, and a
PROVIDER_PROTOCOL_ERROR when a provider's output stopped mid-flight. These tests pin each
outcome, plus the two facts a chat turn later stamps onto its message — the accumulated
`text` and the provider's `external_session_id` — and the attribution rule a
before-first-byte fallback follows (the primary key, honest best-effort, never a false
credit to the fallback target). They mirror the one-shot recorder tests: `FakeProvider`s
for the common shapes, a hand-advanced monotonic clock so latency is asserted.
"""
from datetime import UTC, datetime

import pytest

from backend.app.llm.capabilities import BASELINE_CAPABILITY, Capability
from backend.app.llm.contracts import (
    FinishReason,
    LLMMessage,
    LLMProviderMetadata,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMStreamGen,
    ProviderTransport,
    StreamEventType,
    TaskPurpose,
    TokenUsage,
)
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.recorder import LLMTelemetryRecorder
from backend.app.llm.registry import LLMProviderRegistry
from backend.app.llm.router import LLMRouter, NoProviderAvailable, PrivacyClass, RoutingPolicy
from backend.app.llm.telemetry import LLMRunStatus
from tests.v2_builders import USER
from tests.v2_fakes import FakeLLMRunRepository
from tests.v2_llm import FakeProvider

pytestmark = pytest.mark.asyncio

_EXTERNAL = RoutingPolicy(privacy=PrivacyClass.EXTERNAL_ALLOWED)
_FALLBACK = RoutingPolicy(privacy=PrivacyClass.EXTERNAL_ALLOWED, allow_fallback=True)


def _request(**overrides) -> LLMRequest:
    fields = {"messages": (LLMMessage.user("hi"),)}
    fields.update(overrides)
    return LLMRequest(**fields)


def _router(*providers) -> LLMRouter:
    registry = LLMProviderRegistry()
    registry.register_all(providers)
    return LLMRouter(registry)


def _recorder(runs: FakeLLMRunRepository, **kwargs) -> LLMTelemetryRecorder:
    return LLMTelemetryRecorder(runs=runs, clock=_clock,
                                monotonic=_FakeMonotonic(), **kwargs)


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


class _ScriptedProvider:
    """A provider that replays a fixed event list — for stream shapes `FakeProvider` can't.

    `FakeProvider.stream` only produces the two well-behaved shapes (text+COMPLETED, or a
    pre-content ERROR). A protocol error (no terminal event), a USAGE event the response
    then omits, or an `external_session_id` on the wire need an exact script, which this
    replays verbatim.
    """

    def __init__(self, provider_key: str, events: list[LLMStreamEvent], *,
                 local: bool = False, priority: int = 100) -> None:
        caps = {BASELINE_CAPABILITY}
        if local:
            caps.add(Capability.LOCAL_EXECUTION)
        self._metadata = LLMProviderMetadata(
            provider_key=provider_key, display_name="Scripted",
            transport=ProviderTransport.OPENAI_COMPATIBLE_API,
            capabilities=frozenset(caps), default_model="scripted-model",
            priority=priority)
        self._events = events

    @property
    def metadata(self) -> LLMProviderMetadata:
        return self._metadata

    async def stream(self, request: LLMRequest) -> LLMStreamGen:
        for event in self._events:
            yield event

    async def generate(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise NotImplementedError

    async def healthcheck(self):  # pragma: no cover
        raise NotImplementedError


async def _drain(stream) -> list[LLMStreamEvent]:
    """Iterate the whole stream, returning the events it forwarded."""
    return [event async for event in stream]


# --- the happy path: a completed stream is one SUCCEEDED run ----------------

async def test_a_streamed_success_records_a_succeeded_run():
    provider = FakeProvider(
        provider_key="conn_gateway", text="hello there", model="fake-model",
        usage=TokenUsage(prompt_tokens=8, completion_tokens=4, total_tokens=12,
                         cost_usd=0.02))
    runs = FakeLLMRunRepository()
    recorder = _recorder(runs)
    stream = recorder.stream(_router(provider),
                             _request(purpose=TaskPurpose.CAREER_CHAT),
                             _EXTERNAL, user_id=USER)
    events = await _drain(stream)
    assert events[0].type is StreamEventType.STARTED
    assert events[-1].type is StreamEventType.COMPLETED
    assert stream.text == "hello there"
    (run,) = runs.runs.values()
    assert run is stream.run
    assert run.status is LLMRunStatus.SUCCEEDED
    assert run.provider_key == "conn_gateway"
    assert run.user_id == USER
    assert run.purpose is TaskPurpose.CAREER_CHAT
    assert run.model == "fake-model"
    assert (run.prompt_tokens, run.completion_tokens, run.total_tokens) == (8, 4, 12)
    assert run.cost_usd == 0.02
    assert run.failure_code is None


async def test_streamed_latency_is_measured_on_the_monotonic_clock():
    provider = FakeProvider(provider_key="conn_gateway")
    runs = FakeLLMRunRepository()
    stream = _recorder(runs).stream(_router(provider), _request(), _EXTERNAL,
                                    user_id=USER)
    await _drain(stream)
    (run,) = runs.runs.values()
    # start=1000.0, next reading=1000.25 → 250 ms, from the monotonic source and not the
    # frozen wall clock, exactly as the one-shot path measures it.
    assert run.latency_ms == 250
    assert run.started_at == _clock()


async def test_the_external_session_id_is_captured_for_the_caller_to_stamp():
    provider = _ScriptedProvider("conn_gateway", [
        LLMStreamEvent.started(external_session_id="sess-abc"),
        LLMStreamEvent.text_delta("hi"),
        LLMStreamEvent.completed(LLMResponse(
            text="hi", usage=TokenUsage(), finish_reason=FinishReason.STOP,
            model="scripted-model"))])
    runs = FakeLLMRunRepository()
    stream = _recorder(runs).stream(_router(provider), _request(), _EXTERNAL,
                                    user_id=USER)
    await _drain(stream)
    assert stream.external_session_id == "sess-abc"


async def test_usage_falls_back_to_the_usage_event_when_the_response_omits_it():
    reported = TokenUsage(prompt_tokens=3, completion_tokens=7, total_tokens=10)
    provider = _ScriptedProvider("conn_gateway", [
        LLMStreamEvent.started(),
        LLMStreamEvent.text_delta("answer"),
        LLMStreamEvent(type=StreamEventType.USAGE, usage=reported),
        LLMStreamEvent.completed(LLMResponse(
            text="answer", usage=TokenUsage(), finish_reason=FinishReason.STOP,
            model="scripted-model"))])
    runs = FakeLLMRunRepository()
    stream = _recorder(runs).stream(_router(provider), _request(), _EXTERNAL,
                                    user_id=USER)
    await _drain(stream)
    (run,) = runs.runs.values()
    assert run.status is LLMRunStatus.SUCCEEDED
    assert (run.prompt_tokens, run.completion_tokens, run.total_tokens) == (3, 7, 10)


# --- a terminal ERROR is a run, not an exception ----------------------------

async def test_a_streamed_error_records_a_failed_run_without_raising():
    """A terminal ERROR event is forwarded and recorded — the iteration does not raise."""
    provider = FakeProvider(provider_key="conn_gateway",
                            error=LLMError(LLMFailureCode.PROVIDER_INTERNAL_ERROR))
    runs = FakeLLMRunRepository()
    stream = _recorder(runs).stream(_router(provider), _request(), _EXTERNAL,
                                    user_id=USER)
    events = await _drain(stream)  # no raise: the ERROR is a normal terminal event
    assert events[-1].type is StreamEventType.ERROR
    (run,) = runs.runs.values()
    assert run.status is LLMRunStatus.FAILED
    assert run.failure_code is LLMFailureCode.PROVIDER_INTERNAL_ERROR
    assert run.provider_key == "conn_gateway"


async def test_a_streamed_timeout_records_the_timeout_status():
    provider = FakeProvider(provider_key="conn_gateway",
                            error=LLMError(LLMFailureCode.PROVIDER_TIMEOUT))
    runs = FakeLLMRunRepository()
    stream = _recorder(runs).stream(_router(provider), _request(), _EXTERNAL,
                                    user_id=USER)
    await _drain(stream)
    (run,) = runs.runs.values()
    assert run.status is LLMRunStatus.TIMEOUT
    assert run.failure_code is LLMFailureCode.PROVIDER_TIMEOUT


async def test_a_streamed_cancellation_records_no_failure_code():
    """A client that hung up is CANCELLED, and the model refuses a code on it (§57)."""
    provider = FakeProvider(provider_key="conn_gateway",
                            error=LLMError(LLMFailureCode.PROVIDER_CANCELLED))
    runs = FakeLLMRunRepository()
    stream = _recorder(runs).stream(_router(provider), _request(), _EXTERNAL,
                                    user_id=USER)
    await _drain(stream)
    (run,) = runs.runs.values()
    assert run.status is LLMRunStatus.CANCELLED
    assert run.failure_code is None


async def test_a_stream_that_stops_mid_flight_is_a_protocol_error():
    """A provider whose output ends with no terminal event owes an explanation it did
    not give — recorded as PROVIDER_PROTOCOL_ERROR, never dropped."""
    provider = _ScriptedProvider("conn_gateway", [
        LLMStreamEvent.started(),
        LLMStreamEvent.text_delta("half an ans")])  # …and then nothing
    runs = FakeLLMRunRepository()
    stream = _recorder(runs).stream(_router(provider), _request(), _EXTERNAL,
                                    user_id=USER)
    await _drain(stream)
    assert stream.text == "half an ans"
    (run,) = runs.runs.values()
    assert run.status is LLMRunStatus.FAILED
    assert run.failure_code is LLMFailureCode.PROVIDER_PROTOCOL_ERROR


# --- refusal and fallback ---------------------------------------------------

async def test_a_refusal_with_no_eligible_provider_streams_and_records_nothing():
    """No provider ran, so `NoProviderAvailable` propagates and no run is written."""
    provider = FakeProvider(provider_key="conn_gateway", local=False)
    runs = FakeLLMRunRepository()
    stream = _recorder(runs).stream(_router(provider), _request(),
                                    RoutingPolicy(privacy=PrivacyClass.LOCAL_ONLY),
                                    user_id=USER)
    with pytest.raises(NoProviderAvailable):
        await _drain(stream)
    assert runs.runs == {}
    assert stream.run is None


async def test_a_before_first_byte_fallback_attributes_to_the_primary_key():
    """The streaming router does not surface who served after a pre-content fallback, so
    the run credits the primary — honest best-effort, never a false credit to the target."""
    primary = FakeProvider(provider_key="conn_primary", priority=10,
                           error=LLMError(LLMFailureCode.PROVIDER_UNAVAILABLE))
    backup = FakeProvider(provider_key="conn_backup", priority=20, text="from backup")
    runs = FakeLLMRunRepository()
    stream = _recorder(runs).stream(_router(primary, backup), _request(), _FALLBACK,
                                    user_id=USER)
    await _drain(stream)
    assert stream.text == "from backup"
    (run,) = runs.runs.values()
    assert run.status is LLMRunStatus.SUCCEEDED
    assert run.provider_key == "conn_primary"  # the primary, not the server


async def test_one_recorder_drives_two_streams_without_crossing_state():
    """The recorder holds no per-call state — the `RecordedStream` does — so concurrent
    streams keep their own text and run."""
    recorder = _recorder(FakeLLMRunRepository())
    one = recorder.stream(_router(FakeProvider(provider_key="a", text="first")),
                          _request(), _EXTERNAL, user_id=USER)
    two = recorder.stream(_router(FakeProvider(provider_key="b", text="second")),
                          _request(), _EXTERNAL, user_id=USER)
    await _drain(one)
    await _drain(two)
    assert (one.text, two.text) == ("first", "second")
    assert one.run is not None and two.run is not None
    assert one.run.provider_key == "a"
    assert two.run.provider_key == "b"


