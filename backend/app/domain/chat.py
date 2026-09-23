"""Career Chat as a control plane: the conversation, and the typed actions it proposes.

Phase 13 turns the chat into a safe way to *drive* the platform, and this module holds
the one idea the whole phase rests on: **prose has zero authority**. A sentence the
model streams to the user changes nothing. The only thing that can reach an application
service is a `ChatAction` — a typed, validated proposal the model emits in a fenced
block, that the layer parses independently of the prose, that a human then confirms, and
that is *still* re-checked (ownership, domain state, policy, eligibility, the execution
gate) before it runs. A proposal is a request to act, never a permission to
(docs/… §…, mirroring the CLAUDE.md rule that LLM output proposes typed actions and the
application validates and executes them).

Two consequences shape the models here:

- **The action union is closed and `extra="forbid"`.** A model that proposes an action
  the platform does not offer, or an action with an invented argument, fails to parse
  into a `ChatAction` — it does not become a half-understood command. The closed
  `ChatActionKind` is the whole vocabulary the chat may ever propose, exactly as
  `TaskPurpose` and `PromptName` are closed for the same reason.
- **Every action names an id the executor re-authorizes.** An action carries the id of
  the opportunity, application or search it acts on, never an embedded copy of the
  entity — the executor loads that entity *scoped by the user* at execution time, so a
  proposal about another account's application reads as absent and is refused rather
  than trusted because the model wrote it.

These are pure domain values: `backend.app.domain` imports the standard library and
Pydantic and nothing else, so the chat's grammar cannot grow a dependency on a provider,
a session or a repository (docs/ARCHITECTURE.md §1). The service layer under
`backend.app.chat` is what parses prose into these, validates them, and maps a confirmed
one onto the existing services.
"""
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from backend.app.domain.base import DomainModel, LanguageCode, NonEmptyStr, UtcDatetime
from backend.app.domain.identifiers import (
    ApplicationId,
    ChatActionExecutionId,
    ChatActionProposalId,
    ChatMessageId,
    ConversationId,
    LLMRunId,
    OpportunityId,
    SearchProfileId,
    UserId,
)

# The largest search radius a proposal may set, in kilometres — the same ceiling
# `RadiusSearchArea` enforces, restated here so a chat-proposed radius the domain would
# refuse is refused at parse time rather than at the repository.
_MAX_RADIUS_KM: float = 500.0

# How many keywords a single search-update action may carry. A bound, not a guess: a
# model that emitted a thousand title keywords would be building a filter no human wrote,
# and the allow-list is small in practice (docs/V2_SPECIFICATION.md §…).
_MAX_KEYWORDS: int = 50


class ChatMessageRole(StrEnum):
    """Who authored one stored chat message.

    Only two roles are ever persisted, and that is deliberate. `SYSTEM` is the prompt,
    which lives in the versioned `PromptRegistry` and is never a row a user could read or
    replay; `TOOL` results do not exist here because the chat never lets the *provider*
    run a tool — the application executes confirmed actions, out of band, and records the
    outcome as its own audit rather than feeding it back as a message role.
    """

    USER = "USER"
    ASSISTANT = "ASSISTANT"


class ChatActionKind(StrEnum):
    """Every typed action the career chat may propose — the closed control vocabulary.

    Four families, one member per operation the chat is allowed to drive, and nothing
    else: a member here is the only way an intent can travel from the model to a service.
    Adding a capability to the chat is adding a member and its executor branch, never a
    free string at a call site — which is what keeps "the chat can do X" an answerable
    question (§19, §125). Interview actions are limited to the two existing V1 behaviours
    (open prep, navigate to prep); the simulator itself is Phase 14 and out of scope.
    """

    # Document family — tailoring the candidate's own materials to a posting.
    GENERATE_RESUME = "GENERATE_RESUME"
    GENERATE_COVER_LETTER = "GENERATE_COVER_LETTER"
    # Application family — the lifecycle, every step still policed by the Phase 12 engine.
    CREATE_APPLICATION = "CREATE_APPLICATION"
    PREPARE_APPLICATION = "PREPARE_APPLICATION"
    APPROVE_APPLICATION = "APPROVE_APPLICATION"
    SUBMIT_APPLICATION = "SUBMIT_APPLICATION"
    CANCEL_APPLICATION = "CANCEL_APPLICATION"
    # Search-preference family — bounded edits to a saved search.
    SET_SEARCH_RADIUS = "SET_SEARCH_RADIUS"
    UPDATE_SEARCH_KEYWORDS = "UPDATE_SEARCH_KEYWORDS"
    # Read / navigate family — a client-side hint that mutates nothing on the server.
    NAVIGATE = "NAVIGATE"
    OPEN_INTERVIEW_PREP = "OPEN_INTERVIEW_PREP"


