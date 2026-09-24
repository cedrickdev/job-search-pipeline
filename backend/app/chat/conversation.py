"""The streaming chat turn: prose to the user, typed proposals to the audit, one run.

This is the service that runs one turn of the career chat, and it is where the pieces
the rest of the package built come together under the phase's one rule — **prose has
zero authority**. A turn does four things and nothing more: it persists the user's
message, it composes the model's input from the *versioned* system prompt plus a
*bounded, user-scoped* situation snapshot plus the recent history, it streams the
answer through the telemetry recorder, and — once the stream ends — it parses the
reply into prose (stored as the assistant message) and typed `ChatActionProposal`s
(stored, still merely *proposed*, for a human to later confirm through the executor).

Three properties hold here, each a deliberate boundary:

- **The model is given ids, never authority.** The snapshot (`ChatContextBuilder`) is
  the only place a real id enters the prompt, and it is assembled from user-scoped
  reads, so the model proposes actions against this account's entities or asks in prose.
  The proposals it emits are persisted `PROPOSED`; nothing here executes one.
- **Privacy is decided by wiring, not here.** The service takes its `RoutingPolicy`
  from construction, exactly as `LLMDocumentGenerator` does, so the decision of whether
  a turn may reach a remote provider lives in bootstrap, not in this code.
- **A turn is traceable and idempotent.** The assistant message carries the
  `llm_run_id`/`provider_key` the recorder wrote; message and proposal ids are derived
  from `(conversation, sequence)` and `(message, ordinal)`, so re-finalizing a turn
  writes the same rows rather than duplicating them.

The turn is exposed as a `ChatTurnStream` the caller async-iterates: it forwards token
events as they arrive (for an SSE response) and, on the terminal event, persists the
assistant turn and yields it. `send_message` drives that to completion for a caller
that just wants the finished turn (a test, a non-streaming client).
"""
import re
from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.app.chat.context import ChatContextBuilder
from backend.app.chat.intent import allowed_action_kinds, classify_turn_intent
from backend.app.chat.parsing import ParsedProposal, parse_turn
from backend.app.chat.prompts import career_chat_prompt_registry
from backend.app.chat.validators import action_within_scope
from backend.app.domain.chat import (
    READ_ONLY_ACTION_KINDS,
    ChatAction,
    ChatActionKind,
    ChatActionProposal,
    ChatMessage,
    ChatMessageRole,
    Conversation,
    ConversationScope,
)
from backend.app.domain.identifiers import (
    ConversationId,
    UserId,
    chat_action_proposal_id,
    chat_message_id,
    new_conversation_id,
)
from backend.app.llm.contracts import LLMMessage, LLMRequest, StreamEventType
from backend.app.llm.failures import LLMError
from backend.app.llm.prompts import PromptName, PromptRegistry
from backend.app.llm.recorder import LLMTelemetryRecorder, RecordedStream
from backend.app.llm.router import LLMRouter, RoutingPolicy
from backend.app.llm.telemetry import LLMRun, LLMRunStatus
from backend.app.repositories.contracts import (
    ChatActionProposalRepository,
    ChatMessageRepository,
    ConversationRepository,
)

# How many prior messages a turn replays to the model. A window, not the whole thread:
# the situation snapshot already carries the current state, so history is context for the
# conversation's *thread of reasoning*, and an unbounded replay is a cost that grows with
# every turn. Oldest-first within the window, so the model reads the exchange in order.
_MAX_HISTORY_MESSAGES: int = 20

# The heading that separates the per-turn snapshot from the user's actual words in the
# single user message a turn composes. Stated so the model can tell "here is your
# situation" from "here is what the user said" without either being an instruction.
_USER_TURN_HEADING: str = "=== USER MESSAGE ==="

# What an assistant message stores when the model emitted a proposal-only turn with no
# prose: `ChatMessage.content` is `NonEmptyStr`, so an empty reply falls back to the
# proposals' own captions, and to this fixed note only when there is nothing at all.
_EMPTY_ANSWER_PLACEHOLDER: str = "(no reply)"

# The longest a derived conversation title may be. A caption, not a field the user edits
# here, so the opening message is truncated to something a list can show.
_MAX_TITLE_LENGTH: int = 80
_DEFAULT_TITLE: str = "New conversation"


class ChatConversationError(Exception):
    """Base for the errors a turn raises before it reaches a provider."""


