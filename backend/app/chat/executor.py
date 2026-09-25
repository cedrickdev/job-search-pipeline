"""Executing a confirmed proposal — the last gate, and the only place an action runs.

This is the far end of "prose has zero authority". The parser decided what the model
*said*, the validator decided whether what it said *may* run for this account right now,
and this module is where a proposal that survived both — and that a human confirmed —
finally reaches a service. Nothing upstream mutated anything; every mutation the chat can
cause happens here, by calling the same application services the HTTP routes call, with
no override and no shortcut. The chat is one more caller of those boundaries, never a way
around them.

Four properties make the executor safe to hand a confirmed proposal:

- **It re-authorizes before it runs.** Even a confirmed proposal is passed through the
  `ProposalValidator` first (ownership, coarse domain state), and the service it then
  calls runs its own authoritative checks — the Phase 12 gate for a submission, the
  evidence guard for a document. A refusal at either layer is recorded, never executed:
  `proposal != permission`, restated one last time at the moment of action.
- **It is idempotent by the proposal's id.** The execution row's id is derived from the
  proposal alone, so a double-clicked "Confirm" or a retried request finds the recorded
  execution and returns it rather than running the underlying action twice. The status
  guard (`is_open`) is the second half; the derived id is what makes the common path
  idempotent by construction rather than by a caught race.
- **Every attempt is audited, refusal included.** The outcome — `SUCCEEDED`, `REJECTED`
  or `FAILED` — is written as a `ChatActionExecution` with a secret-free detail and the
  proposal's status moved to match, so "the platform refused this, and why" is data the
  chat can render, not a dropped attempt.
- **A detail never leaks a secret.** A refusal surfaces the service's own typed,
  domain-composed message (ids, states, a failure code — all secret-free by
  construction); an *unexpected* failure is recorded with a fixed generic sentence and
  the raw exception text is dropped, because it could quote a driver, a URL or a token.

Read-only actions (navigate, open prep) mutate nothing on the server, so the executor
records a `SUCCEEDED`, side-effect-free execution for them without calling any service —
the outcome is a client-side hint, and the frontend reads the confirmed action to route.
"""
from datetime import datetime
from typing import assert_never

from backend.app.chat.validators import ProposalValidator
from backend.app.documents import InsufficientEvidence
from backend.app.domain.application_failure import ApplicationError
from backend.app.domain.base import LanguageCode
from backend.app.domain.chat import (
    READ_ONLY_ACTION_KINDS,
    ApproveApplicationAction,
    CancelApplicationAction,
    ChatAction,
    ChatActionExecution,
    ChatActionExecutionOutcome,
    ChatActionProposal,
    ChatActionProposalStatus,
    CreateApplicationAction,
    GenerateCoverLetterAction,
    GenerateResumeAction,
    NavigateAction,
    OpenInterviewPrepAction,
    PrepareApplicationAction,
    SetSearchRadiusAction,
    SubmitApplicationAction,
    UpdateSearchKeywordsAction,
)
from backend.app.domain.documents import CandidateDocumentType
from backend.app.domain.identifiers import (
    ChatActionProposalId,
    OpportunityId,
    UserId,
    chat_action_execution_id,
)
from backend.app.repositories.contracts import (
    ChatActionExecutionRepository,
    ChatActionProposalRepository,
    ConversationRepository,
)
from backend.app.services.applications import (
    ApplicationDecisionMissing,
    ApplicationNotActionable,
    ApplicationNotFound,
    ApplicationService,
)
from backend.app.services.assessment import (
    CandidateProfileNotFound,
    OpportunityNotFound,
)
from backend.app.services.documents import DocumentService
from backend.app.services.onboarding import OnboardingService, SearchProfileNotFound

# The typed refusals a service raises when a confirmed action names something it may not
# act on *right now* — the authoritative per-operation checks the coarse validator
# deliberately leaves to the service: a submit that is not APPROVED, a create with no
# decision behind it, a duplicate already submitted, an exhausted rate budget, too little
# evidence to build a truthful document. Each is a "not permitted", so the executor
# records it as REJECTED carrying the exception's own secret-free message. Anything *not*
# in this tuple is an unexpected failure and becomes FAILED with a fixed generic detail —
# never the raw exception text, which could carry something a message must not.
_REJECTION_EXCEPTIONS: tuple[type[Exception], ...] = (
    ApplicationNotFound,
    ApplicationDecisionMissing,
    ApplicationNotActionable,
    ApplicationError,
    CandidateProfileNotFound,
    OpportunityNotFound,
    SearchProfileNotFound,
    InsufficientEvidence,
)

