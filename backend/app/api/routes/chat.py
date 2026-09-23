"""`/api/v2/chat`: the career chat as a control plane, over HTTP.

The phase's one rule shapes this surface. A turn's prose *streams* — the answer arrives
as Server-Sent Events the client renders live — but a turn *changes nothing*: the model's
fenced block is parsed into `PROPOSED` proposals that sit inert until a human calls
`confirm`, and only then does the executor re-authorize and run the action onto the same
services the other routes call. `proposal != permission`, so `confirm` re-runs every gate
(ownership, domain state, the Phase 12 execution gate for a submit); the chat gets no
override and no shortcut.

The owner is never in the path or the body — it is the account resolved from the session,
so no request can read, resume, confirm or dismiss on another user's conversation or
proposal (docs/ENGINEERING_STANDARDS.md §Security). Reads answer 404 for "no such thread"
and "not yours" alike, so an id cannot be probed.

The one streaming endpoint is `POST /chat/conversations/{id}/messages`: it persists the
user's message and returns a `text/event-stream` of `ChatStreamEventResponse`s — one
`TOKEN` per chunk of prose, then a terminal `COMPLETED` carrying the stored assistant
message and its proposals, or a terminal `ERROR` for a turn that reached a provider and
did not succeed. Ownership and empty-message refusals are raised *before* the stream is
returned, so they surface as ordinary 404/422 JSON rather than an event mid-stream.
"""
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, status
from fastapi.responses import StreamingResponse

from backend.app.api.dependencies import (
    ChatActions,
    ChatConversations,
    CurrentSession,
    Now,
)
from backend.app.api.schemas import (
    ChatActionExecutionResponse,
    ChatActionProposalListResponse,
    ChatActionProposalResponse,
    ChatMessageListResponse,
    ChatStreamEventResponse,
    ConversationListResponse,
    ConversationResponse,
    SendMessageRequest,
    StartConversationRequest,
)
from backend.app.domain.identifiers import ChatActionProposalId, ConversationId

router = APIRouter(tags=["v2-chat"])

# The media type an SSE stream is served with, and the OpenAPI description of the one
# streaming endpoint's body. The 200 is documented against `ChatStreamEventResponse` so the
# generated client has the event type, even though the bytes arrive as `text/event-stream`
# `data:` lines rather than a single JSON body.
_SSE_MEDIA_TYPE = "text/event-stream"
_STREAM_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_200_OK: {
        "model": ChatStreamEventResponse,
        "description": "A Server-Sent Events stream: one ChatStreamEventResponse per "
                       "`data:` line — TOKEN chunks, then a terminal COMPLETED or ERROR."}}


# --- conversations ----------------------------------------------------------

@router.post("/chat/conversations", response_model=ConversationResponse,
             status_code=status.HTTP_201_CREATED)
async def start_conversation(body: StartConversationRequest, current: CurrentSession,
                             service: ChatConversations,
                             instant: Now) -> ConversationResponse:
    """Open a new, empty chat thread for this account (§Security).

    201, because it creates a resource. `title` is a caption the service truncates, never
    authority; an absent or blank one falls back to a default. The thread has no turns yet.
    """
    conversation = await service.start_conversation(
        current.user.id, now=instant, title=body.title)
    return ConversationResponse.of(conversation)


@router.get("/chat/conversations", response_model=ConversationListResponse)
async def list_conversations(current: CurrentSession,
                             service: ChatConversations) -> ConversationListResponse:
    """This account's chat threads, most recent activity first."""
    conversations = await service.list_conversations(current.user.id)
    return ConversationListResponse.of(conversations)


@router.get("/chat/conversations/{conversation_id}",
            response_model=ConversationResponse)
async def read_conversation(conversation_id: ConversationId, current: CurrentSession,
                            service: ChatConversations) -> ConversationResponse:
    """One thread's caption and activity, or 404 if it is not this account's."""
    conversation = await service.read_conversation(current.user.id, conversation_id)
    return ConversationResponse.of(conversation)