class NavigationTarget(StrEnum):
    """A screen a `NAVIGATE` action may point the client at.

    Closed on purpose: navigation is a client-side hint, but letting the model name an
    arbitrary path would be an open redirect dressed as a chat action. The client maps
    each member to a route it owns; a target the client does not recognise is simply
    ignored, never followed.
    """

    OPPORTUNITIES = "OPPORTUNITIES"
    APPLICATIONS = "APPLICATIONS"
    DOCUMENTS = "DOCUMENTS"
    MATCHES = "MATCHES"
    COMPANIES = "COMPANIES"
    INTERVIEW_PREP = "INTERVIEW_PREP"
    SETTINGS = "SETTINGS"


class ChatActionProposalStatus(StrEnum):
    """Where one proposal is in its life from "offered" to "done".

    A proposal is born `PROPOSED`. A human confirming it that succeeds moves it to
    `EXECUTED`; one that the executor refuses at validation moves it to `REJECTED`
    (it was never permitted); one whose underlying service action failed moves it to
    `FAILED` (it was permitted but did not complete). `DISMISSED` is the user declining
    it. Only a `PROPOSED` proposal may be executed or dismissed — the others are terminal.
    """

    PROPOSED = "PROPOSED"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    DISMISSED = "DISMISSED"


class ChatActionExecutionOutcome(StrEnum):
    """How one attempt to execute a confirmed proposal ended.

    Told apart from the proposal's status because an execution is the *event* and the
    status is the proposal's resulting *state*: a `REJECTED` outcome is a proposal the
    executor would not permit, a `FAILED` outcome is one it permitted but whose service
    call raised, and `SUCCEEDED` is the action having run.
    """

    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


# --- the typed action union ------------------------------------------------------------


class GenerateResumeAction(DomainModel):
    """Tailor the candidate's résumé to one posting (executes `DocumentService`)."""

    kind: Literal[ChatActionKind.GENERATE_RESUME] = ChatActionKind.GENERATE_RESUME
    opportunity_id: OpportunityId
    target_language: LanguageCode | None = None


class GenerateCoverLetterAction(DomainModel):
    """Draft the candidate's cover letter for one posting (executes `DocumentService`)."""

    kind: Literal[ChatActionKind.GENERATE_COVER_LETTER] = (
        ChatActionKind.GENERATE_COVER_LETTER)
    opportunity_id: OpportunityId
    target_language: LanguageCode | None = None


class CreateApplicationAction(DomainModel):
    """Open an application for a posting from its stored decision (§2-3, §36)."""

    kind: Literal[ChatActionKind.CREATE_APPLICATION] = ChatActionKind.CREATE_APPLICATION
    opportunity_id: OpportunityId


class PrepareApplicationAction(DomainModel):
    """Prepare an application and route it by the gate — reversible, never submits."""

    kind: Literal[ChatActionKind.PREPARE_APPLICATION] = (
        ChatActionKind.PREPARE_APPLICATION)
    application_id: ApplicationId


class ApproveApplicationAction(DomainModel):
    """Record a human's approval of a prepared application (§52)."""

    kind: Literal[ChatActionKind.APPROVE_APPLICATION] = (
        ChatActionKind.APPROVE_APPLICATION)
    application_id: ApplicationId