# The proposal status each execution outcome drives the proposal into. A SUCCEEDED
# execution marks the proposal EXECUTED (the confirmation ran); a REJECTED one marks it
# REJECTED (it was never permitted); a FAILED one marks it FAILED (permitted, but the
# service call did not complete). One map, so the outcome and the status cannot drift.
_STATUS_FOR_OUTCOME: dict[ChatActionExecutionOutcome, ChatActionProposalStatus] = {
    ChatActionExecutionOutcome.SUCCEEDED: ChatActionProposalStatus.EXECUTED,
    ChatActionExecutionOutcome.REJECTED: ChatActionProposalStatus.REJECTED,
    ChatActionExecutionOutcome.FAILED: ChatActionProposalStatus.FAILED,
}

# The detail recorded when a permitted action's service call raises something the executor
# did not anticipate. Deliberately generic and secret-free: an unexpected exception's own
# text may quote a database driver, a URL or a credential, so it is dropped rather than
# surfaced (mirroring `classify_adapter_failure`, which drops an adapter's message).
_UNEXPECTED_FAILURE_DETAIL = (
    "the action was permitted but could not be completed due to an unexpected error")


class ChatProposalNotFound(Exception):
    """No proposal is stored under that id for this account (or it is not yours).

    User-owned, so "no such proposal" and "not yours" are one condition, for the reason
    every other user-scoped read here folds them together: a caller that could tell them
    apart could probe another account's proposals by id. The API maps it to a 404.
    """


class ChatProposalNotActionable(Exception):
    """The proposal is no longer open, and nothing was ever executed for it.

    A `PROPOSED` proposal is the only one a confirm may act on. One already executed,
    rejected or failed is short-circuited to its recorded execution before this is
    reached — the execution id derives from the proposal, so its audit is always found —
    so this fires only for a stale confirm of a *dismissed* proposal, which has no
    execution to return. The API maps it to a 409.
    """

    def __init__(self, status: ChatActionProposalStatus) -> None:
        super().__init__(f"a proposal in status {status.value} cannot be executed")
        self.status = status