class ConversationNotFound(ChatConversationError):
    """The named conversation does not exist for this account (or belongs to another).

    Raised by `stream_turn` before anything is persisted, so a request naming another
    account's thread — or one that never existed — reads as absent rather than leaking
    that it exists. The API maps this to a 404.
    """

    def __init__(self, conversation_id: ConversationId) -> None:
        super().__init__(f"no conversation {conversation_id} for this account")
        self.conversation_id = conversation_id


class EmptyChatMessage(ChatConversationError):
    """The user's message was empty or whitespace — there is no turn to run."""


class ConversationScopeNotFound(ChatConversationError):
    """A new thread named a scope resource this account cannot open a thread about.

    Raised by `start_conversation` before the thread exists: an anchored scope
    (`OPPORTUNITY`/`APPLICATION`/`SEARCH_PROFILE`/`COMPANY`) whose id is not this account's
    — or does not exist at all — reads as absent rather than leaking that it exists, so a
    foreign or missing anchor is refused before a thread is ever anchored to it. The API
    maps this to a 404.
    """

    def __init__(self, scope: ConversationScope, scope_id: UUID | None) -> None:
        super().__init__(f"no {scope.value} {scope_id} to open a conversation about")
        self.scope = scope
        self.scope_id = scope_id


class ChatStreamEventType(StrEnum):
    """The three shapes a chat turn streams to its caller.

    A service-level event, deliberately distinct from the provider's `LLMStreamEvent`, so
    the API layer serializes *these* to SSE and never the raw LLM contract: a `TOKEN` is a
    chunk of prose to append, `COMPLETED` carries the persisted assistant message and its
    proposals, and `ERROR` carries a typed, secret-free code and note for a turn that
    reached a provider and did not succeed.
    """

    TOKEN = "TOKEN"  # noqa: S105 — a stream event kind, not a credential
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class ChatStreamEvent(BaseModel):
    """One event a `ChatTurnStream` yields — a token, the finished turn, or a failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: ChatStreamEventType
    text: str | None = None
    message: ChatMessage | None = None
    proposals: tuple[ChatActionProposal, ...] = ()
    error_code: str | None = None
    error_detail: str | None = None

    @classmethod
    def token(cls, text: str) -> "ChatStreamEvent":
        return cls(type=ChatStreamEventType.TOKEN, text=text)

    @classmethod
    def completed(cls, message: ChatMessage,
                  proposals: tuple[ChatActionProposal, ...]) -> "ChatStreamEvent":
        return cls(type=ChatStreamEventType.COMPLETED, message=message,
                   proposals=proposals)

    @classmethod
    def errored(cls, code: str, detail: str) -> "ChatStreamEvent":
        return cls(type=ChatStreamEventType.ERROR, error_code=code, error_detail=detail)


class ChatTurn(BaseModel):
    """The finished result of one turn, for a caller that drove the stream to the end.

    Carries the persisted user message always, and — when the model succeeded — the
    assistant message, its proposals and the telemetry run. On a turn that reached a
    provider and failed, `assistant_message` is `None` and the failure is on
    `error_code`/`error_detail`, because a failed turn writes no assistant message.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_message: ChatMessage
    assistant_message: ChatMessage | None = None
    proposals: tuple[ChatActionProposal, ...] = ()
    run: LLMRun | None = None
    error_code: str | None = None
    error_detail: str | None = None


