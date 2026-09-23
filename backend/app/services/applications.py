"""ApplicationService: the lifecycle of one application, orchestrated safely.

This is where Phase 12's rules become one workflow. The service owns the transitions
create → prepare → approve → submit → cancel, and every one of them is written so the
safety invariants hold even when a worker crashes, a policy changes underneath a run,
or the same request arrives twice:

- **the gate decides, twice.** Preparation evaluates the execution gate to route a
  run (auto-approve, wait for review, ask a human, or block); submission evaluates it
  *again* against the freshly loaded policy, so a run authorized this morning is
  refused this afternoon if the policy became MANUAL in between (§1, §5).
- **idempotent by construction.** An application's id is derived from its idempotency
  key, so creating one twice for the same target returns the first rather than opening
  a second, and a submission for an already-submitted target is refused as a
  duplicate (§36-40).
- **only reviewed, rendered documents are sent.** Preparation pins exact document
  versions; submission re-checks each pinned version is still a RENDERED, cleared
  artifact before it hands anything to an adapter (§14-16).
- **the trail only grows.** Every step appends an immutable `ApplicationEvent`, and a
  submission writes an in-flight `SubmissionAttempt` *before* the irreversible act, so
  a crash mid-submit leaves evidence and recovery can resolve it to STATE_UNKNOWN
  rather than a blind retry (§41, §88).

Nothing here drives a browser or sends an email itself: it resolves the adapter for a
channel from the registry and calls typed methods, so no vendor branching leaks into
the workflow (§8).
"""
from datetime import UTC, datetime, timedelta

from backend.app.application_engine.contracts import (
    AdapterPreparation,
    ApplicationContext,
)
from backend.app.application_engine.registry import ApplicationAdapterRegistry
from backend.app.domain.application import (
    Application,
    ApplicationState,
    PinnedDocument,
    SubmissionOutcome,
    SubmissionResult,
    build_idempotency_key,
)
from backend.app.domain.application_answer import ApplicationAnswer, resolve_answer
from backend.app.domain.application_channel import (
    AdapterSafetyLevel,
    ApplicationChannel,
    HumanRequiredReason,
)
from backend.app.domain.application_event import (
    ApplicationEvent,
    ApplicationEventActor,
    ApplicationEventType,
    SubmissionAttempt,
)
from backend.app.domain.application_failure import (
    ApplicationError,
    ApplicationFailureCode,
    classify_adapter_failure,
)
from backend.app.domain.common import Reason
from backend.app.domain.decision import ApplicationDecision
from backend.app.domain.documents import (
    CandidateDocument,
    CandidateDocumentType,
    DocumentStatus,
    DocumentVersion,
)
from backend.app.domain.execution_gate import (
    ApplicationExecutionGate,
    ExecutionAuthorization,
    ExecutionOutcome,
)
from backend.app.domain.identifiers import (
    ApplicationId,
    OpportunityId,
    UserId,
    application_id,
    new_application_event_id,
    submission_attempt_id,
)
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    ApplicationDecisionRepository,
    ApplicationEventRepository,
    ApplicationPolicyRepository,
    ApplicationRepository,
    CandidateDocumentRepository,
    CandidateProfileRepository,
    EligibilityResultRepository,
    MatchEvaluationRepository,
    OpportunityRepository,
    SubmissionAttemptRepository,
)
from backend.app.services.assessment import (
    CandidateProfileNotFound,
    OpportunityNotFound,
)

# The decision kinds an application can be opened for: everything that intends to
# reach an employer, plus the two that prepare materials for a human. A SKIP or a
# SAVE has no application — there is nothing to execute.
_ACTIONABLE_KINDS = frozenset({
    "PREPARE", "REQUIRE_REVIEW", "AUTO_APPLY", "APPLY_AND_OUTREACH",
    "SPONTANEOUS_APPLICATION",
})
_RATE_LIMIT_REASON_CODE = "POLICY_RATE_LIMIT_EXHAUSTED"


class ApplicationNotFound(Exception):
    """No application is stored under that id for this account (or it is not yours)."""