class SubmitApplicationAction(DomainModel):
    """Submit an approved application — the irreversible boundary (§1, §5, §80-88).

    Carries no override of any kind: the executor hands this to `ApplicationService.submit`,
    which re-evaluates the Phase 12 gate (policy, eligibility, rate budget, the
    concurrency-safe reservation) exactly as the HTTP route does. The chat is one more
    caller of that boundary, never a way around it.
    """

    kind: Literal[ChatActionKind.SUBMIT_APPLICATION] = ChatActionKind.SUBMIT_APPLICATION
    application_id: ApplicationId


class CancelApplicationAction(DomainModel):
    """Abandon an application before it reaches the employer."""

    kind: Literal[ChatActionKind.CANCEL_APPLICATION] = ChatActionKind.CANCEL_APPLICATION
    application_id: ApplicationId


class SetSearchRadiusAction(DomainModel):
    """Change the radius of a saved search's radius area(s) (executes `OnboardingService`)."""

    kind: Literal[ChatActionKind.SET_SEARCH_RADIUS] = ChatActionKind.SET_SEARCH_RADIUS
    search_profile_id: SearchProfileId
    radius_km: Annotated[float, Field(gt=0.0, le=_MAX_RADIUS_KM)]


class UpdateSearchKeywordsAction(DomainModel):
    """Replace a saved search's title and/or excluded keyword lists.

    Both fields are optional and default to "leave unchanged" (`None`); an empty tuple is
    a real value that clears a list, which matches the domain's convention that an empty
    allow-list restricts nothing. A field the model omits is not touched, so a proposal to
    change the title keywords cannot silently wipe the exclusions.
    """

    kind: Literal[ChatActionKind.UPDATE_SEARCH_KEYWORDS] = (
        ChatActionKind.UPDATE_SEARCH_KEYWORDS)
    search_profile_id: SearchProfileId
    title_keywords: Annotated[tuple[NonEmptyStr, ...],
                              Field(max_length=_MAX_KEYWORDS)] | None = None
    excluded_keywords: Annotated[tuple[NonEmptyStr, ...],
                                 Field(max_length=_MAX_KEYWORDS)] | None = None


class NavigateAction(DomainModel):
    """Point the client at a screen — a hint that mutates nothing on the server."""

    kind: Literal[ChatActionKind.NAVIGATE] = ChatActionKind.NAVIGATE
    target: NavigationTarget
    opportunity_id: OpportunityId | None = None


class OpenInterviewPrepAction(DomainModel):
    """Open the existing V1 interview-prep view for a posting (client-side, no mutation).

    The only interview capability the chat exposes, and deliberately so: the adaptive
    simulator, scoring and readiness are Phase 14 and out of scope (§19). This is a
    navigation hint to a view V1 already ships, nothing more.
    """

    kind: Literal[ChatActionKind.OPEN_INTERVIEW_PREP] = (
        ChatActionKind.OPEN_INTERVIEW_PREP)
    opportunity_id: OpportunityId


# The discriminated union the layer parses a fenced proposal into. `kind` is the
# discriminator, so Pydantic selects exactly one member and reports a precise error for a
# `kind` that is not a member — never a silently widened match. `CHAT_ACTION_ADAPTER` is
# the one reusable validator the parser and the mappers share, so there is a single place
# that knows how to turn a payload into a typed action.
ChatAction = Annotated[
    GenerateResumeAction
    | GenerateCoverLetterAction
    | CreateApplicationAction
    | PrepareApplicationAction
    | ApproveApplicationAction
    | SubmitApplicationAction
    | CancelApplicationAction
    | SetSearchRadiusAction
    | UpdateSearchKeywordsAction
    | NavigateAction
    | OpenInterviewPrepAction,
    Field(discriminator="kind"),
]

CHAT_ACTION_ADAPTER: TypeAdapter[ChatAction] = TypeAdapter(ChatAction)