class ChatConversationService:
    """Runs one streaming turn of the career chat, from user message to stored proposals.

    Holds the three chat repositories it writes, the `ChatContextBuilder` that assembles
    the per-turn snapshot, the router and telemetry recorder the turn streams through, and
    the `RoutingPolicy` wiring chose for the chat (the privacy decision lives there, never
    here). The prompt registry defaults to the chat's own one-template registry, so a test
    need not supply it. Nothing here executes a proposal — that is the executor's job,
    reached only after a human confirms; this service only ever *proposes*.
    """

    def __init__(self, *, conversations: ConversationRepository,
                 messages: ChatMessageRepository,
                 proposals: ChatActionProposalRepository,
                 context: ChatContextBuilder,
                 router: LLMRouter,
                 recorder: LLMTelemetryRecorder,
                 policy: RoutingPolicy,
                 prompts: PromptRegistry | None = None) -> None:
        self._conversations = conversations
        self._messages = messages
        self._proposals = proposals
        self._context = context
        self._router = router
        self._recorder = recorder
        self._policy = policy
        self._prompts = prompts or career_chat_prompt_registry()

    async def start_conversation(self, user_id: UserId, *, now: datetime,
                                 title: str | None = None,
                                 scope: ConversationScope = ConversationScope.GLOBAL,
                                 scope_id: UUID | None = None) -> Conversation:
        """Open a new, empty thread for this account, captioned from `title`.

        `title` is a caption the service truncates, never model-authored authority; an
        absent or empty one falls back to a fixed default. `scope`/`scope_id` bind the
        thread to a domain surface — an omitted scope is `GLOBAL` (the whole account), and
        an anchored one is validated to name a resource this account may talk about
        (`ConversationScopeNotFound` → 404) *before* the thread exists, so no thread is ever
        anchored to a foreign or missing resource. The thread has no turns yet, so
        `last_message_at` stays `None` until the first message is sent.
        """
        if not await self._context.scope_target_exists(user_id, scope, scope_id):
            raise ConversationScopeNotFound(scope, scope_id)
        conversation = Conversation(
            id=new_conversation_id(), user_id=user_id, title=_derive_title(title),
            scope=scope, scope_id=scope_id, created_at=now, updated_at=now)
        return await self._conversations.upsert(conversation)

    async def stream_turn(self, user_id: UserId, conversation_id: ConversationId,
                          user_text: str, *, now: datetime) -> "ChatTurnStream":
        """Persist the user's message and return the streamable, recordable turn.

        Ownership is checked first: a conversation that does not exist for this account
        raises `ConversationNotFound` before anything is written. The user's message is
        persisted at the next sequence and the thread's activity time is stamped, so even
        a turn whose model call later fails has recorded that the user spoke. The snapshot
        is built *for this thread's scope* (an anchored thread never sees another
        resource's ids), and the user's own words — never any posting or page text — are
        classified into the set of action kinds this turn may propose, which the returned
        `ChatTurnStream` uses to gate the model's proposals before any becomes a card.
        """
        text = user_text.strip()
        if not text:
            raise EmptyChatMessage("a chat message must not be empty")
        conversation = await self._conversations.get(user_id, conversation_id)
        if conversation is None:
            raise ConversationNotFound(conversation_id)

        recent = await self._messages.list_for_conversation(
            user_id, conversation_id, newest_first=True, limit=_MAX_HISTORY_MESSAGES)
        history = tuple(reversed(recent))
        latest = await self._messages.latest_sequence(user_id, conversation_id)
        user_sequence = 0 if latest is None else latest + 1

        user_message = ChatMessage(
            id=chat_message_id(conversation_id, user_sequence),
            conversation_id=conversation_id, user_id=user_id,
            role=ChatMessageRole.USER, content=text, sequence=user_sequence,
            created_at=now)
        await self._messages.upsert(user_message)
        await self._touch_conversation(conversation, now)

        snapshot = await self._context.build_for_conversation(user_id, conversation)
        request = self._compose_request(history=history, snapshot_text=snapshot.render(),
                                        user_text=text, conversation=conversation)
        recorded = self._recorder.stream(self._router, request, self._policy,
                                          user_id=user_id)
        allowed = allowed_action_kinds(classify_turn_intent(text))
        return ChatTurnStream(service=self, conversation=conversation,
                              user_message=user_message,
                              assistant_sequence=user_sequence + 1, recorded=recorded,
                              allowed_action_kinds=allowed, now=now)

    async def send_message(self, user_id: UserId, conversation_id: ConversationId,
                           user_text: str, *, now: datetime) -> ChatTurn:
        """Drive one turn to completion and return it — the non-streaming entry point."""
        stream = await self.stream_turn(user_id, conversation_id, user_text, now=now)
        error_code: str | None = None
        error_detail: str | None = None
        async for event in stream:
            if event.type is ChatStreamEventType.ERROR:
                error_code, error_detail = event.error_code, event.error_detail
        return ChatTurn(user_message=stream.user_message,
                        assistant_message=stream.assistant_message,
                        proposals=stream.proposals, run=stream.run,
                        error_code=error_code, error_detail=error_detail)

    async def list_conversations(
            self, user_id: UserId, *, include_archived: bool = False
    ) -> tuple[Conversation, ...]:
        """This account's threads, most recent activity first.

        Scoped by the owner in the repository, so it can never surface another account's
        thread; no ownership pre-check is needed because the list is itself the scope.
        """
        return await self._conversations.list_for_user(
            user_id, include_archived=include_archived)

    async def read_conversation(self, user_id: UserId,
                                conversation_id: ConversationId) -> Conversation:
        """One thread, or `ConversationNotFound` when it is not this account's.

        The single ownership gate the other reads reuse: a thread that does not exist for
        this account — because it never did or belongs to another — reads as absent rather
        than leaking that it exists (the API maps it to a 404).
        """
        conversation = await self._conversations.get(user_id, conversation_id)
        if conversation is None:
            raise ConversationNotFound(conversation_id)
        return conversation

    async def list_messages(self, user_id: UserId,
                            conversation_id: ConversationId) -> tuple[ChatMessage, ...]:
        """One thread's turns, oldest first — after the ownership gate.

        The message repository already scopes on the owner, but an unowned conversation
        returns an empty tuple there, indistinguishable from an empty owned thread — so the
        ownership check runs first, and a foreign or absent thread raises rather than
        answering an empty, existence-implying list.
        """
        await self.read_conversation(user_id, conversation_id)
        return await self._messages.list_for_conversation(user_id, conversation_id)

    async def list_proposals(
            self, user_id: UserId, conversation_id: ConversationId
    ) -> tuple[ChatActionProposal, ...]:
        """One thread's proposals, oldest first — after the same ownership gate."""
        await self.read_conversation(user_id, conversation_id)
        return await self._proposals.list_for_conversation(user_id, conversation_id)

    def _compose_request(self, *, history: tuple[ChatMessage, ...], snapshot_text: str,
                         user_text: str, conversation: Conversation) -> LLMRequest:
        """Build the turn's request: system prompt, replayed history, this turn's input.

        The system is the *versioned* `CAREER_CHAT_V1` instructions; the history is the
        recent messages as USER/ASSISTANT turns; the current turn is a single USER message
        carrying the per-turn scope metadata, the snapshot and the user's words, separated
        by headings. The scope block is authoritative, per-turn data — it names the resource
        the thread is bound to — so it is composed here, dynamically, never baked into the
        versioned template. There is no `structured_output` — the chat streams prose — and
        no session resume, so a turn is composed wholly from stored rows.
        """
        template = self._prompts.get(PromptName.CAREER_CHAT)
        messages: list[LLMMessage] = []
        for message in history:
            if message.role is ChatMessageRole.USER:
                messages.append(LLMMessage.user(message.content))
            else:
                messages.append(LLMMessage.assistant(message.content))
        messages.append(LLMMessage.user(
            f"{_render_scope(conversation)}\n\n{snapshot_text}\n\n"
            f"{_USER_TURN_HEADING}\n{user_text}"))
        return LLMRequest(
            messages=tuple(messages), system=template.instructions,
            purpose=template.purpose, prompt_name=template.name.value,
            prompt_version=template.version)

    async def _persist_assistant_turn(
            self, conversation: Conversation, recorded: RecordedStream,
            sequence: int, allowed_kinds: frozenset[ChatActionKind], now: datetime
    ) -> tuple[ChatMessage, tuple[ChatActionProposal, ...]]:
        """Parse the finished stream into the stored assistant message and its proposals.

        The fenced proposal block is parsed out of the accumulated text (`parse_turn`),
        never stored in the message content; the prose is stored, with a non-empty fallback
        for a proposal-only turn. Every parsed action must clear *two* independent walls
        before it becomes a `PROPOSED` card: the user's classified turn intent must permit
        its kind, and it must fall inside the conversation's scope. An action failing either
        is silently dropped here — no card, and its caption never reaches the stored content
        — because a confirmable proposal the user never asked for is exactly what this gate
        exists to prevent (read-only navigation is always admitted, it mutates nothing). The
        message carries the run's telemetry provenance and each admitted proposal is born
        `PROPOSED` with a derived id, so a re-finalize writes the same rows.
        """
        parsed = parse_turn(recorded.text)
        admitted = tuple(proposal for proposal in parsed.proposals
                         if _admit_proposal(conversation, allowed_kinds, proposal.action))
        run = recorded.run
        message = ChatMessage(
            id=chat_message_id(conversation.id, sequence),
            conversation_id=conversation.id, user_id=conversation.user_id,
            role=ChatMessageRole.ASSISTANT,
            content=parsed.prose or _proposal_fallback(admitted),
            sequence=sequence,
            llm_run_id=run.id if run is not None else None,
            provider_key=run.provider_key if run is not None else None,
            created_at=now)
        await self._messages.upsert(message)
        proposals = tuple(
            self._build_proposal(conversation, message, ordinal, parsed_proposal, now)
            for ordinal, parsed_proposal in enumerate(admitted))
        for proposal in proposals:
            await self._proposals.upsert(proposal)
        return message, proposals

    @staticmethod
    def _build_proposal(conversation: Conversation, message: ChatMessage, ordinal: int,
                        parsed: ParsedProposal, now: datetime) -> ChatActionProposal:
        return ChatActionProposal(
            id=chat_action_proposal_id(message.id, ordinal),
            conversation_id=conversation.id, message_id=message.id,
            user_id=conversation.user_id, ordinal=ordinal, action=parsed.action,
            summary=parsed.summary, created_at=now, updated_at=now)

    async def _touch_conversation(self, conversation: Conversation,
                                  now: datetime) -> None:
        """Stamp the thread's last activity, so the conversation list orders by it."""
        await self._conversations.upsert(conversation.model_copy(
            update={"last_message_at": now, "updated_at": now}))