class ApplicationDecisionMissing(Exception):
    """No decision exists for this pair, so there is no intent to act on.

    An application is opened *from* a decision (§2-3); without one there is nothing to
    execute. The API maps it to a 409 — decide first, then apply.
    """


class ApplicationNotActionable(Exception):
    """The application is not in a state this operation can act on.

    Raised when, say, `approve` is called on an application that was never prepared,
    or `submit` on one that is not APPROVED. The state machine would refuse the
    transition anyway; this turns that into a clear message before the attempt.
    """

    def __init__(self, state: ApplicationState, operation: str) -> None:
        super().__init__(f"cannot {operation} an application in state {state}")
        self.state = state
        self.operation = operation


class ApplicationService:
    """Orchestrates the application lifecycle over its repositories and the registry.

    Every collaborator is injected, so a flow test drives the whole workflow over
    fakes and the fake adapters without a browser, a database or an LLM. The service
    holds no clock: each method takes `now`, so the gate's rate window and every
    timestamp read the one instant the caller controls.
    """

    def __init__(self, *,
                 applications: ApplicationRepository,
                 events: ApplicationEventRepository,
                 attempts: SubmissionAttemptRepository,
                 decisions: ApplicationDecisionRepository,
                 policies: ApplicationPolicyRepository,
                 matches: MatchEvaluationRepository,
                 eligibilities: EligibilityResultRepository,
                 profiles: CandidateProfileRepository,
                 opportunities: OpportunityRepository,
                 documents: CandidateDocumentRepository,
                 registry: ApplicationAdapterRegistry) -> None:
        self._applications = applications
        self._events = events
        self._attempts = attempts
        self._decisions = decisions
        self._policies = policies
        self._matches = matches
        self._eligibilities = eligibilities
        self._profiles = profiles
        self._opportunities = opportunities
        self._documents = documents
        self._registry = registry

    # -- reads ---------------------------------------------------------------

    async def get(self, user_id: UserId,
                  app_id: ApplicationId) -> Application:
        found = await self._applications.get(user_id, app_id)
        if found is None:
            raise ApplicationNotFound(str(app_id))
        return found

    async def list(self, user_id: UserId, *,
                   limit: int = DEFAULT_LIMIT) -> tuple[Application, ...]:
        return await self._applications.list_for_user(user_id, limit=limit)

    async def events(self, user_id: UserId, app_id: ApplicationId, *,
                     limit: int = DEFAULT_LIMIT) -> tuple[ApplicationEvent, ...]:
        await self.get(user_id, app_id)  # ownership check; raises if not this user's
        return await self._events.list_for_application(user_id, app_id, limit=limit)

    # -- create --------------------------------------------------------------

    async def create(self, user_id: UserId, opportunity_id: OpportunityId, *,
                     now: datetime,
                     correlation_id: str | None = None) -> Application:
        """Open an application for a posting from its current decision (§2-3, §36).

        Idempotent: the id is derived from the target and channel, so a second create
        for the same pair returns the first application rather than opening another.
        A create against a target already SUBMITTED is refused as a duplicate.
        """
        profile = await self._profiles.get_default(user_id)
        if profile is None:
            raise CandidateProfileNotFound(str(user_id))
        opportunity = await self._opportunities.get(opportunity_id)
        if opportunity is None:
            raise OpportunityNotFound(str(opportunity_id))
        decision = await self._decisions.get_for_pair(
            user_id, profile.id, opportunity_id)
        if decision is None or decision.kind.value not in _ACTIONABLE_KINDS:
            raise ApplicationDecisionMissing(str(opportunity_id))

        channel = _resolve_channel(opportunity.application_url)
        key = build_idempotency_key(
            candidate_profile_id=profile.id, channel=channel,
            opportunity_id=opportunity_id)

        existing = await self._applications.get_by_idempotency_key(user_id, key)
        if existing is not None:
            if existing.state is ApplicationState.SUBMITTED:
                await self._append(existing, ApplicationEventType.DUPLICATE_BLOCKED,
                                   now=now, detail="already submitted for this target")
                raise ApplicationError(ApplicationFailureCode.APPLICATION_DUPLICATE)
            return existing

        app = Application(
            id=application_id(key), user_id=user_id,
            candidate_profile_id=profile.id, decision_id=decision.id,
            channel=channel, state=ApplicationState.PLANNED, idempotency_key=key,
            opportunity_id=opportunity_id, policy_id=decision.policy_id,
            correlation_id=correlation_id, created_at=now, updated_at=now)
        stored = await self._applications.upsert(app)
        await self._append(stored, ApplicationEventType.CREATED, now=now,
                          detail=f"opened for {stored.target_key} via {channel}")
        return stored

    # -- prepare -------------------------------------------------------------

    async def prepare(self, user_id: UserId, app_id: ApplicationId, *,
                      now: datetime) -> Application:
        """Prepare materials, resolve answers, pin documents, and route by the gate.

        Reversible (§33): it may be re-run after a human resolves something. It never
        submits — the furthest it goes is APPROVED when the gate permits unattended
        submission, and even then the actual send waits for `submit`, which re-gates.
        """
        app = await self.get(user_id, app_id)
        if app.is_terminal or app.state is ApplicationState.SUBMITTED:
            raise ApplicationNotActionable(app.state, "prepare")
        adapter = self._registry.resolve(app.channel)

        app = app.transition_to(ApplicationState.PREPARING, at=now)
        app = await self._applications.upsert(app)
        await self._append(app, ApplicationEventType.PREPARATION_STARTED, now=now)

        context = await self._build_context(app)
        try:
            prep = await adapter.prepare(context)
        except Exception as exc:
            error = classify_adapter_failure(exc)
            failed = app.transition_to(ApplicationState.FAILED, at=now)
            failed = await self._applications.upsert(failed)
            await self._append(failed, ApplicationEventType.FAILED, now=now,
                             detail=error.detail)
            return failed

        answers, answer_reasons = _resolve_answers(prep)
        human_reasons = _dedupe(prep.human_required_reasons + answer_reasons)
        pinned = self._pin_documents(context.documents, now=now)

        app = app.with_answers(answers, at=now)
        app = app.with_pinned_documents(pinned, at=now)
        app = app.with_form_fingerprint(prep.form_fingerprint, at=now)
        await self._append(app, ApplicationEventType.PREPARED, now=now,
                          detail=prep.detail)

        if human_reasons:
            resolved = app.transition_to(ApplicationState.REQUIRES_HUMAN, at=now)
            resolved = await self._applications.upsert(resolved)
            await self._append(resolved, ApplicationEventType.HUMAN_REQUIRED, now=now,
                             detail="; ".join(r.value for r in human_reasons))
            return resolved

        authorization = await self._evaluate_gate(
            app, adapter_safety=adapter.capabilities.safety_level, now=now)
        return await self._route_prepared(app, authorization, now=now)

    async def _route_prepared(self, app: Application,
                              authorization: ExecutionAuthorization, *,
                              now: datetime) -> Application:
        """Move a prepared application to the state the gate's outcome implies."""
        await self._append(app, ApplicationEventType.GATE_EVALUATED, now=now,
                          reasons=authorization.reasons,
                          detail=f"gate: {authorization.outcome}")
        if authorization.outcome is ExecutionOutcome.PERMITTED:
            target, event = ApplicationState.APPROVED, ApplicationEventType.APPROVED
        elif authorization.outcome is ExecutionOutcome.REQUIRES_APPROVAL:
            target, event = (ApplicationState.READY_FOR_REVIEW,
                            ApplicationEventType.PREPARED)
        elif authorization.outcome is ExecutionOutcome.REQUIRES_HUMAN:
            target, event = (ApplicationState.REQUIRES_HUMAN,
                            ApplicationEventType.HUMAN_REQUIRED)
        else:  # BLOCKED
            target, event = (ApplicationState.CANCELLED,
                            ApplicationEventType.CANCELLED)
        moved = app.transition_to(target, at=now)
        moved = await self._applications.upsert(moved)
        await self._append(moved, event, now=now,
                          reasons=authorization.reasons)
        return moved

    # -- approve -------------------------------------------------------------

    async def approve(self, user_id: UserId, app_id: ApplicationId, *,
                      now: datetime) -> Application:
        """Record a human's approval of a prepared application (§52).

        Only a READY_FOR_REVIEW application can be approved — approval is a person
        signing off on the *specific* prepared version, so an application that was
        never prepared, or already approved, is not actionable here.
        """
        app = await self.get(user_id, app_id)
        if app.state is not ApplicationState.READY_FOR_REVIEW:
            raise ApplicationNotActionable(app.state, "approve")
        moved = app.transition_to(ApplicationState.APPROVED, at=now)
        moved = await self._applications.upsert(moved)
        await self._append(moved, ApplicationEventType.APPROVED, now=now,
                          actor=ApplicationEventActor.USER)
        return moved

    # -- submit --------------------------------------------------------------

    async def submit(self, user_id: UserId, app_id: ApplicationId, *,
                     now: datetime) -> Application:
        """Submit an approved application — the irreversible boundary (§1, §80-88).

        Re-evaluates the gate against the freshly loaded policy (§5) before doing
        anything irreversible, re-checks the pinned documents are still rendered
        (§15), writes an in-flight attempt before the send (§88), and records the
        typed outcome. An ambiguous send becomes STATE_UNKNOWN, never a retryable
        failure.
        """
        app = await self.get(user_id, app_id)
        if app.state is not ApplicationState.APPROVED:
            raise ApplicationNotActionable(app.state, "submit")
        adapter = self._registry.resolve(app.channel)

        # §49-51: serialize this user's budget for the rest of the transaction, so the
        # count → gate → reserve-as-SUBMITTING sequence below is atomic against another
        # worker racing the same slot. The lock releases when session_scope commits or
        # rolls back, and the count now sees the slot the reservation consumes.
        await self._applications.lock_submission_budget(user_id)
        authorization = await self._evaluate_gate(
            app, adapter_safety=adapter.capabilities.safety_level, now=now)
        stopped = await self._stop_if_gate_refuses(app, authorization, now=now)
        if stopped is not None:
            return stopped

        context = await self._build_context(app)
        not_ready = _first_unready_pin(app.pinned_documents, context.documents)
        app = app.transition_to(ApplicationState.SUBMITTING, at=now)
        app = app.with_next_attempt(at=now)
        app = await self._applications.upsert(app)
        await self._append(app, ApplicationEventType.SUBMISSION_STARTED, now=now,
                          actor=ApplicationEventActor.WORKER)
        attempt = await self._open_attempt(app, adapter.capabilities.key, now=now)

        if not_ready is not None:
            return await self._finish(
                app, attempt, SubmissionResult(
                    outcome=SubmissionOutcome.FAILED,
                    failure_code=ApplicationFailureCode.APPLICATION_DOCUMENT_NOT_READY,
                    detail=not_ready), now=now)

        try:
            result = await adapter.submit(context)
        except Exception as exc:
            # §84/§88: an exception during the irreversible act means we do not know
            # whether it landed — never assume failure, which would invite a retry.
            classify_adapter_failure(exc)  # drops any secret-bearing message
            result = SubmissionResult(
                outcome=SubmissionOutcome.STATE_UNKNOWN,
                detail="the adapter raised during submission; the outcome is unknown")
        return await self._finish(app, attempt, result, now=now)

    async def _stop_if_gate_refuses(self, app: Application,
                                    authorization: ExecutionAuthorization, *,
                                    now: datetime) -> Application | None:
        """Refuse a submission the re-checked gate no longer permits (§5).

        An already-approved application may still submit under PERMITTED or
        REQUIRES_APPROVAL (the approval was granted); anything stricter stops it. A
        rate-limit block is a transient refusal that keeps the approval for a retry
        and raises; any other block or human requirement moves the application to a
        non-submitted state so the user sees why.
        """
        await self._append(app, ApplicationEventType.GATE_EVALUATED, now=now,
                          reasons=authorization.reasons,
                          detail=f"submit-time gate: {authorization.outcome}")
        if authorization.outcome in (ExecutionOutcome.PERMITTED,
                                     ExecutionOutcome.REQUIRES_APPROVAL):
            return None
        if authorization.is_blocked and _has_rate_limit(authorization.reasons):
            await self._append(app, ApplicationEventType.RATE_LIMITED, now=now,
                             reasons=authorization.reasons)
            raise ApplicationError(ApplicationFailureCode.APPLICATION_RATE_LIMITED)
        moved = app.transition_to(ApplicationState.REQUIRES_HUMAN, at=now)
        moved = await self._applications.upsert(moved)
        await self._append(moved, ApplicationEventType.HUMAN_REQUIRED, now=now,
                          reasons=authorization.reasons)
        return moved

    async def _finish(self, app: Application, attempt: SubmissionAttempt,
                      result: SubmissionResult, *, now: datetime) -> Application:
        """Record a submission outcome: complete the attempt, move state, append."""
        completed = attempt.model_copy(update={
            "outcome": result.outcome, "detail": result.detail,
            "confirmation_reference": result.confirmation_reference,
            "human_required_reason": result.human_required_reason,
            "failure_code": result.failure_code, "finished_at": now})
        await self._attempts.upsert(completed)
        moved = app.transition_to(result.target_state, at=now)
        moved = await self._applications.upsert(moved)
        await self._append(moved, _OUTCOME_EVENT[result.outcome], now=now,
                          actor=ApplicationEventActor.WORKER, detail=result.detail)
        return moved

    # -- cancel --------------------------------------------------------------

    async def cancel(self, user_id: UserId, app_id: ApplicationId, *,
                     now: datetime) -> Application:
        """Abandon an application before it reaches the employer.

        Refused once an application is SUBMITTED — that is a withdrawal, a different
        act — or already terminal.
        """
        app = await self.get(user_id, app_id)
        if not app.can_transition_to(ApplicationState.CANCELLED):
            raise ApplicationNotActionable(app.state, "cancel")
        moved = app.transition_to(ApplicationState.CANCELLED, at=now)
        moved = await self._applications.upsert(moved)
        await self._append(moved, ApplicationEventType.CANCELLED, now=now,
                          actor=ApplicationEventActor.USER)
        return moved

    # -- recovery ------------------------------------------------------------

    async def recover_in_flight(self, *, now: datetime,
                               limit: int = DEFAULT_LIMIT) -> tuple[Application, ...]:
        """Resolve applications a crash left mid-flight, safely (§88).

        A SUBMITTING application whose worker died may have reached the employer, so
        it becomes SUBMISSION_STATE_UNKNOWN — never retried automatically. A PREPARING
        one changed nothing, so it becomes FAILED and can be prepared again. Run at
        startup, across every account.
        """
        recovered: list[Application] = []
        for app in await self._applications.list_in_flight(limit=limit):
            if app.state is ApplicationState.SUBMITTING:
                target = ApplicationState.SUBMISSION_STATE_UNKNOWN
                event = ApplicationEventType.SUBMISSION_STATE_UNKNOWN
            elif app.state is ApplicationState.PREPARING:
                target, event = ApplicationState.FAILED, ApplicationEventType.FAILED
            else:
                continue
            moved = app.transition_to(target, at=now)
            moved = await self._applications.upsert(moved)
            await self._append(moved, event, now=now,
                             detail="recovered from an interrupted run")
            recovered.append(moved)
        return tuple(recovered)

    # -- helpers -------------------------------------------------------------

    async def _evaluate_gate(self, app: Application, *,
                            adapter_safety: AdapterSafetyLevel, now: datetime,
                            ) -> ExecutionAuthorization:
        """Load the pair's current policy/eligibility/match and evaluate the gate.

        Everything is re-read here, which is what makes the submit-time evaluation
        honest (§5): the policy is whatever it is *now*, and the rate counts are
        against this instant.
        """
        decision = await self._decisions.get(app.user_id, app.decision_id)
        policy = await self._policies.get_default(app.user_id)
        match = None
        eligibility = None
        opportunity_type = None
        if app.opportunity_id is not None:
            match = await self._matches.get_for_pair(
                app.user_id, app.candidate_profile_id, app.opportunity_id)
            eligibility = await self._eligibilities.get_for_pair(
                app.user_id, app.candidate_profile_id, app.opportunity_id)
            opportunity = await self._opportunities.get(app.opportunity_id)
            opportunity_type = (opportunity.opportunity_type
                               if opportunity is not None else None)
        today, week = await self._rate_counts(app.user_id, now)
        # A missing decision at gate time is itself a block: there is no intent left.
        if decision is None or policy is None:
            return _no_authority(decision)
        return ApplicationExecutionGate.evaluate(
            decision=decision, policy=policy, adapter_safety=adapter_safety,
            opportunity_type=opportunity_type, eligibility=eligibility, match=match,
            submitted_today=today, submitted_this_week=week)

    async def _rate_counts(self, user_id: UserId,
                          now: datetime) -> tuple[int, int]:
        instant = now.astimezone(UTC)
        start_day = instant.replace(hour=0, minute=0, second=0, microsecond=0)
        start_week = start_day - timedelta(days=instant.weekday())
        today = await self._applications.count_active_submissions_since(
            user_id, start_day)
        week = await self._applications.count_active_submissions_since(
            user_id, start_week)
        return today, week

    async def _build_context(self, app: Application) -> ApplicationContext:
        profile = await self._profiles.get_default(app.user_id)
        if profile is None:
            raise CandidateProfileNotFound(str(app.user_id))
        decision = await self._decisions.get(app.user_id, app.decision_id)
        if decision is None:
            raise ApplicationDecisionMissing(str(app.id))
        opportunity = None
        documents: tuple[CandidateDocument, ...] = ()
        if app.opportunity_id is not None:
            opportunity = await self._opportunities.get(app.opportunity_id)
            documents = await self._documents_for(app, app.opportunity_id)
        return ApplicationContext(
            application=app, decision=decision, profile=profile,
            opportunity=opportunity, documents=documents,
            pinned_documents=app.pinned_documents, answers=app.answers,
            expected_form_fingerprint=app.form_fingerprint,
            correlation_id=app.correlation_id)

    async def _documents_for(self, app: Application,
                            opportunity_id: OpportunityId
                            ) -> tuple[CandidateDocument, ...]:
        found: list[CandidateDocument] = []
        for document_type in (CandidateDocumentType.RESUME,
                             CandidateDocumentType.COVER_LETTER):
            document = await self._documents.get_for_pair(
                app.user_id, app.candidate_profile_id, opportunity_id, document_type)
            if document is not None:
                found.append(document)
        return tuple(found)

    def _pin_documents(self, documents: tuple[CandidateDocument, ...], *,
                      now: datetime) -> tuple[PinnedDocument, ...]:
        """Pin the newest rendered version of each document for submission (§14-16)."""
        pinned: list[PinnedDocument] = []
        for document in documents:
            version = _latest_rendered(document)
            if version is not None:
                pinned.append(PinnedDocument(
                    document_id=document.id, version_id=version.id,
                    version=version.version, document_type=document.document_type))
        return tuple(pinned)

    async def _open_attempt(self, app: Application, adapter_key: str, *,
                           now: datetime) -> SubmissionAttempt:
        attempt = SubmissionAttempt(
            id=submission_attempt_id(app.id, app.attempt_count),
            application_id=app.id, attempt_number=app.attempt_count,
            adapter_key=adapter_key, correlation_id=app.correlation_id,
            started_at=now)
        return await self._attempts.upsert(attempt)

    async def _append(self, app: Application, event_type: ApplicationEventType, *,
                     now: datetime, detail: str | None = None,
                     reasons: tuple[Reason, ...] = (),
                     actor: ApplicationEventActor = ApplicationEventActor.SYSTEM,
                     ) -> ApplicationEvent:
        event = ApplicationEvent(
            id=new_application_event_id(), application_id=app.id,
            event_type=event_type, actor=actor, to_state=app.state,
            detail=detail, reasons=reasons, correlation_id=app.correlation_id,
            occurred_at=now)
        return await self._events.append(event)


