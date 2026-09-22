"""The append-only audit trail of an application, and its submission attempts.

§41-43 ask for two things this module provides. `ApplicationEvent` is one entry in an
application's *append-only* history — created, prepared, approved, submitted, failed —
each stamped with who acted, when, and a composed, secret-free detail, all threaded by
a `correlation_id` so one logical run reads as one story. `SubmissionAttempt` is the
heavier record of one actual try at the irreversible act (§39): its outcome, the
adapter that ran it, and the employer's confirmation reference when there is one.

Neither is ever mutated. An event is a fact that happened at an instant, so a
correction is a new event, never an edit; a re-submission is a new attempt with the
next `attempt_number`, never an overwrite of the last. That is what makes the trail
trustworthy: it can only grow, so it cannot be quietly rewritten after the fact.
"""
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.application import ApplicationState, SubmissionOutcome
from backend.app.domain.application_channel import HumanRequiredReason
from backend.app.domain.application_failure import ApplicationFailureCode
from backend.app.domain.base import DomainModel, NonEmptyStr, UtcDatetime
from backend.app.domain.common import Reason
from backend.app.domain.identifiers import (
    ApplicationEventId,
    ApplicationId,
    SubmissionAttemptId,
)


class ApplicationEventType(StrEnum):
    """What happened, as a stable code a dashboard groups on (§41).

    The vocabulary of the trail. Most correspond to a state change, but a few record
    a *decision the engine made* without a state change — a gate evaluation, a
    duplicate blocked at creation — because those are exactly the facts an audit of
    "why did (didn't) this apply?" needs and a bare state history would lose.
    """

    CREATED = "CREATED"
    PREPARATION_STARTED = "PREPARATION_STARTED"
    PREPARED = "PREPARED"
    GATE_EVALUATED = "GATE_EVALUATED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    APPROVED = "APPROVED"
    SUBMISSION_STARTED = "SUBMISSION_STARTED"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_STATE_UNKNOWN = "SUBMISSION_STATE_UNKNOWN"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    WITHDRAWN = "WITHDRAWN"
    DUPLICATE_BLOCKED = "DUPLICATE_BLOCKED"
    RATE_LIMITED = "RATE_LIMITED"


class ApplicationEventActor(StrEnum):
    """Who caused an event, which is itself an audit fact.

    - `SYSTEM` — the deterministic engine (a gate evaluation, an automatic transition);
    - `USER` — the account holder (an approval, a cancellation from the UI);
    - `WORKER` — the isolated browser/submission worker reporting an outcome.
    """

    SYSTEM = "SYSTEM"
    USER = "USER"
    WORKER = "WORKER"


class ApplicationEvent(DomainModel):
    """One immutable entry in an application's history (§41-43).

    Records the transition it represents (`from_state`/`to_state`, both optional
    because a `GATE_EVALUATED` or `DUPLICATE_BLOCKED` event changes no state) and the
    reasons behind it, so the "why" is captured beside the "what". `detail` is a
    composed, secret-free sentence (§83) — never an adapter's or a page's raw text.
    `correlation_id` ties the event to the run that produced it.
    """

    id: ApplicationEventId
    application_id: ApplicationId
    event_type: ApplicationEventType
    actor: ApplicationEventActor = ApplicationEventActor.SYSTEM
    from_state: ApplicationState | None = None
    to_state: ApplicationState | None = None
    detail: NonEmptyStr | None = None
    reasons: tuple[Reason, ...] = ()
    correlation_id: NonEmptyStr | None = None
    occurred_at: UtcDatetime


class SubmissionAttempt(DomainModel):
    """One try at the irreversible act, recorded whole (§39).

    Keyed by `(application_id, attempt_number)` — the pair `submission_attempt_id`
    derives its stable id from — so a retried write of one attempt lands on its own
    row and never collides with the previous. `outcome` is the typed
    `SubmissionOutcome`; the fields that qualify it (`failure_code`,
    `human_required_reason`, `confirmation_reference`) mirror `SubmissionResult`, and
    the validator ties each to its outcome so an attempt cannot record, say, a
    confirmation reference on a failure.

    `finished_at` is `None` while an attempt is in flight — a row written at
    `SUBMISSION_STARTED` so a crash mid-submit leaves evidence the attempt began
    (§88), then completed when the worker reports back.
    """

    id: SubmissionAttemptId
    application_id: ApplicationId
    attempt_number: Annotated[int, Field(ge=1)]
    adapter_key: NonEmptyStr
    outcome: SubmissionOutcome | None = None
    detail: NonEmptyStr | None = None
    confirmation_reference: NonEmptyStr | None = None
    human_required_reason: HumanRequiredReason | None = None
    failure_code: ApplicationFailureCode | None = None
    correlation_id: NonEmptyStr | None = None
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _qualifiers_match_the_outcome(self) -> Self:
        """The same outcome/field agreement `SubmissionResult` enforces (§39).

        Only applied once an outcome is recorded: an in-flight attempt (outcome
        `None`, written at `SUBMISSION_STARTED`) carries none of these yet, which is
        the crash-evidence row §88 wants.
        """
        if self.outcome is None:
            return self
        if self.outcome is SubmissionOutcome.REQUIRES_HUMAN \
                and self.human_required_reason is None:
            raise ValueError(
                "a REQUIRES_HUMAN attempt must name a human_required_reason")
        if self.outcome is SubmissionOutcome.FAILED and self.failure_code is None:
            raise ValueError("a FAILED attempt must carry a failure_code")
        if self.outcome is not SubmissionOutcome.REQUIRES_HUMAN \
                and self.human_required_reason is not None:
            raise ValueError(
                "only a REQUIRES_HUMAN attempt may carry a human_required_reason")
        if self.outcome is not SubmissionOutcome.FAILED \
                and self.failure_code is not None:
            raise ValueError("only a FAILED attempt may carry a failure_code")
        if self.outcome is not SubmissionOutcome.SUBMITTED \
                and self.confirmation_reference is not None:
            raise ValueError(
                "only a SUBMITTED attempt may carry a confirmation_reference")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("SubmissionAttempt finished_at must not precede started_at")
        return self

    @property
    def is_in_flight(self) -> bool:
        """Whether this attempt began but has not yet reported an outcome."""
        return self.outcome is None