class ChatActionExecutor:
    """Runs a confirmed proposal onto the existing services — validate, dispatch, record.

    Holds the two chat repositories it audits through, the `ProposalValidator` it
    re-authorizes with, and the three application services a mutating action maps to
    (`DocumentService`, `ApplicationService`, `OnboardingService`). It reaches no provider
    and drives no browser: it calls typed service methods and records what they return, so
    the chat inherits every safety rule those services already enforce rather than
    re-implementing — or weakening — any of them.
    """

    def __init__(self, *, proposals: ChatActionProposalRepository,
                 executions: ChatActionExecutionRepository,
                 conversations: ConversationRepository,
                 validator: ProposalValidator,
                 documents: DocumentService,
                 applications: ApplicationService,
                 onboarding: OnboardingService) -> None:
        self._proposals = proposals
        self._executions = executions
        self._conversations = conversations
        self._validator = validator
        self._documents = documents
        self._applications = applications
        self._onboarding = onboarding

    async def execute(self, user_id: UserId, proposal_id: ChatActionProposalId, *,
                      now: datetime) -> ChatActionExecution:
        """Execute one confirmed proposal for `user_id`, recording an audited outcome.

        The whole control-plane rule in one method:

        1. the proposal is loaded *scoped by the owner*, so a confirmation naming another
           account's proposal reads as absent and raises `ChatProposalNotFound`;
        2. an execution already recorded for it is returned unchanged — the id derives
           from the proposal, so a double-clicked "Confirm" collapses onto the one row and
           the underlying action never runs twice;
        3. a proposal no longer `PROPOSED` (a dismissed one) raises
           `ChatProposalNotActionable` rather than running;
        4. the `ProposalValidator` re-authorizes it against the proposal's own
           conversation — scope first, then ownership and coarse domain state; a refusal is
           recorded as a `REJECTED` execution and the proposal moves to `REJECTED`;
        5. a read-only action is recorded `SUCCEEDED` without calling any service — there
           is nothing on the server to change;
        6. a mutating action is dispatched to its service inside a guard that maps a typed
           precondition refusal to `REJECTED`, any other failure to `FAILED`, and a normal
           return to `SUCCEEDED`.
        """
        proposal = await self._proposals.get(user_id, proposal_id)
        if proposal is None:
            raise ChatProposalNotFound(str(proposal_id))
        existing = await self._executions.get(user_id, proposal_id)
        if existing is not None:
            return existing
        if not proposal.is_open:
            raise ChatProposalNotActionable(proposal.status)

        conversation = await self._conversations.get(user_id, proposal.conversation_id)
        validation = await self._validator.validate(
            user_id, proposal.action, conversation=conversation)
        if not validation.permitted:
            return await self._record(
                proposal, ChatActionExecutionOutcome.REJECTED,
                detail=validation.detail,
                result_ref=validation.code.value if validation.code else None,
                now=now)

        if proposal.action.kind in READ_ONLY_ACTION_KINDS:
            result_ref, detail = _read_only_result(proposal.action)
            return await self._record(
                proposal, ChatActionExecutionOutcome.SUCCEEDED,
                detail=detail, result_ref=result_ref, now=now)

        try:
            result_ref, detail = await self._perform(user_id, proposal.action, now=now)
        except _REJECTION_EXCEPTIONS as exc:
            return await self._record(
                proposal, ChatActionExecutionOutcome.REJECTED,
                detail=_rejection_detail(exc), result_ref=_rejection_ref(exc), now=now)
        except Exception:
            return await self._record(
                proposal, ChatActionExecutionOutcome.FAILED,
                detail=_UNEXPECTED_FAILURE_DETAIL, result_ref=None, now=now)
        return await self._record(
            proposal, ChatActionExecutionOutcome.SUCCEEDED,
            detail=detail, result_ref=result_ref, now=now)

    async def dismiss(self, user_id: UserId, proposal_id: ChatActionProposalId, *,
                      now: datetime) -> ChatActionProposal:
        """Decline an open proposal without running it — `PROPOSED` → `DISMISSED`.

        The user's other move on a proposal, and the quiet one: it writes no
        `ChatActionExecution` because nothing was attempted, only advances the proposal's
        status so it leaves the open set and cannot later be confirmed. Scoped and guarded
        exactly as `execute`'s opening steps are — a proposal that is not this account's
        reads as absent (`ChatProposalNotFound`), and one no longer `PROPOSED` (already
        executed, rejected, failed or dismissed) raises `ChatProposalNotActionable` rather
        than being dismissed a second time.
        """
        proposal = await self._proposals.get(user_id, proposal_id)
        if proposal is None:
            raise ChatProposalNotFound(str(proposal_id))
        if not proposal.is_open:
            raise ChatProposalNotActionable(proposal.status)
        return await self._proposals.upsert(proposal.model_copy(update={
            "status": ChatActionProposalStatus.DISMISSED, "updated_at": now}))

    async def _perform(self, user_id: UserId, action: ChatAction, *,
                       now: datetime) -> tuple[str, str]:
        """Call the one service the action maps to, and describe what it produced.

        Returns `(result_ref, detail)` for a `SUCCEEDED` execution; raises the service's
        own typed exception, which `execute` maps to `REJECTED` or `FAILED`. Exhaustive
        over the *mutating* members of the union — the read-only kinds are recorded before
        this is reached, and `assert_never` proves nothing else can arrive here, so a new
        mutating action added without a branch is a type error rather than a silent skip.
        """
        match action:
            case GenerateResumeAction():
                return await self._generate(
                    user_id, action.opportunity_id, CandidateDocumentType.RESUME,
                    language=action.target_language, now=now)
            case GenerateCoverLetterAction():
                return await self._generate(
                    user_id, action.opportunity_id, CandidateDocumentType.COVER_LETTER,
                    language=action.target_language, now=now)
            case CreateApplicationAction():
                app = await self._applications.create(
                    user_id, action.opportunity_id, now=now)
                return str(app.id), f"application is {app.state.value}"
            case PrepareApplicationAction():
                app = await self._applications.prepare(
                    user_id, action.application_id, now=now)
                return str(app.id), f"application is now {app.state.value}"
            case ApproveApplicationAction():
                app = await self._applications.approve(
                    user_id, action.application_id, now=now)
                return str(app.id), f"application is now {app.state.value}"
            case SubmitApplicationAction():
                app = await self._applications.submit(
                    user_id, action.application_id, now=now)
                return str(app.id), f"application is now {app.state.value}"
            case CancelApplicationAction():
                app = await self._applications.cancel(
                    user_id, action.application_id, now=now)
                return str(app.id), f"application is now {app.state.value}"
            case SetSearchRadiusAction():
                search = await self._onboarding.set_search_radius(
                    user_id, action.search_profile_id,
                    radius_km=action.radius_km, now=now)
                return str(search.id), f"radius set to {action.radius_km:g} km"
            case UpdateSearchKeywordsAction():
                search = await self._onboarding.set_search_keywords(
                    user_id, action.search_profile_id,
                    title_keywords=action.title_keywords,
                    excluded_keywords=action.excluded_keywords, now=now)
                return str(search.id), _keywords_detail(action)
            case NavigateAction() | OpenInterviewPrepAction():
                # Read-only kinds are recorded by `execute` before dispatch; kept so the
                # match is exhaustive and `assert_never` can prove nothing mutating slips
                # past unhandled.
                raise AssertionError("read-only action reached the mutating dispatch")
            case _:  # pragma: no cover - exhaustiveness guard over the closed union
                assert_never(action)

    async def _generate(self, user_id: UserId, opportunity_id: OpportunityId,
                        document_type: CandidateDocumentType, *,
                        language: LanguageCode | None,
                        now: datetime) -> tuple[str, str]:
        """Generate one document version and describe the newest version it produced.

        A guard rejection is not a failure of *this* action: the generate ran and
        produced a document whose newest version may be RENDERED or REJECTED, and either
        is a `SUCCEEDED` execution whose detail names the version's status. Too little
        evidence to build anything truthful, though, raises `InsufficientEvidence`, which
        `execute` maps to a REJECTED execution.
        """
        document = await self._documents.generate(
            user_id, opportunity_id, document_type, now=now, language=language)
        version = document.versions[-1]
        label = ("résumé" if document_type is CandidateDocumentType.RESUME
                 else "cover letter")
        return (str(document.id),
                f"generated {label} v{version.version} ({version.status.value})")

    async def _record(self, proposal: ChatActionProposal,
                      outcome: ChatActionExecutionOutcome, *,
                      detail: str | None, result_ref: str | None,
                      now: datetime) -> ChatActionExecution:
        """Write the execution audit and move the proposal to the matching status.

        The two writes are the executor's whole footprint: the `ChatActionExecution` keyed
        on the proposal's derived id (so a retry lands on the one row) and the proposal's
        `status` advanced off `PROPOSED`. Written in this order so the audit exists before
        the proposal is marked done.
        """
        execution = ChatActionExecution(
            id=chat_action_execution_id(proposal.id),
            proposal_id=proposal.id, user_id=proposal.user_id,
            outcome=outcome, detail=detail, result_ref=result_ref, created_at=now)
        stored = await self._executions.upsert(execution)
        await self._proposals.upsert(proposal.model_copy(
            update={"status": _STATUS_FOR_OUTCOME[outcome], "updated_at": now}))
        return stored