_OUTCOME_EVENT: dict[SubmissionOutcome, ApplicationEventType] = {
    SubmissionOutcome.SUBMITTED: ApplicationEventType.SUBMITTED,
    SubmissionOutcome.REQUIRES_HUMAN: ApplicationEventType.HUMAN_REQUIRED,
    SubmissionOutcome.FAILED: ApplicationEventType.FAILED,
    SubmissionOutcome.STATE_UNKNOWN: ApplicationEventType.SUBMISSION_STATE_UNKNOWN,
}


def _resolve_channel(application_url: str | None) -> ApplicationChannel:
    """The channel a target implies, before the registry maps it to an adapter.

    A posting with a URL is something a browser can drive; one without is a manual
    hand-off. The registry then resolves the channel to an adapter, degrading an
    unserved channel to the generic one (§12-13) — so this only has to name the
    natural route, not know which adapters are deployed.
    """
    if application_url is None:
        return ApplicationChannel.MANUAL
    return ApplicationChannel.BROWSER


def _resolve_answers(
    prep: AdapterPreparation,
) -> tuple[tuple[ApplicationAnswer, ...], tuple[HumanRequiredReason, ...]]:
    """Resolve each question deterministically, collecting the answers and the gaps.

    The one place a proposal becomes an answer (§28-29): `resolve_answer` fills a
    field only from a trustworthy value, routes a sensitive or unanswerable-required
    field to a `HumanRequiredReason`, and leaves an optional blank as nothing. A model
    never fills a field on its own here.
    """
    proposals = {p.question_key: p for p in prep.answer_proposals}
    answers: list[ApplicationAnswer] = []
    reasons: list[HumanRequiredReason] = []
    for question in prep.questions:
        outcome = resolve_answer(question, proposals.get(question.key))
        if isinstance(outcome, ApplicationAnswer):
            answers.append(outcome)
        elif isinstance(outcome, HumanRequiredReason):
            reasons.append(outcome)
    return tuple(answers), tuple(reasons)


