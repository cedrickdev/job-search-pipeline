"""The contract every application adapter meets, and the bundle it is handed.

One `ApplicationAdapter` Protocol (§8-13), so the engine dispatches on a typed
`ApplicationChannel` and calls typed methods rather than branching on a platform
string. Two phases, deliberately separated (§33): `prepare` is non-irreversible —
it reads the form, discovers the questions and computes a fingerprint, and it may be
run, discarded and re-run freely; `submit` is the irreversible boundary and is
called at most once per attempt, only after the execution gate has authorized it.

Nothing here decides policy or resolves answers. An adapter reports what a form
*asks* (`AdapterPreparation.questions`); the service resolves each question to an
answer deterministically (`backend.app.domain.application_answer.resolve_answer`)
and hands the cleared answers back on the context before `submit`. This keeps §28-29
true: a model may propose, but the adapter only ever fills answers a deterministic
step already approved, and it never invents one.
"""
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from backend.app.domain.application import Application, PinnedDocument, SubmissionResult
from backend.app.domain.application_answer import (
    ApplicationAnswer,
    ApplicationAnswerProposal,
    ApplicationQuestion,
)
from backend.app.domain.application_channel import (
    AdapterSafetyLevel,
    ApplicationChannel,
    HumanRequiredReason,
)
from backend.app.domain.base import NonEmptyStr
from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.company import Company
from backend.app.domain.decision import ApplicationDecision
from backend.app.domain.documents import CandidateDocument
from backend.app.domain.opportunity import Opportunity


class EngineValue(BaseModel):
    """Frozen base for the engine's port DTOs.

    Like `DomainModel`, but it lives in the engine layer rather than the domain: a
    context bundles already-validated domain aggregates for an adapter, so it is a
    carrier, not a new domain concept. `frozen` keeps it a value; `extra='forbid'`
    keeps a typo'd field from being silently dropped, which matters most when a
    context is assembled from several sources.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class AdapterCapabilities(EngineValue):
    """What an adapter declares about itself (§9-11, §59).

    `key` is its stable provenance stamp (recorded on events and attempts, like
    `LLMProviderMetadata.provider_key`); `channel` is the one route it serves;
    `safety_level` is the autonomy ceiling it can bear, which the execution gate
    takes the minimum of against the user's policy — an adapter can lower autonomy,
    never raise it.

    `can_prepare` and `can_submit` must agree with the safety level, so a capability
    set cannot claim to submit while declaring itself `MANUAL_ONLY`. An `UNSUPPORTED`
    adapter can do neither; a `MANUAL_ONLY` one can prepare but not submit; the two
    higher levels can do both.
    """

    key: NonEmptyStr
    channel: ApplicationChannel
    safety_level: AdapterSafetyLevel
    can_prepare: bool
    can_submit: bool

    @model_validator(mode="after")
    def _capabilities_agree_with_safety(self) -> Self:
        if self.can_prepare and not self.safety_level.permits_preparation:
            raise ValueError(
                f"an adapter at {self.safety_level} cannot claim can_prepare")
        if self.can_submit and not self.safety_level.permits_unattended_submission \
                and self.safety_level is not AdapterSafetyLevel.SUPPORTED_WITH_REVIEW:
            raise ValueError(
                f"an adapter at {self.safety_level} cannot claim can_submit; only "
                "FULLY_SUPPORTED or SUPPORTED_WITH_REVIEW may submit")
        return self


class AdapterPreparation(EngineValue):
    """What `prepare` reports back: what the form asks, and what stopped it.

    `questions` is the form's fields as the adapter read them, for the service to
    resolve into answers. `form_fingerprint` is a hash of the form's structure at
    this moment (§34); the service stores it and the adapter re-checks it before
    `submit`, so a form that changed under the prepared answers is caught rather than
    filled blind. `human_required_reasons` are obstacles preparation already hit — a
    login wall, an obviously sensitive form — that mean a human is needed before this
    can go further. `detail` is a composed, secret-free sentence (§83), never a
    page's raw text.
    """

    questions: tuple[ApplicationQuestion, ...] = ()
    answer_proposals: tuple[ApplicationAnswerProposal, ...] = ()
    form_fingerprint: NonEmptyStr | None = None
    human_required_reasons: tuple[HumanRequiredReason, ...] = ()
    detail: NonEmptyStr | None = None


class ApplicationContext(EngineValue):
    """Everything an adapter is given for one application, and nothing it must not.

    A carrier of already-validated domain aggregates, assembled by the service and
    never persisted. `application` is the aggregate being acted on; `decision` is the
    intent behind it; `profile` and `opportunity`/`company` are the target and the
    candidate. `documents` are the candidate's usable documents for this posting, and
    `pinned_documents` are the exact versions the service pinned for submission
    (§14-16) — the adapter submits those, not "the latest".

    `answers` is empty during `prepare` and carries the deterministically resolved
    answers by the time `submit` is called; an adapter never resolves an answer
    itself. `recipient_email` is the explicit address an `EMAIL` application must have
    (§62) — the engine refuses to guess a recipient, so an email adapter reads it
    from here or reports `UNSUPPORTED`/`REQUIRES_HUMAN`.
    """

    application: Application
    decision: ApplicationDecision
    profile: CandidateProfile
    opportunity: Opportunity | None = None
    company: Company | None = None
    documents: tuple[CandidateDocument, ...] = ()
    pinned_documents: tuple[PinnedDocument, ...] = ()
    answers: tuple[ApplicationAnswer, ...] = ()
    expected_form_fingerprint: NonEmptyStr | None = None
    recipient_email: NonEmptyStr | None = None
    correlation_id: NonEmptyStr | None = None


@runtime_checkable
class ApplicationAdapter(Protocol):
    """Prepares and submits one application through one channel (§8-13).

    The one shape the registry dispatches to. Both methods are `async`, for the same
    reason `DocumentGenerator`'s are: a real adapter drives a browser subprocess or
    an HTTP call, which is I/O the service awaits, and a synchronous protocol would
    make an I/O-bound adapter unable to honour it. An adapter never raises an
    adapter-specific exception across this boundary; a failure it cannot handle is
    reported as a typed `SubmissionResult` (from `submit`) or classified by the
    service (`backend.app.domain.application_failure.classify_adapter_failure`).
    """

    @property
    def capabilities(self) -> AdapterCapabilities:
        """This adapter's channel, safety ceiling and provenance key."""
        ...

    async def prepare(self, context: ApplicationContext) -> AdapterPreparation:
        """Read the form and report what it asks — never submit, never mutate.

        Idempotent and reversible (§33): running it twice must not leave a partial
        application behind, because the service may prepare, have the gate refuse,
        and prepare again after a human resolves something.
        """
        ...

    async def submit(self, context: ApplicationContext) -> SubmissionResult:
        """Submit the prepared application; the one irreversible act (§80-88).

        Called at most once per attempt and only after the gate authorized it. It
        must re-check the form fingerprint and the pinned documents before sending,
        and it must report an ambiguous outcome honestly as `STATE_UNKNOWN` rather
        than assume success — a blind retry could double-submit (§38).
        """
        ...
