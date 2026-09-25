"""Writing the telemetry row for every call the router runs (§12, §56).

The router chooses a provider and runs it; the recorder is what turns that into the
`LLMRun` an operator later reads — provider, model, tokens, cost, latency, purpose,
and how it ended. It wraps `LLMRouter.route` rather than living inside it, for the
same reason the router holds no repository: routing is a pure decision over a
registry, and persistence is a side effect a service owns. A caller that wants
telemetry routes *through* the recorder; one that does not (a unit test of routing,
say) calls the router directly.

Three properties this boundary holds:

- **Latency is measured on a monotonic clock, timestamps on the wall clock.** A
  wall-clock adjustment mid-call (an NTP step) must not make a 200 ms call report a
  negative latency, so the duration comes from `time.monotonic` while `started_at`
  and `finished_at` come from the injected `clock`. The two are independent readings
  of the same call, each from the source that cannot lie about it.
- **The unknown stays null (§58).** Tokens and cost are copied straight off the
  response's `TokenUsage`, which is already null where a provider reported nothing —
  the recorder never substitutes a 0.
- **A failure is recorded, typed and secret-free, then re-raised.** The recorder does
  not swallow the error: it writes the run and lets the exception continue, so a
  caller's own failure handling is unchanged by telemetry being on. The detail comes
  off the already-redacted `LLMError`, never a raw provider message (§61).

A call the router *refused* before trying any provider (`NoProviderAvailable`, when
nothing is eligible) is not an LLM call and gets no run — there was no provider to
attribute one to. Everything that reached a provider, succeeded or failed, is
recorded exactly once.
"""
import time
from collections.abc import Callable, Sequence
from datetime import datetime

from backend.app.domain.identifiers import UserId, new_llm_run_id
from backend.app.llm.connection import LLMConnection
from backend.app.llm.contracts import (
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMStreamGen,
    StreamEventType,
    TokenUsage,
)
from backend.app.llm.factory import connection_provider_key
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.router import LLMRouter, RoutingOutcome, RoutingPolicy
from backend.app.llm.telemetry import LLMRun, LLMRunStatus
from backend.app.repositories.contracts import LLMRunRepository

Clock = Callable[[], datetime]
Monotonic = Callable[[], float]


def utc_now() -> datetime:
    from datetime import UTC
    return datetime.now(UTC)


# Which terminal status a failure code records. A timeout and a cancellation are told
# apart from an ordinary failure because they are different operational facts (§57): a
# deadline is not a refusal, and a client that hung up is not the provider's fault and
# is not counted against it. Everything else is a plain FAILED.
_STATUS_BY_CODE: dict[LLMFailureCode, LLMRunStatus] = {
    LLMFailureCode.PROVIDER_TIMEOUT: LLMRunStatus.TIMEOUT,
    LLMFailureCode.PROVIDER_CANCELLED: LLMRunStatus.CANCELLED,
}