class ChatTurnStream:
    """One turn's answer, streamable and self-finalizing — iterate it, then read the turn.

    `ChatConversationService.stream_turn` returns one of these after persisting the user's
    message. Async-iterating it forwards each token as a `ChatStreamEvent` and, on the same
    pass, when the stream ends: on success it persists the assistant message and its
    proposals and yields a terminal COMPLETED carrying them; on a turn that reached a
    provider and failed it yields a terminal ERROR and writes no assistant message; a
    refusal with no eligible provider (`NoProviderAvailable`) is caught and surfaced as an
    ERROR the same way, because the user's message was already persisted and the turn is
    over. After iterating, the caller reads `assistant_message`, `proposals` and `run` —
    the finished turn — off this object.
    """

    def __init__(self, *, service: ChatConversationService, conversation: Conversation,
                 user_message: ChatMessage, assistant_sequence: int,
                 recorded: RecordedStream,
                 allowed_action_kinds: frozenset[ChatActionKind],
                 now: datetime) -> None:
        self._service = service
        self._conversation = conversation
        self._assistant_sequence = assistant_sequence
        self._recorded = recorded
        self._allowed_action_kinds = allowed_action_kinds
        self._now = now
        self.user_message = user_message
        self.assistant_message: ChatMessage | None = None
        self.proposals: tuple[ChatActionProposal, ...] = ()
        self.run: LLMRun | None = None

    async def __aiter__(self) -> AsyncIterator[ChatStreamEvent]:
        """Forward tokens, then finalize the turn — the service's whole streaming footprint.

        Prose is forwarded through a `_StreamProseFilter`, so the fenced proposal block is
        never emitted as `TOKEN` events even though the raw text (which `parse_turn` needs)
        keeps accumulating in `recorded.text` untouched. A provider that raises rather than
        yielding a terminal ERROR — most notably the `NoProviderAvailable` an empty route
        raises on the first pull — is caught and turned into a terminal ERROR event, so a
        caller iterating the turn always sees a terminal event and never an exception
        mid-stream.
        """
        prose = _StreamProseFilter()
        try:
            async for event in self._recorded:
                if event.type is StreamEventType.TEXT_DELTA and event.text is not None:
                    visible = prose.feed(event.text)
                    if visible:
                        yield ChatStreamEvent.token(visible)
        except LLMError as error:
            yield ChatStreamEvent.errored(error.code.value, error.detail)
            return
        tail = prose.flush()
        if tail:
            yield ChatStreamEvent.token(tail)
        run = self._recorded.run
        self.run = run
        if run is not None and run.status is LLMRunStatus.SUCCEEDED:
            message, proposals = await self._service._persist_assistant_turn(
                self._conversation, self._recorded, self._assistant_sequence,
                self._allowed_action_kinds, self._now)
            self.assistant_message = message
            self.proposals = proposals
            yield ChatStreamEvent.completed(message, proposals)
            return
        code, detail = _run_failure(run)
        yield ChatStreamEvent.errored(code, detail)


