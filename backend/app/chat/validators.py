"""Re-authorizing a confirmed proposal — where "proposal != permission" is enforced.

The parser (`backend.app.chat.parsing`) decides what the model *said*; this module decides
whether what it said may *run*, for this account, right now. A parsed `ChatAction` is a
well-formed request and nothing more — it has passed the domain union, so its shape and
arguments are valid, but it has not been checked against the state of the world. That
check is here, and it is the load-bearing half of Phase 13's rule: a proposal the model
emitted, that a human confirmed, is *still* refused if it names an entity that is not this
account's, or one in a state the action cannot touch.

The validator is deliberately narrow, and the narrowness is the point:

- **Ownership by reading, not by trusting the id.** Every entity an action names is loaded
  through a `user_id`-scoped repository, so a proposal about another account's application
  or search reads as absent and is rejected — never trusted because the id was well-formed
  or the model wrote it. Opportunities are the one shared fact (a posting belongs to no
  one) and are only checked to exist.
- **A coarse state guard, never the state machine.** For the application lifecycle it
  rejects only what is closed for good (`is_terminal`); the exact per-operation rule
  ("submit needs APPROVED", the whole Phase 12 gate) stays in `ApplicationService`, which
  the executor calls and whose refusal it maps to a rejected outcome. Re-encoding that rule
  here would be a second copy free to drift from the one the service enforces — so the
  validator does the drift-free check and leaves the authoritative one to its owner.
- **Read-only actions skip every check.** `NAVIGATE` and `OPEN_INTERVIEW_PREP` mutate
  nothing on the server, so there is nothing to authorize; they are always permitted and
  the executor records them without calling a service (`READ_ONLY_ACTION_KINDS`).

It returns a `ProposalValidation`, never raises for a refusal: a rejection is data the
executor records as a `REJECTED` outcome with a secret-free reason, not an exception. The
messages name only ids and states — the user's own data — so nothing here can leak a secret.
"""
from enum import StrEnum
from typing import assert_never
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.app.domain.chat import (
    READ_ONLY_ACTION_KINDS,
    ApproveApplicationAction,
    CancelApplicationAction,
    ChatAction,
    Conversation,
    ConversationScope,
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
from backend.app.domain.identifiers import (
    ApplicationId,
    OpportunityId,
    SearchProfileId,
    UserId,
)
from backend.app.domain.search import SearchAreaKind
from backend.app.repositories.contracts import (
    ApplicationRepository,
    OpportunityRepository,
    SearchProfileRepository,
)


class ProposalRejectionCode(StrEnum):
    """Why the validator refused a proposal — a stable, secret-free reason.

    A closed vocabulary so the frontend can render each refusal in its own words and the
    telemetry can count them, rather than parsing a free-text sentence. Each maps to a
    pre-execution check: the action fell outside the conversation's scope
    (`SCOPE_MISMATCH`), the entity was not this account's (`*_NOT_FOUND`), the application
    is closed for good (`APPLICATION_CLOSED`), or the search has no radius area to resize
    (`SEARCH_HAS_NO_RADIUS`).
    """

    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    OPPORTUNITY_NOT_FOUND = "OPPORTUNITY_NOT_FOUND"
    APPLICATION_NOT_FOUND = "APPLICATION_NOT_FOUND"
    APPLICATION_CLOSED = "APPLICATION_CLOSED"
    SEARCH_NOT_FOUND = "SEARCH_NOT_FOUND"
    SEARCH_HAS_NO_RADIUS = "SEARCH_HAS_NO_RADIUS"


class ProposalValidation(BaseModel):
    """The verdict on whether one confirmed action may run — permit, or a reason it may not.

    A value, not an exception: the executor records a rejection as a `REJECTED` outcome
    carrying `code` and `detail`, so "the platform refused this and why" is auditable data
    rather than a stack trace. `code` and `detail` are set together and only when refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    permitted: bool
    code: ProposalRejectionCode | None = None
    detail: str | None = None

    @classmethod
    def permit(cls) -> "ProposalValidation":
        """The action may run; the executor proceeds to the service call."""
        return cls(permitted=True)

    @classmethod
    def reject(cls, code: ProposalRejectionCode, detail: str) -> "ProposalValidation":
        """The action may not run; the executor records a `REJECTED` outcome."""
        return cls(permitted=False, code=code, detail=detail)


def action_scope_anchor(action: ChatAction) -> tuple[ConversationScope, UUID] | None:
    """The single domain resource an action is bound to, or `None` if it is read-only.

    A pure, read-free structural fact about the action's *shape*: which scope it belongs
    to and the id it names. Because it touches no repository, the proposal-creation gate
    (`backend.app.chat.conversation`) and the executor's validator can both call it — one
    rule, two callers, no drift. Exhaustive over the closed union: a new mutating action
    added without an anchor here is a type error, never a silently unscoped one.
    """
    match action:
        case NavigateAction() | OpenInterviewPrepAction():
            return None
        case (GenerateResumeAction() | GenerateCoverLetterAction()
              | CreateApplicationAction()):
            return ConversationScope.OPPORTUNITY, action.opportunity_id
        case (PrepareApplicationAction() | ApproveApplicationAction()
              | SubmitApplicationAction() | CancelApplicationAction()):
            return ConversationScope.APPLICATION, action.application_id
        case SetSearchRadiusAction() | UpdateSearchKeywordsAction():
            return ConversationScope.SEARCH_PROFILE, action.search_profile_id
        case _:  # pragma: no cover - exhaustiveness guard over the closed union
            assert_never(action)


def action_within_scope(scope: ConversationScope, scope_id: UUID | None,
                        action: ChatAction) -> bool:
    """Whether `action` may be proposed or run inside a thread of this scope.

    `GLOBAL` places no restriction — it may reference any of the account's resources, and
    ordinary ownership validation is what then checks each id. An anchored thread admits
    only an action whose own anchor is the *same* scope and the *same* id: an
    `APPLICATION(A)` thread thus refuses `SUBMIT_APPLICATION(B)` even when B is the user's,
    and a `COMPANY` thread — no action anchors to a company — admits no mutating action at
    all, so a cross-type flow must go through a `GLOBAL` thread rather than a mis-scoped
    one. Read-only actions have no anchor and are always in scope.
    """
    if scope is ConversationScope.GLOBAL:
        return True
    anchor = action_scope_anchor(action)
    if anchor is None:
        return True
    anchor_scope, anchor_id = anchor
    return anchor_scope is scope and anchor_id == scope_id


class ProposalValidator:
    """Re-authorizes a confirmed `ChatAction` against ownership and coarse domain state.

    Holds only the three read-side repositories the checks need, each `user_id`-scoped, so
    the validator can prove an entity belongs to the account but can neither mutate it nor
    reach anything else — the type of what it can read is the guarantee, exactly as the
    context builder's is. It calls no service and no provider: authorizing is a question,
    executing is the next step's answer.
    """

    def __init__(self, *, opportunities: OpportunityRepository,
                 applications: ApplicationRepository,
                 searches: SearchProfileRepository) -> None:
        self._opportunities = opportunities
        self._applications = applications
        self._searches = searches

    async def validate(self, user_id: UserId, action: ChatAction, *,
                       conversation: Conversation | None = None) -> ProposalValidation:
        """Whether `action` may run for `user_id`, or the reason it may not.

        Scope is the first wall, distinct from ownership: when a `conversation` is given
        and it is anchored, an action outside that anchor is refused `SCOPE_MISMATCH`
        before any read — an `APPLICATION(A)` thread cannot drive application B even though
        B is the user's. Passing no `conversation` (the executor's older two-argument call,
        a `GLOBAL` thread) skips the scope wall, and ordinary ownership validation stands
        alone. Exhaustive over the closed union by construction: `assert_never` makes a new
        `ChatActionKind` added without a branch here a type error, so a future mutating
        action cannot slip through unauthorized.
        """
        if conversation is not None and not action_within_scope(
                conversation.scope, conversation.scope_id, action):
            return ProposalValidation.reject(
                ProposalRejectionCode.SCOPE_MISMATCH,
                "this action is outside the conversation's "
                f"{conversation.scope.value} scope")
        if action.kind in READ_ONLY_ACTION_KINDS:
            return ProposalValidation.permit()
        match action:
            case NavigateAction() | OpenInterviewPrepAction():
                # Already handled by the read-only guard above; kept so the match is
                # exhaustive over the union and `assert_never` can prove it.
                return ProposalValidation.permit()
            case (GenerateResumeAction() | GenerateCoverLetterAction()
                  | CreateApplicationAction()):
                return await self._opportunity_exists(action.opportunity_id)
            case (PrepareApplicationAction() | ApproveApplicationAction()
                  | SubmitApplicationAction() | CancelApplicationAction()):
                return await self._application_actionable(user_id, action.application_id)
            case SetSearchRadiusAction():
                return await self._search_radius_settable(
                    user_id, action.search_profile_id)
            case UpdateSearchKeywordsAction():
                return await self._search_exists(user_id, action.search_profile_id)
            case _:  # pragma: no cover - exhaustiveness guard over the closed union
                assert_never(action)

    async def _opportunity_exists(
            self, opportunity_id: OpportunityId) -> ProposalValidation:
        """A posting is a shared fact: it need only exist to be referenced."""
        if await self._opportunities.get(opportunity_id) is None:
            return ProposalValidation.reject(
                ProposalRejectionCode.OPPORTUNITY_NOT_FOUND,
                f"no opportunity {opportunity_id} exists to act on")
        return ProposalValidation.permit()

    async def _application_actionable(
            self, user_id: UserId,
            application_id: ApplicationId) -> ProposalValidation:
        """The application must be this account's and not closed for good.

        The exact transition rule for the specific operation (prepare/approve/submit/
        cancel) is `ApplicationService`'s; this rejects only the terminal case no
        operation can act on, so the two checks never disagree.
        """
        app = await self._applications.get(user_id, application_id)
        if app is None:
            return ProposalValidation.reject(
                ProposalRejectionCode.APPLICATION_NOT_FOUND,
                f"no application {application_id} belongs to this account")
        if app.is_terminal:
            return ProposalValidation.reject(
                ProposalRejectionCode.APPLICATION_CLOSED,
                f"application is {app.state.value} and cannot change")
        return ProposalValidation.permit()

    async def _search_exists(self, user_id: UserId,
                             search_profile_id: SearchProfileId) -> ProposalValidation:
        if await self._searches.get(user_id, search_profile_id) is None:
            return ProposalValidation.reject(
                ProposalRejectionCode.SEARCH_NOT_FOUND,
                f"no saved search {search_profile_id} belongs to this account")
        return ProposalValidation.permit()

    async def _search_radius_settable(
            self, user_id: UserId,
            search_profile_id: SearchProfileId) -> ProposalValidation:
        """A radius can only be set on a search that has a radius area to resize."""
        search = await self._searches.get(user_id, search_profile_id)
        if search is None:
            return ProposalValidation.reject(
                ProposalRejectionCode.SEARCH_NOT_FOUND,
                f"no saved search {search_profile_id} belongs to this account")
        if not any(area.kind is SearchAreaKind.RADIUS for area in search.areas):
            return ProposalValidation.reject(
                ProposalRejectionCode.SEARCH_HAS_NO_RADIUS,
                "this search has no radius area to resize")
        return ProposalValidation.permit()
