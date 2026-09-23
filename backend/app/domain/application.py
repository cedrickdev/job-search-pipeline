"""`Application` — one attempt to put a candidate in front of one employer.

Where `ApplicationDecision` records *intent* ("apply to this"),  `Application`
records *execution*: the lifecycle of actually doing it, from planned through
prepared, reviewed, approved, submitted — or stopped at a human, failed, cancelled
or left in an unknown state. Keeping the two apart is deliberate (§2-3): a decision
is a value the matcher produces and never mutates, while an application is a
long-lived aggregate that moves through states as a worker and a human act on it.

Three invariants live here rather than in a service, because a service can forget
and a frozen model cannot:

- the state machine is closed (§17-18). Every move goes through `transition_to`,
  which consults `ALLOWED_TRANSITIONS`; an illegal jump (SUBMITTED straight back to
  PREPARING, say) raises rather than silently corrupting the trail.
- the idempotency key is a function of the target, not a free field (§36). The
  validator recomputes it from `(candidate_profile, target, channel)` and refuses a
  hand-built application whose key does not match — so the key that `application_id`
  derives the primary key from cannot drift from what it claims to identify.
- a submission pins *exact* document versions (§14-16). `pinned_documents` holds
  version ids, not "the latest", so a résumé regenerated to v4 after v3 was reviewed
  is never the thing that gets sent.
"""
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.application_answer import ApplicationAnswer
from backend.app.domain.application_channel import (
    ApplicationChannel,
    HumanRequiredReason,
)
from backend.app.domain.application_failure import ApplicationFailureCode
from backend.app.domain.base import DomainModel, NonEmptyStr, UtcDatetime
from backend.app.domain.documents import CandidateDocumentType
from backend.app.domain.identifiers import (
    ApplicationDecisionId,
    ApplicationId,
    ApplicationPolicyId,
    CandidateDocumentId,
    CandidateProfileId,
    CompanyId,
    DocumentVersionId,
    OpportunityId,
    UserId,
)


class ApplicationState(StrEnum):
    """Every state one application can be in (§17-18).

    The happy path runs `PLANNED → PREPARING → READY_FOR_REVIEW → APPROVED →
    SUBMITTING → SUBMITTED`; the branches are the honest ways it can leave that path.

    - `PLANNED` — created from a decision, nothing prepared yet;
    - `PREPARING` — an adapter is drafting materials and reading the form;
    - `READY_FOR_REVIEW` — prepared and filled, waiting for a human to approve (§52);
    - `REQUIRES_HUMAN` — stopped on something only a person can resolve (§19-27);
    - `APPROVED` — cleared to submit, by a human or an autopilot policy;
    - `SUBMITTING` — a submission attempt is in flight;
    - `SUBMITTED` — confirmed to have reached the employer;
    - `SUBMISSION_STATE_UNKNOWN` — it left the platform but no confirmation could be
      read, so whether it landed is genuinely unknown; never retried automatically,
      because a blind retry could double-submit (§38, §84, §88);
    - `FAILED` — an execution failure that a fresh preparation could retry;
    - `CANCELLED` — abandoned before it ever reached the employer;
    - `WITHDRAWN` — retracted after it was submitted.
    """

    PLANNED = "PLANNED"
    PREPARING = "PREPARING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    REQUIRES_HUMAN = "REQUIRES_HUMAN"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_STATE_UNKNOWN = "SUBMISSION_STATE_UNKNOWN"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    WITHDRAWN = "WITHDRAWN"


