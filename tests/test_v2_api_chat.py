# tests/test_v2_api_chat.py
"""`/api/v2/chat` over HTTP: the control plane as a browser meets it.

The service tests (`test_v2_chat_conversation.py`, `test_v2_chat_executor.py`) cover the
decisions; this covers the route layer and its wiring — what actually crosses the wire.
The phase's rule shapes every case here: a turn *streams* prose as Server-Sent Events but
*changes nothing*, the model's fenced block arrives as `PROPOSED` proposals that sit inert
until a human calls `confirm`, and `confirm` re-runs every gate. The owner is never in the
path or the body — it is the session's account — so a thread or a proposal that is not
this account's reads as a 404, never as another user's data.

The LLM is a `FakeProvider` the harness runs the streaming turn through: a test hands it a
canned reply (prose, or prose plus a fenced proposal block) and asserts on what the route
streams and stores, so the behaviour under test is the API's, not a provider's. The SSE
body is buffered by `httpx`'s ASGI transport, so `response.text` holds the whole
`data: {json}\n\n` stream and `_events` parses it back into the events the client sees.
"""
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from backend.app.chat.conversation import ChatStreamEventType
from backend.app.chat.prompts import PROPOSAL_FENCE_TAG
from backend.app.domain.chat import (
    ChatActionExecutionOutcome,
    ChatActionProposalStatus,
)
from backend.app.llm.capabilities import BASELINE_CAPABILITY, Capability
from tests.v2_api import PLACEHOLDER_ID, api_harness
from tests.v2_llm import FakeProvider

pytestmark = pytest.mark.asyncio


def _navigate(target: str = "OPPORTUNITIES") -> dict[str, Any]:
    """A read-only NAVIGATE action — always permitted, executes without a service call."""
    return {"kind": "NAVIGATE", "target": target}


def _set_radius(search_profile_id: str, radius_km: float = 42.0) -> dict[str, Any]:
    """A mutating SET_SEARCH_RADIUS action, for the re-authorization path."""
    return {"kind": "SET_SEARCH_RADIUS", "search_profile_id": search_profile_id,
            "radius_km": radius_km}


def _fenced_reply(prose: str, *actions: dict[str, Any]) -> str:
    """A realistic assistant reply: prose plus one fenced proposal block."""
    proposals = [{"summary": f"do thing {i}", "action": action}
                 for i, action in enumerate(actions)]
    body = json.dumps({"proposals": proposals})
    return f"{prose}\n\n```{PROPOSAL_FENCE_TAG}\n{body}\n```"


def _provider(text: str) -> FakeProvider:
    """A stub provider that can serve the chat request and returns `text` as the reply."""
    return FakeProvider(
        provider_key="conn_gateway",
        capabilities=frozenset({BASELINE_CAPABILITY, Capability.SYSTEM_INSTRUCTIONS}),
        text=text)


def _events(response: httpx.Response) -> list[dict[str, Any]]:
    """The parsed SSE events from a buffered `text/event-stream` body."""
    return [json.loads(chunk.removeprefix("data: "))
            for chunk in response.text.split("\n\n") if chunk.strip()]