def _read_only_result(action: ChatAction) -> tuple[str, str]:
    """The client-side hint a read-only action records — a target, and a human sentence.

    No server state changes, so there is nothing to produce but the hint itself: the
    frontend routes off the confirmed action, and `result_ref` is the destination for the
    audit to read at a glance.
    """
    if isinstance(action, NavigateAction):
        detail = f"navigate to {action.target.value}"
        if action.opportunity_id is not None:
            detail += f" for opportunity {action.opportunity_id}"
        return action.target.value, detail
    if isinstance(action, OpenInterviewPrepAction):
        return (str(action.opportunity_id),
                f"open interview prep for opportunity {action.opportunity_id}")
    raise AssertionError("non-read-only action passed to _read_only_result")


def _keywords_detail(action: UpdateSearchKeywordsAction) -> str:
    """A sentence naming which keyword lists an update changed, and how."""
    parts: list[str] = []
    if action.title_keywords is not None:
        parts.append("title keywords " + (", ".join(action.title_keywords)
                                           if action.title_keywords else "cleared"))
    if action.excluded_keywords is not None:
        parts.append("excluded keywords " + (", ".join(action.excluded_keywords)
                                             if action.excluded_keywords else "cleared"))
    return "updated " + "; ".join(parts) if parts else "no keyword change requested"


def _rejection_detail(exc: Exception) -> str:
    """A secret-free reason for a service-level refusal.

    Every exception in `_REJECTION_EXCEPTIONS` carries only ids, states or a typed code in
    its message — the domain composes them and never lets an adapter's raw text through,
    and `ApplicationError.detail` is the guarded, length-capped sentence — so surfacing
    the message is safe and more useful than a generic one.
    """
    if isinstance(exc, ApplicationError):
        return exc.detail
    return str(exc)


def _rejection_ref(exc: Exception) -> str | None:
    """The stable code a refusal carries when it has one, for the frontend to branch on."""
    if isinstance(exc, ApplicationError):
        return exc.code.value
    return None