def _first_unready_pin(pinned: tuple[PinnedDocument, ...],
                      documents: tuple[CandidateDocument, ...]) -> str | None:
    """A message naming the first pinned version that is not send-ready, or `None`.

    The submit-time re-check of §15: a pinned version must still be exactly a
    RENDERED, cleared artifact. Anything else — a version regenerated past the pin, a
    document no longer present — means there is nothing safe to submit.
    """
    by_id = {document.id: document for document in documents}
    for pin in pinned:
        document = by_id.get(pin.document_id)
        version = document.version(pin.version) if document is not None else None
        if version is None or version.status is not DocumentStatus.RENDERED \
                or version.artifact is None:
            return "a pinned document version is no longer a rendered, cleared artifact"
    return None


def _latest_rendered(document: CandidateDocument) -> DocumentVersion | None:
    for version in reversed(document.versions):
        if version.status is DocumentStatus.RENDERED and version.artifact is not None:
            return version
    return None


def _has_rate_limit(reasons: tuple[Reason, ...]) -> bool:
    return any(reason.code == _RATE_LIMIT_REASON_CODE for reason in reasons)


def _dedupe(reasons: tuple[HumanRequiredReason, ...]
            ) -> tuple[HumanRequiredReason, ...]:
    seen: dict[HumanRequiredReason, None] = {}
    for reason in reasons:
        seen.setdefault(reason, None)
    return tuple(seen)


def _no_authority(decision: ApplicationDecision | None) -> ExecutionAuthorization:
    """A gate result for a missing decision or policy: block, do not submit."""
    code = "DECISION_MISSING" if decision is None else "POLICY_MISSING"
    detail = ("no decision backs this application" if decision is None
              else "no active policy authorizes this application")
    return ExecutionAuthorization(
        outcome=ExecutionOutcome.BLOCKED,
        reasons=(Reason(code=code, detail=detail),))