async def _start(api: Any, *, title: str | None = "Ma recherche") -> str:
    """Open a thread over HTTP and return its id."""
    response = await api.write("POST", "/chat/conversations", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _turn(api: Any, conversation_id: str, text: str) -> list[dict[str, Any]]:
    """Send one user turn and return the SSE events the route streamed."""
    response = await api.write(
        "POST", f"/chat/conversations/{conversation_id}/messages", json={"text": text})
    assert response.status_code == 200, response.text
    return _events(response)


def _proposal_from(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The single proposal carried by the terminal COMPLETED event."""
    completed = next(e for e in events if e["type"] == ChatStreamEventType.COMPLETED)
    (proposal,) = completed["proposals"]
    return proposal


# --- opening and listing threads --------------------------------------------

async def test_starting_a_conversation_returns_201_and_an_empty_thread(tmp_path):
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        response = await api.write("POST", "/chat/conversations",
                                   json={"title": "Postuler chez Fixture SA"})
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["title"] == "Postuler chez Fixture SA"
        assert body["is_archived"] is False
        assert body["last_message_at"] is None


async def test_a_blank_title_falls_back_to_a_default(tmp_path):
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        response = await api.write("POST", "/chat/conversations", json={"title": "   "})
        assert response.status_code == 201, response.text
        assert response.json()["title"] == "New conversation"


async def test_listing_conversations_returns_only_this_accounts_threads(tmp_path):
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await _start(api, title="Première")
        await _start(api, title="Deuxième")
        response = await api.read("/chat/conversations")
        assert response.status_code == 200
        titles = {c["title"] for c in response.json()["conversations"]}
        assert titles == {"Première", "Deuxième"}


async def test_reading_a_thread_that_is_not_yours_is_404(tmp_path):
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        conversation_id = await _start(api)
        # A second account replaces the session; the first account's thread is now
        # another user's and must read as absent, not as a 403 that confirms it exists.
        await api.register(email="someone.else@example.com")
        response = await api.read(f"/chat/conversations/{conversation_id}")
        assert response.status_code == 404
        assert response.json()["error"] == "conversation_not_found"


# --- one streaming turn -----------------------------------------------------

async def test_a_turn_streams_tokens_then_a_completed_event(tmp_path):
    reply = _fenced_reply("Voici ma suggestion.", _navigate("APPLICATIONS"))
    async with api_harness(tmp_path, chat_provider=_provider(reply)) as api:
        await api.sign_in()
        conversation_id = await _start(api)
        events = await _turn(api, conversation_id, "Où en suis-je ?")

        assert events[-1]["type"] == ChatStreamEventType.COMPLETED
        tokens = [e["text"] for e in events if e["type"] == ChatStreamEventType.TOKEN]
        assert "".join(t for t in tokens if t) == reply  # raw prose+block to the client
        message = events[-1]["message"]
        assert message["role"] == "ASSISTANT"
        assert message["content"] == "Voici ma suggestion."  # stored prose is clean
        assert PROPOSAL_FENCE_TAG not in message["content"]
        (proposal,) = events[-1]["proposals"]
        assert proposal["status"] == ChatActionProposalStatus.PROPOSED
        assert proposal["action"]["kind"] == "NAVIGATE"


async def test_an_empty_message_is_422_before_the_stream(tmp_path):
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        conversation_id = await _start(api)
        response = await api.write(
            "POST", f"/chat/conversations/{conversation_id}/messages",
            json={"text": "   "})
        assert response.status_code == 422
        assert response.json()["error"] == "empty_chat_message"


async def test_a_turn_on_a_thread_that_is_not_yours_is_404(tmp_path):
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        conversation_id = await _start(api)
        await api.register(email="someone.else@example.com")
        response = await api.write(
            "POST", f"/chat/conversations/{conversation_id}/messages",
            json={"text": "Bonjour"})
        assert response.status_code == 404
        assert response.json()["error"] == "conversation_not_found"


async def test_a_turn_persists_messages_and_proposals_readable_via_get(tmp_path):
    reply = _fenced_reply("Je peux vous aider.", _navigate())
    async with api_harness(tmp_path, chat_provider=_provider(reply)) as api:
        await api.sign_in()
        conversation_id = await _start(api)
        await _turn(api, conversation_id, "Aide-moi")

        messages = await api.read(f"/chat/conversations/{conversation_id}/messages")
        assert messages.status_code == 200
        roles = [m["role"] for m in messages.json()["messages"]]
        assert roles == ["USER", "ASSISTANT"]

        proposals = await api.read(f"/chat/conversations/{conversation_id}/proposals")
        assert proposals.status_code == 200
        (proposal,) = proposals.json()["proposals"]
        assert proposal["status"] == ChatActionProposalStatus.PROPOSED
        assert proposal["action"]["kind"] == "NAVIGATE"


# --- confirming a proposal: the last gate --------------------------------------

async def test_confirming_a_navigate_proposal_executes_it(tmp_path):
    reply = _fenced_reply("Allons voir vos candidatures.", _navigate("APPLICATIONS"))
    async with api_harness(tmp_path, chat_provider=_provider(reply)) as api:
        await api.sign_in()
        conversation_id = await _start(api)
        proposal = _proposal_from(await _turn(api, conversation_id, "Montre-moi"))

        response = await api.write("POST", f"/chat/proposals/{proposal['id']}/confirm")
        assert response.status_code == 200, response.text
        execution = response.json()
        assert execution["outcome"] == ChatActionExecutionOutcome.SUCCEEDED
        assert execution["result_ref"] == "APPLICATIONS"  # the navigation target

        # The card is now terminal: the read route reflects the new status.
        proposals = await api.read(f"/chat/conversations/{conversation_id}/proposals")
        (stored,) = proposals.json()["proposals"]
        assert stored["status"] == ChatActionProposalStatus.EXECUTED


async def test_confirm_re_authorizes_a_mutation_so_a_proposal_is_not_permission(tmp_path):
    # The model proposes widening a search that this account does not own. `confirm`
    # re-runs the gate; the validator rejects it and nothing mutates — proposal != permission.
    reply = _fenced_reply("Élargissons votre zone.", _set_radius(str(uuid4())))
    async with api_harness(tmp_path, chat_provider=_provider(reply)) as api:
        await api.sign_in()
        conversation_id = await _start(api)
        proposal = _proposal_from(await _turn(api, conversation_id, "Plus loin"))

        response = await api.write("POST", f"/chat/proposals/{proposal['id']}/confirm")
        assert response.status_code == 200, response.text  # a recorded, audited refusal
        assert response.json()["outcome"] == ChatActionExecutionOutcome.REJECTED

        proposals = await api.read(f"/chat/conversations/{conversation_id}/proposals")
        (stored,) = proposals.json()["proposals"]
        assert stored["status"] == ChatActionProposalStatus.REJECTED


async def test_confirming_a_proposal_that_is_not_yours_is_404(tmp_path):
    reply = _fenced_reply("Voici.", _navigate())
    async with api_harness(tmp_path, chat_provider=_provider(reply)) as api:
        await api.sign_in()
        conversation_id = await _start(api)
        proposal = _proposal_from(await _turn(api, conversation_id, "Aide"))

        await api.register(email="someone.else@example.com")
        response = await api.write("POST", f"/chat/proposals/{proposal['id']}/confirm")
        assert response.status_code == 404
        assert response.json()["error"] == "chat_proposal_not_found"


async def test_confirm_is_idempotent_by_proposal_id(tmp_path):
    reply = _fenced_reply("Voici.", _navigate())
    async with api_harness(tmp_path, chat_provider=_provider(reply)) as api:
        await api.sign_in()
        conversation_id = await _start(api)
        proposal = _proposal_from(await _turn(api, conversation_id, "Aide"))

        first = await api.write("POST", f"/chat/proposals/{proposal['id']}/confirm")
        second = await api.write("POST", f"/chat/proposals/{proposal['id']}/confirm")
        assert first.status_code == second.status_code == 200
        # The second confirm returns the recorded execution, it does not run again.
        assert first.json()["id"] == second.json()["id"]


# --- dismissing a proposal --------------------------------------------------

async def test_dismiss_moves_a_proposal_out_of_the_open_set(tmp_path):
    reply = _fenced_reply("Voici.", _navigate())
    async with api_harness(tmp_path, chat_provider=_provider(reply)) as api:
        await api.sign_in()
        conversation_id = await _start(api)
        proposal = _proposal_from(await _turn(api, conversation_id, "Aide"))

        response = await api.write("POST", f"/chat/proposals/{proposal['id']}/dismiss")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == ChatActionProposalStatus.DISMISSED


async def test_confirming_a_dismissed_proposal_is_409(tmp_path):
    reply = _fenced_reply("Voici.", _navigate())
    async with api_harness(tmp_path, chat_provider=_provider(reply)) as api:
        await api.sign_in()
        conversation_id = await _start(api)
        proposal = _proposal_from(await _turn(api, conversation_id, "Aide"))

        await api.write("POST", f"/chat/proposals/{proposal['id']}/dismiss")
        response = await api.write("POST", f"/chat/proposals/{proposal['id']}/confirm")
        assert response.status_code == 409
        assert response.json()["error"] == "chat_proposal_not_actionable"