# The closed transition graph (§17-18). A state maps to the set it may move to; a
# state absent from a value's set is an illegal move. `transition_to` is the only
# code that reads this, so the machine has exactly one definition.
ALLOWED_TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = {
    ApplicationState.PLANNED: frozenset({
        ApplicationState.PREPARING,
        ApplicationState.CANCELLED,
    }),
    ApplicationState.PREPARING: frozenset({
        ApplicationState.READY_FOR_REVIEW,
        ApplicationState.REQUIRES_HUMAN,
        ApplicationState.APPROVED,
        ApplicationState.FAILED,
        ApplicationState.CANCELLED,
    }),
    ApplicationState.READY_FOR_REVIEW: frozenset({
        ApplicationState.APPROVED,
        ApplicationState.REQUIRES_HUMAN,
        ApplicationState.PREPARING,
        ApplicationState.CANCELLED,
    }),
    ApplicationState.REQUIRES_HUMAN: frozenset({
        ApplicationState.PREPARING,
        ApplicationState.READY_FOR_REVIEW,
        ApplicationState.APPROVED,
        ApplicationState.CANCELLED,
    }),
    ApplicationState.APPROVED: frozenset({
        ApplicationState.SUBMITTING,
        ApplicationState.REQUIRES_HUMAN,
        ApplicationState.CANCELLED,
    }),
    ApplicationState.SUBMITTING: frozenset({
        ApplicationState.SUBMITTED,
        ApplicationState.SUBMISSION_STATE_UNKNOWN,
        ApplicationState.REQUIRES_HUMAN,
        ApplicationState.FAILED,
    }),
    ApplicationState.SUBMITTED: frozenset({
        ApplicationState.WITHDRAWN,
    }),
    ApplicationState.SUBMISSION_STATE_UNKNOWN: frozenset({
        ApplicationState.SUBMITTED,
        ApplicationState.FAILED,
        ApplicationState.WITHDRAWN,
    }),
    ApplicationState.FAILED: frozenset({
        ApplicationState.PREPARING,
        ApplicationState.CANCELLED,
    }),
    ApplicationState.CANCELLED: frozenset(),
    ApplicationState.WITHDRAWN: frozenset(),
}

# The states from which nothing moves: an application here is closed for good.
_TERMINAL_STATES: frozenset[ApplicationState] = frozenset({
    ApplicationState.CANCELLED,
    ApplicationState.WITHDRAWN,
})


def build_idempotency_key(
    *,
    candidate_profile_id: CandidateProfileId,
    channel: ApplicationChannel,
    opportunity_id: OpportunityId | None = None,
    company_id: CompanyId | None = None,
) -> str:
    """The stable key that identifies "this candidate applying here, this way" (§36).

    The whole duplicate-prevention story rests on this being a pure function of the
    target: two independent attempts to apply for the same posting through the same
    channel compose the same key, `application_id` derives the same primary key from
    it, and the second attempt collides instead of opening a second application. The
    target is the opportunity when there is one and the company when the application
    is spontaneous (§60) — exactly the two cases `ApplicationDecision` distinguishes.

    Exactly one of `opportunity_id`/`company_id` must be given; naming both would be
    two different targets sharing one key, and naming neither would key on nothing.
    """
    if (opportunity_id is None) == (company_id is None):
        raise ValueError(
            "build_idempotency_key needs exactly one of opportunity_id or company_id")
    target = (f"opportunity:{opportunity_id}" if opportunity_id is not None
              else f"company:{company_id}")
    return f"{candidate_profile_id}:{target}:{channel}"


class PinnedDocument(DomainModel):
    """An exact document version pinned to an application for submission (§14-16).

    A version id, not "the latest": the regression §16 names is review résumé v3,
    regenerate to v4, submit — and the thing submitted must be v3. Pinning the
    `version_id` and its `version` number is what makes that guarantee structural;
    the submission service re-checks the pinned version is still a RENDERED,
    guard-cleared artifact before sending it, and refuses with
    `APPLICATION_DOCUMENT_NOT_READY` if it is not.
    """

    document_id: CandidateDocumentId
    version_id: DocumentVersionId
    version: Annotated[int, Field(ge=1)]
    document_type: CandidateDocumentType


class SubmissionOutcome(StrEnum):
    """How one submission attempt ended (§38-40, §80-88).

    - `SUBMITTED` — the platform confirmed the application reached the employer;
    - `REQUIRES_HUMAN` — a human-only obstacle appeared mid-submission (a CAPTCHA, a
      login), so the attempt stopped without submitting;
    - `FAILED` — a typed, recoverable failure; a fresh preparation may retry;
    - `STATE_UNKNOWN` — the request left the platform but no confirmation could be
      read, so whether it landed is unknown and it is never retried automatically.
    """

    SUBMITTED = "SUBMITTED"
    REQUIRES_HUMAN = "REQUIRES_HUMAN"
    FAILED = "FAILED"
    STATE_UNKNOWN = "STATE_UNKNOWN"