def _run_failure(run: LLMRun | None) -> tuple[str, str]:
    """The typed code and secret-free note a non-succeeded run surfaces to the client.

    A FAILED/TIMEOUT run carries a `failure_code` and the layer's own redacted detail; a
    CANCELLED run carries no code (§57), so its status stands in, and a run that is somehow
    absent falls back to a generic FAILED — never a raw provider message.
    """
    if run is None:
        return LLMRunStatus.FAILED.value, "the turn did not reach a provider"
    code = run.failure_code.value if run.failure_code is not None else run.status.value
    return code, run.failure_detail or "the turn did not complete"


def _proposal_fallback(proposals: tuple[ParsedProposal, ...]) -> str:
    """Non-empty content for a proposal-only turn: the proposals' captions, or a note.

    `ChatMessage.content` is `NonEmptyStr`, so a turn whose prose was empty (the model
    emitted only a fenced block) falls back to the proposals' own human summaries, and to
    a fixed placeholder only when there was nothing at all to say.
    """
    return " ".join(proposal.summary for proposal in proposals) or _EMPTY_ANSWER_PLACEHOLDER


# The heading of the per-turn scope block — authoritative, dynamic data composed into the
# user message, never baked into the versioned prompt.
_SCOPE_HEADING: str = "=== CONVERSATION SCOPE ==="