# The kinds whose execution changes nothing on the server — a client-side navigation hint.
# The executor treats these as always-permitted and record-only, and the validators skip
# the ownership and state checks that a mutating action must pass. Kept as a set here, on
# the grammar, so "which actions are side-effect-free" is one fact rather than a
# repeated literal in the validator and the executor.
READ_ONLY_ACTION_KINDS: frozenset[ChatActionKind] = frozenset({
    ChatActionKind.NAVIGATE,
    ChatActionKind.OPEN_INTERVIEW_PREP,
})


# --- the persisted entities ------------------------------------------------------------


class Conversation(DomainModel):
    """One career-chat thread, owned by exactly one account.

    User-owned like every Phase 4+ entity: a conversation is read `WHERE user_id = ?`, so
    one account can neither list nor resume another's. `title` is a short human label the
    service derives from the opening message (never model-authored authority — just a
    caption). `last_message_at` orders the conversation list and is `None` for a thread
    with no turns yet; `is_archived` hides a thread without deleting its audit trail.
    """

    id: ConversationId
    user_id: UserId
    title: NonEmptyStr
    is_archived: bool = False
    created_at: UtcDatetime
    updated_at: UtcDatetime
    last_message_at: UtcDatetime | None = None


class ChatMessage(DomainModel):
    """One turn in a conversation — the prose, and the provenance of who produced it.

    `sequence` is the message's position in the thread, a monotonic counter the service
    assigns; the message id is derived from `(conversation_id, sequence)`, so re-finalizing
    a turn writes the same row rather than duplicating it. `content` is the prose only —
    the fenced proposal block is parsed out into `ChatActionProposal`s and never stored
    here, because the block is a control payload, not a message a user reads. An assistant
    message carries the `llm_run_id` and `provider_key` of the call that produced it, so a
    turn is traceable to its telemetry; a user message carries neither.
    """

    id: ChatMessageId
    conversation_id: ConversationId
    user_id: UserId
    role: ChatMessageRole
    content: NonEmptyStr
    sequence: Annotated[int, Field(ge=0)]
    llm_run_id: LLMRunId | None = None
    provider_key: str | None = None
    created_at: UtcDatetime


class ChatActionProposal(DomainModel):
    """One typed action the model proposed in a turn, awaiting a human's confirmation.

    The heart of "prose has zero authority": this is a *request* to act, parsed and
    validated out of the assistant's fenced block, that changes nothing until a human
    confirms it and the executor re-authorizes it. `action` is the validated `ChatAction`;
    `ordinal` is its position in the turn's block, from which the id is derived so a
    re-finalize is idempotent. `status` starts `PROPOSED` and only leaves that state
    through an explicit confirm or dismiss.
    """

    id: ChatActionProposalId
    conversation_id: ConversationId
    message_id: ChatMessageId
    user_id: UserId
    ordinal: Annotated[int, Field(ge=0)]
    action: ChatAction
    status: ChatActionProposalStatus = ChatActionProposalStatus.PROPOSED
    summary: NonEmptyStr
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @property
    def is_open(self) -> bool:
        """Whether this proposal may still be confirmed or dismissed."""
        return self.status is ChatActionProposalStatus.PROPOSED


class ChatActionExecution(DomainModel):
    """The record of one attempt to execute a confirmed proposal (the executor's audit).

    Written once per proposal — the id is derived from the proposal, so a double-confirm
    collapses onto one row rather than running the action twice. `outcome` says whether the
    action was refused at validation (`REJECTED`), permitted but failed (`FAILED`), or ran
    (`SUCCEEDED`); `result_ref` carries the id or handle the action produced (an
    application's new state, a document id, a navigation target) so the chat can show what
    happened without re-deriving it. `detail` is a secret-free, human-readable note.
    """

    id: ChatActionExecutionId
    proposal_id: ChatActionProposalId
    user_id: UserId
    outcome: ChatActionExecutionOutcome
    detail: str | None = None
    result_ref: str | None = None
    created_at: UtcDatetime