class SubmissionResult(DomainModel):
    """What an adapter reports back from one submission attempt.

    The typed boundary between an adapter and the submission service: the adapter
    says what happened in this vocabulary and never raises an adapter-specific
    exception across it (§8, §80). The validator ties each outcome to the field it
    requires, so a `REQUIRES_HUMAN` result that names no reason, or a `FAILED` one
    that carries no code, cannot be constructed — the service can trust the shape.

    `confirmation_reference` is the employer's own handle for the submission (an ATS
    confirmation id, a reference number) when one is returned; it is provenance for
    the audit trail, never a secret. `detail` is a composed, secret-free sentence
    (§83) — never a page's raw text.
    """

    outcome: SubmissionOutcome
    detail: NonEmptyStr | None = None
    confirmation_reference: NonEmptyStr | None = None
    human_required_reason: HumanRequiredReason | None = None
    failure_code: ApplicationFailureCode | None = None

    @model_validator(mode="after")
    def _fields_match_the_outcome(self) -> Self:
        if self.outcome is SubmissionOutcome.REQUIRES_HUMAN \
                and self.human_required_reason is None:
            raise ValueError(
                "a REQUIRES_HUMAN submission result must name a human_required_reason")
        if self.outcome is SubmissionOutcome.FAILED and self.failure_code is None:
            raise ValueError("a FAILED submission result must carry a failure_code")
        if self.outcome is not SubmissionOutcome.REQUIRES_HUMAN \
                and self.human_required_reason is not None:
            raise ValueError(
                "only a REQUIRES_HUMAN result may carry a human_required_reason")
        if self.outcome is not SubmissionOutcome.FAILED \
                and self.failure_code is not None:
            raise ValueError("only a FAILED result may carry a failure_code")
        if self.outcome is not SubmissionOutcome.SUBMITTED \
                and self.confirmation_reference is not None:
            raise ValueError(
                "only a SUBMITTED result may carry a confirmation_reference")
        return self

    @property
    def target_state(self) -> ApplicationState:
        """The application state this result drives the aggregate into."""
        return _OUTCOME_TO_STATE[self.outcome]


_OUTCOME_TO_STATE: dict[SubmissionOutcome, ApplicationState] = {
    SubmissionOutcome.SUBMITTED: ApplicationState.SUBMITTED,
    SubmissionOutcome.REQUIRES_HUMAN: ApplicationState.REQUIRES_HUMAN,
    SubmissionOutcome.FAILED: ApplicationState.FAILED,
    SubmissionOutcome.STATE_UNKNOWN: ApplicationState.SUBMISSION_STATE_UNKNOWN,
}