def _admit_proposal(conversation: Conversation, allowed_kinds: frozenset[ChatActionKind],
                    action: ChatAction) -> bool:
    """Whether a parsed action may become a `PROPOSED` card on this turn.

    Two independent walls, both of which a mutating action must clear, plus one always-open
    door: read-only navigation (`NAVIGATE`, `OPEN_INTERVIEW_PREP`) mutates nothing on the
    server and is always admitted; every other action must be a kind the user's classified
    turn intent permits (`allowed_kinds`) *and* fall inside the conversation's scope. The
    executor re-checks scope and ownership at confirm, so this is the first of two
    enforcements — a proposal the user never asked for never even becomes a confirmable card.
    """
    if action.kind in READ_ONLY_ACTION_KINDS:
        return True
    return (action.kind in allowed_kinds
            and action_within_scope(conversation.scope, conversation.scope_id, action))


def _render_scope(conversation: Conversation) -> str:
    """The authoritative per-turn scope block naming the resource the thread is bound to.

    Dynamic, per-turn data — never part of the versioned prompt. It states plainly which
    resource (if any) the thread may act on; the scope-aware gate enforces the same rule
    whether or not the model honours the block, so this is guidance to the model, never the
    guarantee. It carries only the scope kind and the bracketed id — no secret, no detail.
    """
    scope = conversation.scope
    if scope is ConversationScope.GLOBAL:
        return (f"{_SCOPE_HEADING}\n"
                "This conversation is GLOBAL: you may reference any of this account's own "
                "resources by the bracketed ids in the situation below.")
    return (f"{_SCOPE_HEADING}\n"
            f"This conversation is bound to {scope.value} [{conversation.scope_id}]. "
            "Only propose actions on that one resource; anything else will be refused.")


def _derive_title(title: str | None) -> str:
    """A short, non-empty caption from an opening message, or the default."""
    trimmed = (title or "").strip()
    if not trimmed:
        return _DEFAULT_TITLE
    return trimmed if len(trimmed) <= _MAX_TITLE_LENGTH else trimmed[:_MAX_TITLE_LENGTH - 1] + "…"


# --- streaming proposal-fence filter ----------------------------------------

# The opening fence of a proposal block, as a *line*: three-or-more backticks, optional
# inline whitespace, the tag, optional inline whitespace, then the newline that ends the
# line. The newline is required, so an inline mention of the word is never mistaken for a
# fence. A loose form (no terminating newline) is used only at flush, to suppress a block
# that opened at the very end of the stream and never received a body. The close is any run
# of three backticks — the JSON body carries none, so the first after an open is the close.
_STREAM_OPEN: Final = re.compile(r"`{3,}[ \t]*proposal[ \t]*\r?\n", re.IGNORECASE)
_STREAM_OPEN_LOOSE: Final = re.compile(r"`{3,}[ \t]*proposal", re.IGNORECASE)
_STREAM_CLOSE: Final = re.compile(r"`{3,}")
# How far back to look for the start of a held-back opening-fence prefix. Generous: the
# longest real prefix is "```proposal" plus a little trailing whitespace and a lone "\r".
_MAX_OPEN_PREFIX: Final = 32
_PROPOSAL_TAG: Final = "proposal"