@router.get("/chat/conversations/{conversation_id}/messages",
            response_model=ChatMessageListResponse)
async def list_messages(conversation_id: ConversationId, current: CurrentSession,
                        service: ChatConversations) -> ChatMessageListResponse:
    """One thread's turns, oldest first. 404 when the thread is not this account's."""
    messages = await service.list_messages(current.user.id, conversation_id)
    return ChatMessageListResponse.of(messages)


@router.get("/chat/conversations/{conversation_id}/proposals",
            response_model=ChatActionProposalListResponse)
async def list_proposals(conversation_id: ConversationId, current: CurrentSession,
                         service: ChatConversations) -> ChatActionProposalListResponse:
    """One thread's proposals, oldest first. 404 when the thread is not this account's.

    The current status of each proposal is on the response, so a UI knows which cards are
    still open to confirm or dismiss and which are already terminal.
    """
    proposals = await service.list_proposals(current.user.id, conversation_id)
    return ChatActionProposalListResponse.of(proposals)


# --- one streaming turn -----------------------------------------------------

@router.post("/chat/conversations/{conversation_id}/messages",
             response_model=None, responses=_STREAM_RESPONSES)
async def send_message(conversation_id: ConversationId, body: SendMessageRequest,
                       current: CurrentSession, service: ChatConversations,
                       instant: Now) -> StreamingResponse:
    """Send one user turn and stream the assistant's reply as Server-Sent Events.

    Ownership and empty-message checks run *before* the stream is returned, so a thread
    that is not this account's is a 404 and an empty message a 422 — an ordinary JSON error,
    not an event mid-stream. Once past them the user's message is persisted and the reply
    streams: `TOKEN` events as prose arrives, then a terminal `COMPLETED` carrying the
    stored assistant message and its `PROPOSED` proposals, or `ERROR` for a turn that
    reached a provider and failed. Nothing here executes a proposal — that is `confirm`.
    """
    stream = await service.stream_turn(
        current.user.id, conversation_id, body.text, now=instant)

    async def events() -> AsyncIterator[str]:
        async for event in stream:
            payload = ChatStreamEventResponse.of(event).model_dump_json()
            yield f"data: {payload}\n\n"

    return StreamingResponse(events(), media_type=_SSE_MEDIA_TYPE)


# --- confirming or dismissing a proposal ------------------------------------

@router.post("/chat/proposals/{proposal_id}/confirm",
             response_model=ChatActionExecutionResponse)
async def confirm_proposal(proposal_id: ChatActionProposalId, current: CurrentSession,
                           service: ChatActions,
                           instant: Now) -> ChatActionExecutionResponse:
    """Execute one confirmed proposal — the last gate, and the only place it runs.

    The proposal is re-authorized (ownership, coarse domain state) and then handed to the
    same service the HTTP routes call, which runs its own authoritative checks — a submit
    re-runs the whole Phase 12 gate. Idempotent by the proposal's id: a double-confirm
    returns the recorded execution rather than running twice. The response is the audited
    outcome (`SUCCEEDED`, `REJECTED` or `FAILED`); a 404 when the proposal is not this
    account's, a 409 when it is no longer open.
    """
    execution = await service.execute(current.user.id, proposal_id, now=instant)
    return ChatActionExecutionResponse.of(execution)


@router.post("/chat/proposals/{proposal_id}/dismiss",
             response_model=ChatActionProposalResponse)
async def dismiss_proposal(proposal_id: ChatActionProposalId, current: CurrentSession,
                           service: ChatActions,
                           instant: Now) -> ChatActionProposalResponse:
    """Decline an open proposal without running it — `PROPOSED` → `DISMISSED`.

    Writes no execution because nothing was attempted; it only moves the proposal out of
    the open set so it cannot later be confirmed. A 404 when the proposal is not this
    account's, a 409 when it is no longer open (already executed, rejected, failed or
    dismissed). Returns the proposal in its new status.
    """
    proposal = await service.dismiss(current.user.id, proposal_id, now=instant)
    return ChatActionProposalResponse.of(proposal)