class Application(DomainModel):
    """The execution aggregate for one (candidate, target, channel) application.

    User-owned and scoped to a single target: `opportunity_id` when applying to a
    posting, `company_id` when applying spontaneously (§60), exactly one of the two.
    `state` moves only through `transition_to`, `pinned_documents` records the exact
    versions a submission will send (§14-16), and `attempt_count` is the monotonic
    counter `submission_attempt_id` turns into a stable per-attempt key (§39).

    `correlation_id` threads one logical run through the append-only event trail
    (§43), so a decision, a preparation and a submission that belong together can be
    read as one story.
    """

    id: ApplicationId
    user_id: UserId
    candidate_profile_id: CandidateProfileId
    decision_id: ApplicationDecisionId
    channel: ApplicationChannel
    state: ApplicationState = ApplicationState.PLANNED
    idempotency_key: NonEmptyStr
    opportunity_id: OpportunityId | None = None
    company_id: CompanyId | None = None
    policy_id: ApplicationPolicyId | None = None
    pinned_documents: tuple[PinnedDocument, ...] = ()
    answers: tuple[ApplicationAnswer, ...] = ()
    form_fingerprint: NonEmptyStr | None = None
    attempt_count: Annotated[int, Field(ge=0)] = 0
    correlation_id: NonEmptyStr | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _target_is_singular_and_keyed(self) -> Self:
        if (self.opportunity_id is None) == (self.company_id is None):
            raise ValueError(
                "an Application names exactly one of opportunity_id or company_id")
        expected = build_idempotency_key(
            candidate_profile_id=self.candidate_profile_id,
            channel=self.channel,
            opportunity_id=self.opportunity_id,
            company_id=self.company_id)
        if self.idempotency_key != expected:
            raise ValueError(
                "idempotency_key does not match the application's target and channel; "
                "it must be build_idempotency_key(...) for this pair")
        if self.updated_at < self.created_at:
            raise ValueError("Application updated_at must not precede created_at")
        return self

    @property
    def is_terminal(self) -> bool:
        """Whether the application is closed for good (cancelled or withdrawn)."""
        return self.state in _TERMINAL_STATES

    @property
    def target_key(self) -> str:
        """A stable label for what this application targets, for logs and events."""
        return (f"opportunity:{self.opportunity_id}" if self.opportunity_id is not None
                else f"company:{self.company_id}")

    def can_transition_to(self, state: ApplicationState) -> bool:
        """Whether the state machine permits moving to `state` from here."""
        return state in ALLOWED_TRANSITIONS[self.state]

    def transition_to(self, state: ApplicationState, *,
                      at: UtcDatetime) -> "Application":
        """Return a copy in `state`, or raise if the machine forbids the move.

        The one door every state change goes through (§18). It refuses an illegal
        transition rather than performing it, so a bug that tries to re-submit a
        withdrawn application fails loudly instead of corrupting the trail. `at`
        stamps `updated_at` and is passed in — the caller owns the clock, as every
        domain object here does.
        """
        if not self.can_transition_to(state):
            raise ValueError(
                f"illegal application transition {self.state} -> {state}")
        return self.model_copy(update={"state": state, "updated_at": at})

    def with_pinned_documents(self, pinned: tuple[PinnedDocument, ...], *,
                             at: UtcDatetime) -> "Application":
        """Return a copy carrying the exact versions a submission will send (§14-16)."""
        return self.model_copy(update={"pinned_documents": pinned, "updated_at": at})

    def with_form_fingerprint(self, fingerprint: str | None, *,
                             at: UtcDatetime) -> "Application":
        """Return a copy recording the form's fingerprint from preparation (§34).

        The submission service compares this against the form again before sending;
        a change means the form moved under the prepared answers, and the run stops
        with `APPLICATION_FORM_CHANGED` rather than filling a form it no longer knows.
        """
        return self.model_copy(
            update={"form_fingerprint": fingerprint, "updated_at": at})

    def with_answers(self, answers: tuple[ApplicationAnswer, ...], *,
                    at: UtcDatetime) -> "Application":
        """Return a copy carrying the answers cleared to fill the form."""
        return self.model_copy(update={"answers": answers, "updated_at": at})

    def with_next_attempt(self, *, at: UtcDatetime) -> "Application":
        """Return a copy with the submission attempt counter advanced (§39).

        The counter is monotonic and never reused, so `submission_attempt_id` derives
        a fresh stable key per attempt and a retried write of one attempt lands on its
        own row rather than colliding with the previous.
        """
        return self.model_copy(
            update={"attempt_count": self.attempt_count + 1, "updated_at": at})


class PreparedApplication(DomainModel):
    """The transient result of preparing an application, handed to the gate (§33-35).

    Not persisted as itself — its parts land on the `Application` and its events. It
    bundles what preparation produced so the submission service can re-check it: the
    pinned document versions, the answers cleared to fill, the human-required reasons
    preparation surfaced, and the `form_fingerprint` — a hash of the form's structure
    at preparation time (§34). The submission service compares that fingerprint again
    before sending; a change means the form moved under the prepared answers, and the
    run stops with `APPLICATION_FORM_CHANGED` rather than filling a form it no longer
    understands.
    """

    application_id: ApplicationId
    channel: ApplicationChannel
    pinned_documents: tuple[PinnedDocument, ...] = ()
    answers: tuple[ApplicationAnswer, ...] = ()
    human_required_reasons: tuple[HumanRequiredReason, ...] = ()
    form_fingerprint: NonEmptyStr | None = None
    prepared_at: UtcDatetime