def _is_open_prefix(text: str) -> bool:
    """Whether `text` could be the start of an opening proposal fence, still unfinished.

    True for a growing backtick run, backticks followed by inline whitespace, and backticks
    plus a *prefix* of the tag (optionally with trailing whitespace and a lone `\\r`). It
    diverges to False as soon as the text can no longer become a fence — so a plain code
    fence (```python) or ordinary prose ending in a backtick is released, not held forever.
    """
    if not text or text[0] != "`":
        return False
    i, n = 0, len(text)
    while i < n and text[i] == "`":
        i += 1
    if i < 3:
        return i == n  # a backtick run that may still grow to the required three
    while i < n and text[i] in " \t":
        i += 1
    if i == n:
        return True
    j = 0
    while i < n and j < len(_PROPOSAL_TAG) and text[i] == _PROPOSAL_TAG[j]:
        i, j = i + 1, j + 1
    if i == n:
        return True  # consumed a prefix of the tag, still open
    if j < len(_PROPOSAL_TAG):
        return False  # diverged from the tag before finishing it
    while i < n and text[i] in " \t":
        i += 1
    return i == n or (text[i] == "\r" and i + 1 == n)


class _StreamProseState(StrEnum):
    """Where the fence filter is: streaming prose, or swallowing a proposal block."""

    PROSE = "PROSE"
    IN_PROPOSAL = "IN_PROPOSAL"


class _StreamProseFilter:
    """Strips proposal fences from streamed prose while the raw text is left untouched.

    The provider emits prose and, when it proposes actions, a fenced ```proposal block. The
    prose must stream to the client token by token; the fence and its body must not. This is
    a tiny two-state machine over the concatenated deltas: in PROSE it emits text up to an
    opening fence, holding back only a trailing suffix that might be the start of one (so a
    fence split across chunks is never half-emitted); in IN_PROPOSAL it emits nothing until
    the closing fence, then returns to PROSE (so multiple blocks and trailing prose are
    handled). An unterminated block emits nothing — fail-safe, no leak. The filter never
    touches `recorded.text`, which keeps accumulating the raw deltas, so `parse_turn` still
    sees the whole fenced block.
    """

    def __init__(self) -> None:
        self._state = _StreamProseState.PROSE
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        """Absorb one delta and return the prose that is now safe to emit (maybe empty)."""
        self._buffer += chunk
        out: list[str] = []
        while True:
            if self._state is _StreamProseState.PROSE:
                match = _STREAM_OPEN.search(self._buffer)
                if match is None:
                    emit, self._buffer = self._split_prose_tail(self._buffer)
                    out.append(emit)
                    break
                out.append(self._buffer[:match.start()].rstrip())
                self._buffer = self._buffer[match.end():]
                self._state = _StreamProseState.IN_PROPOSAL
            else:
                match = _STREAM_CLOSE.search(self._buffer)
                if match is None:
                    self._buffer = _closing_tail(self._buffer)
                    break
                self._buffer = self._buffer[match.end():]
                self._state = _StreamProseState.PROSE
        return "".join(out)

    def flush(self) -> str:
        """The prose remaining once the stream ends; nothing if a block never closed."""
        if self._state is _StreamProseState.IN_PROPOSAL:
            self._buffer = ""
            return ""
        tail, self._buffer = self._buffer, ""
        loose = _STREAM_OPEN_LOOSE.search(tail)
        return tail[:loose.start()] if loose is not None else tail

    @staticmethod
    def _split_prose_tail(buffer: str) -> tuple[str, str]:
        """Split PROSE `buffer` into (emit-now, hold-back), holding a possible-open suffix.

        Holds back the longest trailing run that could be the start of an opening fence, so
        a fence split across chunks completes on the next feed rather than leaking its start.
        """
        for start in range(max(0, len(buffer) - _MAX_OPEN_PREFIX), len(buffer)):
            if _is_open_prefix(buffer[start:]):
                return buffer[:start], buffer[start:]
        return buffer, ""


def _closing_tail(buffer: str) -> str:
    """Inside a block: keep only a trailing partial backtick run, drop the suppressed body.

    A closing fence split across chunks would otherwise be missed, so the trailing one or
    two backticks are retained to be completed by the next delta; everything else is proposal
    body the client must never see, and is discarded.
    """
    held = 0
    while held < 2 and held < len(buffer) and buffer[-1 - held] == "`":
        held += 1
    return buffer[len(buffer) - held:] if held else ""