class LLMTelemetryRecorder:
    """Runs a request through the router and writes one `LLMRun` for the call (§12).

    Holds the run repository it writes to and the connections a request may use — the
    same list `bootstrap` built the registry from — so the serving `provider_key` can
    be resolved back to the connection that produced it, giving the run its
    `connection_id` and `provider_type`. A provider with no backing connection (a
    bootstrap-built default, a probe) resolves to neither, which the nullable columns
    are for.
    """

    def __init__(self, *, runs: LLMRunRepository,
                 connections: Sequence[LLMConnection] = (),
                 clock: Clock = utc_now,
                 monotonic: Monotonic = time.monotonic) -> None:
        self._runs = runs
        self._by_key = {connection_provider_key(c): c for c in connections}
        self._clock = clock
        self._monotonic = monotonic

    async def route(self, router: LLMRouter, request: LLMRequest,
                    policy: RoutingPolicy, *,
                    user_id: UserId | None = None) -> RoutingOutcome:
        """Route the request, record the call, and return the outcome (or re-raise).

        On success a SUCCEEDED run is written carrying the winning provider, its
        measured usage and, when the winner was a fallback, the key it fell back from.
        On a provider failure the run is written with the failure's typed code and the
        primary provider it was attributed to, then the `LLMError` is re-raised
        unchanged. A refusal with no eligible provider writes nothing.
        """
        candidates = router.candidates(request, policy)
        primary_key = candidates[0].metadata.provider_key if candidates else None
        started_at = self._clock()
        start = self._monotonic()
        try:
            outcome = await router.route(request, policy)
        except LLMError as error:
            finished_at = self._clock()
            if primary_key is not None:
                await self._record_failure(
                    request, error, user_id=user_id, provider_key=primary_key,
                    started_at=started_at, finished_at=finished_at,
                    latency_ms=self._elapsed_ms(start))
            raise
        finished_at = self._clock()
        await self._record_success(
            request, outcome, user_id=user_id, started_at=started_at,
            finished_at=finished_at, latency_ms=self._elapsed_ms(start))
        return outcome

    def stream(self, router: LLMRouter, request: LLMRequest, policy: RoutingPolicy, *,
               user_id: UserId | None = None) -> "RecordedStream":
        """Wrap `router.stream` so one `LLMRun` is written when the stream ends.

        Returns a `RecordedStream`: the caller async-iterates it to forward each event,
        and the recorder — on that same pass — accumulates the call and writes exactly one
        run at the terminal event (SUCCEEDED on COMPLETED, FAILED/TIMEOUT on ERROR). A
        refusal with no eligible provider raises `NoProviderAvailable` out of the iteration
        and writes nothing, the same as `route`. The recorder holds no per-call state — the
        `RecordedStream` does — so one recorder can drive many concurrent streams.
        """
        return RecordedStream(self, router, request, policy, user_id=user_id)

    def _elapsed_ms(self, start: float) -> int:
        """Whole milliseconds since `start`, never negative — a monotonic reading."""
        return max(0, round((self._monotonic() - start) * 1000))

    async def _record_success(self, request: LLMRequest, outcome: RoutingOutcome, *,
                              user_id: UserId | None, started_at: datetime,
                              finished_at: datetime, latency_ms: int) -> None:
        connection = self._by_key.get(outcome.provider_key)
        usage = outcome.response.usage
        # Mint the id first so it can be both the run's key and the outcome's `run_id` —
        # a caller reads exact provenance straight off the outcome it already holds, never
        # a "latest run for this user" query that another concurrent call could win.
        run_id = new_llm_run_id()
        await self._runs.upsert(LLMRun(
            id=run_id,
            user_id=user_id,
            connection_id=connection.id if connection else None,
            provider_key=outcome.provider_key,
            provider_type=connection.provider_type if connection else None,
            model=outcome.response.model or request.model,
            purpose=request.purpose,
            status=LLMRunStatus.SUCCEEDED,
            prompt_name=request.prompt_name,
            prompt_version=request.prompt_version,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cost_usd=usage.cost_usd,
            latency_ms=latency_ms,
            fallback_from=outcome.fallback_from,
            fallback_reason=outcome.fallback_reason,
            started_at=started_at,
            finished_at=finished_at))
        outcome.run_id = run_id

    async def _record_failure(self, request: LLMRequest, error: LLMError, *,
                             user_id: UserId | None, provider_key: str,
                             started_at: datetime, finished_at: datetime,
                             latency_ms: int) -> None:
        connection = self._by_key.get(provider_key)
        status = _STATUS_BY_CODE.get(error.code, LLMRunStatus.FAILED)
        # A CANCELLED run is not a failure, so it carries no failure_code — the status
        # already says what happened, and the model's validator refuses a code on it.
        carries_code = status in (LLMRunStatus.FAILED, LLMRunStatus.TIMEOUT)
        await self._runs.upsert(LLMRun(
            id=new_llm_run_id(),
            user_id=user_id,
            connection_id=connection.id if connection else None,
            provider_key=provider_key,
            provider_type=connection.provider_type if connection else None,
            model=request.model,
            purpose=request.purpose,
            status=status,
            prompt_name=request.prompt_name,
            prompt_version=request.prompt_version,
            latency_ms=latency_ms,
            failure_code=error.code if carries_code else None,
            failure_detail=error.detail,
            started_at=started_at,
            finished_at=finished_at))

    async def _record_stream(self, request: LLMRequest, *,
                             user_id: UserId | None, provider_key: str,
                             response: LLMResponse | None,
                             error: LLMStreamEvent | None, usage: TokenUsage,
                             started_at: datetime, finished_at: datetime,
                             latency_ms: int) -> LLMRun:
        """Write the one run a finished stream earns — success, failure or protocol error.

        A COMPLETED stream is a SUCCEEDED run carrying the terminal response's usage and
        model (falling back to the deltas' usage only when the response reported none). A
        terminal ERROR is a FAILED/TIMEOUT run carrying the event's typed code and already
        secret-free detail. A stream that ended with neither — a provider whose output
        stopped mid-flight — is a `PROVIDER_PROTOCOL_ERROR` FAILED run, so a call that
        reached a provider is always recorded exactly once, never dropped.
        """
        connection = self._by_key.get(provider_key)
        connection_id = connection.id if connection else None
        provider_type = connection.provider_type if connection else None
        if response is not None:
            final = response.usage if response.usage != TokenUsage() else usage
            return await self._runs.upsert(LLMRun(
                id=new_llm_run_id(), user_id=user_id, connection_id=connection_id,
                provider_key=provider_key, provider_type=provider_type,
                model=response.model or request.model, purpose=request.purpose,
                status=LLMRunStatus.SUCCEEDED,
                prompt_name=request.prompt_name, prompt_version=request.prompt_version,
                prompt_tokens=final.prompt_tokens,
                completion_tokens=final.completion_tokens,
                total_tokens=final.total_tokens, cost_usd=final.cost_usd,
                latency_ms=latency_ms, started_at=started_at, finished_at=finished_at))
        code, detail = _stream_failure(error)
        status = _STATUS_BY_CODE.get(code, LLMRunStatus.FAILED)
        carries_code = status in (LLMRunStatus.FAILED, LLMRunStatus.TIMEOUT)
        return await self._runs.upsert(LLMRun(
            id=new_llm_run_id(), user_id=user_id, connection_id=connection_id,
            provider_key=provider_key, provider_type=provider_type,
            model=request.model, purpose=request.purpose, status=status,
            prompt_name=request.prompt_name, prompt_version=request.prompt_version,
            latency_ms=latency_ms,
            failure_code=code if carries_code else None, failure_detail=detail,
            started_at=started_at, finished_at=finished_at))


