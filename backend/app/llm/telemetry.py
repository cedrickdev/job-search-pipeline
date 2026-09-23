"""One record of one LLM call — what it cost, how it ended, and why (§12, §56).

An `LLMRun` is the audit row the platform writes for every call the router runs. It
answers the operator's questions — which provider and model served a purpose, how
many tokens and how much it cost, how long it took, and whether it succeeded, failed,
timed out or was cancelled — and the fallback columns record when the serving
provider was not the first choice, so a silent model switch is visible after the fact
(§7).

Three rules the model holds:

- **The unknown is null, never zero (§58).** Every token and cost field defaults to
  `None`: a CLI that prints no usage yields `None`, which a telemetry sum skips rather
  than counting a fabricated 0. A run therefore reports what was measured, not a
  guess.
- **A failure is typed and secret-free.** `failure_code` is one of the closed
  `LLMFailureCode` set and `failure_detail` is a composed sentence — the caller runs
  it through `redact_secrets` before constructing the run, because a provider's own
  message can echo the credential it rejected (§61, docs/ENGINEERING_STANDARDS.md
  §Security).
- **Status and shape agree.** A `STARTED` run is in flight and has no `finished_at`; a
  terminal run has one. A failure carries a code; a success does not. The validators
  hold both, so a half-built row cannot be stored.
"""
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import Field, model_validator

from backend.app.domain.identifiers import LLMConnectionId, LLMRunId, UserId
from backend.app.llm.connection import LLMProviderType
from backend.app.llm.contracts import LLMValue, TaskPurpose
from backend.app.llm.failures import LLMFailureCode


class LLMRunStatus(StrEnum):
    """How an LLM call ended (§57).

    `STARTED` is the in-flight state a run is written in before the call returns, so a
    crash mid-call leaves a visible unfinished row rather than nothing. The four
    terminal states tell the outcomes apart that a single "failed" would blur:
    `CANCELLED` (a client disconnect, a shutdown) is not counted against a provider,
    and `TIMEOUT` is separated from `FAILED` because a deadline and a refusal are
    different operational problems.
    """

    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"

    @property
    def is_terminal(self) -> bool:
        return self is not LLMRunStatus.STARTED


# The terminal states that record a failure code, as opposed to a clean end. Stated
# once so the validator and any reader agree on which outcomes carry a `failure_code`.
_FAILURE_STATUSES: Final[frozenset[LLMRunStatus]] = frozenset({
    LLMRunStatus.FAILED, LLMRunStatus.TIMEOUT,
})


class LLMRun(LLMValue):
    """The telemetry record of one call routed through the platform (§12, §56).

    `user_id` and `connection_id` are nullable because not every call has them: a
    healthcheck probe has no user, and a provider built by `bootstrap` rather than
    from a stored connection has no `connection_id`. `provider_key` — the stable
    identity that actually served — is always present, so a run is always attributable
    to a provider even when it is not attributable to a connection.

    `fallback_from` and `fallback_reason` are set together, and only when the serving
    provider was not the first choice: the key the router tried first and the coded
    reason it gave way, copied straight off the `RoutingOutcome`.
    """

    id: LLMRunId
    user_id: UserId | None = None
    connection_id: LLMConnectionId | None = None
    provider_key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_\-]*$")]
    provider_type: LLMProviderType | None = None
    model: str | None = None
    purpose: TaskPurpose = TaskPurpose.GENERIC
    status: LLMRunStatus

    prompt_name: str | None = None
    prompt_version: str | None = None

    prompt_tokens: Annotated[int, Field(ge=0)] | None = None
    completion_tokens: Annotated[int, Field(ge=0)] | None = None
    total_tokens: Annotated[int, Field(ge=0)] | None = None
    cost_usd: Annotated[float, Field(ge=0.0)] | None = None
    latency_ms: Annotated[int, Field(ge=0)] | None = None

    failure_code: LLMFailureCode | None = None
    # A secret-free sentence, already run through `redact_secrets` by the caller.
    failure_detail: str | None = None

    fallback_from: str | None = None
    fallback_reason: LLMFailureCode | None = None

    started_at: datetime
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def _status_agrees_with_shape(self) -> Self:
        if self.status.is_terminal and self.finished_at is None:
            raise ValueError(
                f"a {self.status} run must carry finished_at")
        if not self.status.is_terminal and self.finished_at is not None:
            raise ValueError("a STARTED run has not finished")
        if self.status in _FAILURE_STATUSES and self.failure_code is None:
            raise ValueError(f"a {self.status} run must carry a failure_code")
        if self.status not in _FAILURE_STATUSES and self.failure_code is not None:
            raise ValueError(
                f"a {self.status} run must not carry a failure_code")
        return self

    @model_validator(mode="after")
    def _fallback_pair_is_complete(self) -> Self:
        if (self.fallback_from is None) != (self.fallback_reason is None):
            raise ValueError(
                "fallback_from and fallback_reason are recorded together or not at all")
        return self
