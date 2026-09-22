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
from backend.app.llm.contracts import LLMRequest
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

    def _elapsed_ms(self, start: float) -> int:
        """Whole milliseconds since `start`, never negative — a monotonic reading."""
        return max(0, round((self._monotonic() - start) * 1000))

    async def _record_success(self, request: LLMRequest, outcome: RoutingOutcome, *,
                              user_id: UserId | None, started_at: datetime,
                              finished_at: datetime, latency_ms: int) -> None:
        connection = self._by_key.get(outcome.provider_key)
        usage = outcome.response.usage
        await self._runs.upsert(LLMRun(
            id=new_llm_run_id(),
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