class RecordedStream:
    """A router stream with a telemetry row riding along — iterate it, then read its run.

    `LLMTelemetryRecorder.stream` returns one of these. Async-iterating it forwards every
    `LLMStreamEvent` exactly as the provider produced it (to an SSE response, say); the
    recorder, on that same pass, accumulates the call and writes exactly one `LLMRun` when
    the stream ends — SUCCEEDED on the terminal COMPLETED, FAILED or TIMEOUT on a terminal
    ERROR, and nothing when the router refused before any provider ran (a
    `NoProviderAvailable` propagates out of the iteration, un-recorded, exactly as an empty
    route writes no run).

    After the iteration the caller reads what the recorder captured: `run` (the row
    written, `None` if none was), `text` (the concatenated prose) and `external_session_id`
    (the provider's resume handle, when it issued one) — so a caller can stamp its own
    record (a chat message's `llm_run_id`/`provider_key`) without re-deriving them.

    The run attributes to the *first* eligible provider's key — the same primary the
    failure path of `route` attributes to. The streaming router does not surface which
    provider ultimately served after a before-first-byte fallback, so the primary is the
    honest best-effort, and a fallback is never falsely credited to its target.
    """

    def __init__(self, recorder: LLMTelemetryRecorder, router: LLMRouter,
                 request: LLMRequest, policy: RoutingPolicy, *,
                 user_id: UserId | None) -> None:
        self._recorder = recorder
        self._router = router
        self._request = request
        self._policy = policy
        self._user_id = user_id
        self.run: LLMRun | None = None
        self.text: str = ""
        self.external_session_id: str | None = None

    async def __aiter__(self) -> LLMStreamGen:
        """Yield each event, then write the run — the recorder's whole streaming footprint.

        The clock and monotonic readings bracket the *whole* stream, so latency is the
        wall time the user waited for the answer, measured on the monotonic clock the same
        way `route` measures a one-shot call. `NoProviderAvailable` from an empty route is
        raised by the first `__anext__` below and propagates out un-recorded.
        """
        candidates = self._router.candidates(self._request, self._policy)
        primary_key = candidates[0].metadata.provider_key if candidates else None
        started_at = self._recorder._clock()
        start = self._recorder._monotonic()
        parts: list[str] = []
        usage = TokenUsage()
        response: LLMResponse | None = None
        error: LLMStreamEvent | None = None
        async for event in self._router.stream(self._request, self._policy):
            if event.type is StreamEventType.TEXT_DELTA and event.text is not None:
                parts.append(event.text)
            elif event.type is StreamEventType.USAGE and event.usage is not None:
                usage = event.usage
            elif event.type is StreamEventType.COMPLETED:
                response = event.response
            elif event.type is StreamEventType.ERROR:
                error = event
            if event.external_session_id is not None:
                self.external_session_id = event.external_session_id
            yield event
        self.text = "".join(parts)
        if primary_key is not None:
            self.run = await self._recorder._record_stream(
                self._request, user_id=self._user_id, provider_key=primary_key,
                response=response, error=error, usage=usage, started_at=started_at,
                finished_at=self._recorder._clock(),
                latency_ms=self._recorder._elapsed_ms(start))


def _stream_failure(event: LLMStreamEvent | None) -> tuple[LLMFailureCode, str]:
    """The typed code and secret-free detail a terminal ERROR (or a broken stream) records.

    A stream that ended without a terminal event is a protocol error the provider owes us
    an explanation for and did not give — recorded as `PROVIDER_PROTOCOL_ERROR` rather
    than dropped. An ERROR event's code is mapped back to the closed `LLMFailureCode` set,
    defaulting to `PROVIDER_INTERNAL_ERROR` for anything unrecognised; its detail is
    already the layer's own redacted sentence, so it is surfaced as-is (an absent one falls
    back to the code's table entry).
    """
    if event is None:
        error = LLMError(LLMFailureCode.PROVIDER_PROTOCOL_ERROR)
        return error.code, error.detail
    try:
        code = LLMFailureCode(event.error_code or "")
    except ValueError:
        code = LLMFailureCode.PROVIDER_INTERNAL_ERROR
    detail = event.error_detail if event.error_detail is not None else LLMError(code).detail
    return code, detail

