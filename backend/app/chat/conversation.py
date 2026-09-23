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
from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from backend.app.chat.context import ChatContextBuilder
from backend.app.chat.parsing import ParsedProposal, parse_turn
from backend.app.chat.prompts import career_chat_prompt_registry
from backend.app.domain.chat import (
    ChatActionProposal,
    ChatMessage,
    ChatMessageRole,
    Conversation,
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
                                 title: str | None = None) -> Conversation:
        """Open a new, empty thread for this account, captioned from `title`.

        `title` is a caption the service truncates, never model-authored authority; an
        absent or empty one falls back to a fixed default. The thread has no turns yet, so
        `last_message_at` stays `None` until the first message is sent.
        """
        conversation = Conversation(
            id=new_conversation_id(), user_id=user_id, title=_derive_title(title),
            created_at=now, updated_at=now)
        return await self._conversations.upsert(conversation)

    async def stream_turn(self, user_id: UserId, conversation_id: ConversationId,
                          user_text: str, *, now: datetime) -> "ChatTurnStream":
        """Persist the user's message and return the streamable, recordable turn.

        Ownership is checked first: a conversation that does not exist for this account
        raises `ConversationNotFound` before anything is written. The user's message is
        persisted at the next sequence and the thread's activity time is stamped, so even
        a turn whose model call later fails has recorded that the user spoke. The returned
        `ChatTurnStream` is what actually streams the answer and finalizes the assistant
        turn when the caller iterates it.
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

        snapshot = await self._context.build(user_id)
        request = self._compose_request(history=history, snapshot_text=snapshot.render(),
                                        user_text=text)
        recorded = self._recorder.stream(self._router, request, self._policy,
                                          user_id=user_id)
        return ChatTurnStream(service=self, conversation=conversation,
                              user_message=user_message,
                              assistant_sequence=user_sequence + 1, recorded=recorded,
                              now=now)

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
                         user_text: str) -> LLMRequest:
        """Build the turn's request: system prompt, replayed history, this turn's input.

        The system is the *versioned* `CAREER_CHAT_V1` instructions; the history is the
        recent messages as USER/ASSISTANT turns; the current turn is a single USER message
        carrying the per-turn snapshot and the user's words, separated by a heading. There
        is no `structured_output` — the chat streams prose — and no session resume, so a
        turn is composed wholly from stored rows rather than a provider-held handle.
        """
        template = self._prompts.get(PromptName.CAREER_CHAT)
        messages: list[LLMMessage] = []
        for message in history:
            if message.role is ChatMessageRole.USER:
                messages.append(LLMMessage.user(message.content))
            else:
                messages.append(LLMMessage.assistant(message.content))
        messages.append(LLMMessage.user(
            f"{snapshot_text}\n\n{_USER_TURN_HEADING}\n{user_text}"))
        return LLMRequest(
            messages=tuple(messages), system=template.instructions,
            purpose=template.purpose, prompt_name=template.name.value,
            prompt_version=template.version)

    async def _persist_assistant_turn(
            self, conversation: Conversation, recorded: RecordedStream,
            sequence: int, now: datetime
    ) -> tuple[ChatMessage, tuple[ChatActionProposal, ...]]:
        """Parse the finished stream into the stored assistant message and its proposals.

        The fenced proposal block is parsed out of the accumulated text (`parse_turn`),
        never stored in the message content; the prose is stored, with a non-empty
        fallback for a proposal-only turn. The message carries the run's telemetry
        provenance, and each proposal is born `PROPOSED` with a derived id, so a
        re-finalize writes the same rows.
        """
        parsed = parse_turn(recorded.text)
        run = recorded.run
        message = ChatMessage(
            id=chat_message_id(conversation.id, sequence),
            conversation_id=conversation.id, user_id=conversation.user_id,
            role=ChatMessageRole.ASSISTANT,
            content=parsed.prose or _proposal_fallback(parsed.proposals),
            sequence=sequence,
            llm_run_id=run.id if run is not None else None,
            provider_key=run.provider_key if run is not None else None,
            created_at=now)
        await self._messages.upsert(message)
        proposals = tuple(
            self._build_proposal(conversation, message, ordinal, parsed_proposal, now)
            for ordinal, parsed_proposal in enumerate(parsed.proposals))
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
                 recorded: RecordedStream, now: datetime) -> None:
        self._service = service
        self._conversation = conversation
        self._assistant_sequence = assistant_sequence
        self._recorded = recorded
        self._now = now
        self.user_message = user_message
        self.assistant_message: ChatMessage | None = None
        self.proposals: tuple[ChatActionProposal, ...] = ()
        self.run: LLMRun | None = None

    async def __aiter__(self) -> AsyncIterator[ChatStreamEvent]:
        """Forward tokens, then finalize the turn — the service's whole streaming footprint.

        A provider that raises rather than yielding a terminal ERROR — most notably the
        `NoProviderAvailable` an empty route raises on the first pull — is caught and turned
        into a terminal ERROR event, so a caller iterating the turn always sees a terminal
        event and never an exception mid-stream.
        """
        try:
            async for event in self._recorded:
                if event.type is StreamEventType.TEXT_DELTA and event.text is not None:
                    yield ChatStreamEvent.token(event.text)
        except LLMError as error:
            yield ChatStreamEvent.errored(error.code.value, error.detail)
            return
        run = self._recorded.run
        self.run = run
        if run is not None and run.status is LLMRunStatus.SUCCEEDED:
            message, proposals = await self._service._persist_assistant_turn(
                self._conversation, self._recorded, self._assistant_sequence, self._now)
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


def _derive_title(title: str | None) -> str:
    """A short, non-empty caption from an opening message, or the default."""
    trimmed = (title or "").strip()
    if not trimmed:
        return _DEFAULT_TITLE
    return trimmed if len(trimmed) <= _MAX_TITLE_LENGTH else trimmed[:_MAX_TITLE_LENGTH - 1] + "…"



