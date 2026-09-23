# tests/test_v2_chat_conversation.py
"""One streaming turn end to end: prose to the user, typed proposals to the audit.

The conversation service is where the phase's rule stops being a claim and becomes
mechanism — it streams the model's answer, stores the *prose* as the assistant message,
and stores the fenced block as `PROPOSED` proposals that change nothing until a human
confirms them through the executor. These tests pin what one turn does and does not do:
it numbers and persists the user's message, composes the request from the versioned
prompt + the bounded snapshot + the recent history, records the run and stamps its
provenance onto the assistant message, parses proposals out of the prose (never into the
stored content), and refuses a turn on a conversation this account does not own. The LLM
is a `FakeProvider` returning a canned reply, so the behaviour under test is the service's,
not a provider's.
"""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from backend.app.chat.context import ChatContextBuilder
from backend.app.chat.conversation import (
    ChatConversationService,
    ChatStreamEventType,
    ConversationNotFound,
    EmptyChatMessage,
)
from backend.app.chat.prompts import CAREER_CHAT_V1, PROPOSAL_FENCE_TAG
from backend.app.domain.chat import (
    ChatActionProposalStatus,
    ChatMessageRole,
    NavigateAction,
    NavigationTarget,
)
from backend.app.domain.identifiers import chat_action_proposal_id, chat_message_id
from backend.app.llm.capabilities import BASELINE_CAPABILITY, Capability
from backend.app.llm.contracts import MessageRole
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.recorder import LLMTelemetryRecorder
from backend.app.llm.registry import LLMProviderRegistry
from backend.app.llm.router import LLMRouter, PrivacyClass, RoutingPolicy
from backend.app.llm.telemetry import LLMRunStatus
from tests.v2_builders import CONVERSATION, NOW, OTHER_USER, USER, a_conversation
from tests.v2_fakes import (
    FakeApplicationRepository,
    FakeCandidateProfileRepository,
    FakeChatActionProposalRepository,
    FakeChatMessageRepository,
    FakeConversationRepository,
    FakeLLMRunRepository,
    FakeOpportunityRepository,
    FakeSearchProfileRepository,
)
from tests.v2_llm import FakeProvider

pytestmark = pytest.mark.asyncio

_EXTERNAL = RoutingPolicy(privacy=PrivacyClass.EXTERNAL_ALLOWED)


class _Mono:
    """A hand-advanced monotonic clock, so a recorded run's latency is deterministic."""

    def __init__(self) -> None:
        self._now = 1000.0

    def __call__(self) -> float:
        value = self._now
        self._now += 0.25
        return value


def _clock() -> datetime:
    return datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


def _navigate(target: str = "OPPORTUNITIES") -> dict:
    return {"kind": "NAVIGATE", "target": target}


def _provider(**kwargs) -> FakeProvider:
    """A `FakeProvider` that can serve the chat request — its system prompt requires the
    SYSTEM_INSTRUCTIONS capability, which the baseline stub does not otherwise claim."""
    kwargs.setdefault("provider_key", "conn_gateway")
    kwargs.setdefault(
        "capabilities", frozenset({BASELINE_CAPABILITY, Capability.SYSTEM_INSTRUCTIONS}))
    return FakeProvider(**kwargs)


def _fenced_reply(prose: str, *actions: dict) -> str:
    """A realistic assistant reply: prose plus one fenced proposal block."""
    proposals = [{"summary": f"do thing {i}", "action": action}
                 for i, action in enumerate(actions)]
    body = json.dumps({"proposals": proposals})
    return f"{prose}\n\n```{PROPOSAL_FENCE_TAG}\n{body}\n```"


def _harness(*, provider: FakeProvider | None = None,
             policy: RoutingPolicy = _EXTERNAL) -> SimpleNamespace:
    """A wired service over fakes, with its repositories exposed for assertions."""
    provider = provider or _provider(text="Bonjour.")
    registry = LLMProviderRegistry()
    registry.register_all([provider])
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs, clock=_clock, monotonic=_Mono())
    conversations = FakeConversationRepository()
    messages = FakeChatMessageRepository()
    proposals = FakeChatActionProposalRepository()
    context = ChatContextBuilder(
        profiles=FakeCandidateProfileRepository(),
        searches=FakeSearchProfileRepository(),
        applications=FakeApplicationRepository(),
        opportunities=FakeOpportunityRepository())
    service = ChatConversationService(
        conversations=conversations, messages=messages, proposals=proposals,
        context=context, router=LLMRouter(registry), recorder=recorder, policy=policy)
    return SimpleNamespace(
        service=service, conversations=conversations, messages=messages,
        proposals=proposals, provider=provider, runs=runs)


async def _seed_conversation(harness: SimpleNamespace) -> None:
    """A fresh, empty thread owned by USER — no turns yet."""
    await harness.conversations.upsert(a_conversation(last_message_at=None))


# --- opening a thread -------------------------------------------------------

async def test_start_conversation_derives_a_short_title_and_no_activity_yet():
    harness = _harness()
    long_opening = "Aidez-moi à préparer ma candidature " + "très " * 40 + "urgente"
    conversation = await harness.service.start_conversation(
        USER, now=NOW, title=long_opening)
    assert conversation.user_id == USER
    assert conversation.last_message_at is None
    assert 0 < len(conversation.title) <= 80


async def test_start_conversation_without_a_title_uses_a_default():
    harness = _harness()
    conversation = await harness.service.start_conversation(USER, now=NOW, title="   ")
    assert conversation.title == "New conversation"


# --- the user's message -----------------------------------------------------

async def test_the_first_user_message_is_persisted_at_sequence_zero():
    harness = _harness()
    await _seed_conversation(harness)
    turn = await harness.service.send_message(USER, CONVERSATION, "Bonjour", now=NOW)
    assert turn.user_message.sequence == 0
    assert turn.user_message.role is ChatMessageRole.USER
    assert turn.user_message.content == "Bonjour"
    assert turn.user_message.id == chat_message_id(CONVERSATION, 0)


async def test_sending_a_message_stamps_the_threads_activity():
    harness = _harness()
    await _seed_conversation(harness)
    await harness.service.send_message(USER, CONVERSATION, "Bonjour", now=NOW)
    stored = await harness.conversations.get(USER, CONVERSATION)
    assert stored is not None
    assert stored.last_message_at == NOW


async def test_an_empty_message_is_refused_before_anything_is_written():
    harness = _harness()
    await _seed_conversation(harness)
    with pytest.raises(EmptyChatMessage):
        await harness.service.send_message(USER, CONVERSATION, "   ", now=NOW)
    assert harness.messages.messages == {}


async def test_a_turn_on_another_accounts_conversation_reads_as_absent():
    harness = _harness()
    await harness.conversations.upsert(
        a_conversation(user_id=OTHER_USER, last_message_at=None))
    with pytest.raises(ConversationNotFound):
        await harness.service.send_message(USER, CONVERSATION, "Bonjour", now=NOW)


# --- the assistant turn: prose stored, provenance stamped -------------------

async def test_a_completed_turn_persists_the_assistant_message_with_its_provenance():
    harness = _harness(provider=_provider(text="Vous avez trois candidatures."))
    await _seed_conversation(harness)
    turn = await harness.service.send_message(USER, CONVERSATION, "Où en suis-je ?",
                                              now=NOW)
    assert turn.assistant_message is not None
    assert turn.assistant_message.role is ChatMessageRole.ASSISTANT
    assert turn.assistant_message.content == "Vous avez trois candidatures."
    assert turn.assistant_message.sequence == 1
    assert turn.run is not None and turn.run.status is LLMRunStatus.SUCCEEDED
    assert turn.assistant_message.llm_run_id == turn.run.id
    assert turn.assistant_message.provider_key == "conn_gateway"
    assert turn.proposals == ()


async def test_a_fenced_proposal_becomes_a_stored_proposed_action():
    reply = _fenced_reply("Je peux vous aider.", _navigate("APPLICATIONS"))
    harness = _harness(provider=_provider(text=reply))
    await _seed_conversation(harness)
    turn = await harness.service.send_message(USER, CONVERSATION, "Aide", now=NOW)
    (proposal,) = turn.proposals
    assert proposal.status is ChatActionProposalStatus.PROPOSED
    assert proposal.ordinal == 0
    assert proposal.summary == "do thing 0"
    assert isinstance(proposal.action, NavigateAction)
    assert proposal.action.target is NavigationTarget.APPLICATIONS
    # the ids are derived, so a re-finalize would write the same rows
    assert turn.assistant_message is not None
    assert turn.assistant_message.id == chat_message_id(CONVERSATION, 1)
    assert proposal.id == chat_action_proposal_id(turn.assistant_message.id, 0)


async def test_the_proposal_block_never_survives_into_the_stored_prose():
    reply = _fenced_reply("Voici ma suggestion.", _navigate())
    harness = _harness(provider=_provider(text=reply))
    await _seed_conversation(harness)
    turn = await harness.service.send_message(USER, CONVERSATION, "?", now=NOW)
    assert turn.assistant_message is not None
    assert turn.assistant_message.content == "Voici ma suggestion."
    assert PROPOSAL_FENCE_TAG not in turn.assistant_message.content
    assert "kind" not in turn.assistant_message.content


async def test_a_proposal_only_turn_falls_back_to_the_summary_for_content():
    # The model emitted only a fenced block, no prose; content is NonEmptyStr, so it
    # falls back to the proposal's own caption rather than storing an empty string.
    reply = _fenced_reply("", _navigate())
    harness = _harness(provider=_provider(text=reply))
    await _seed_conversation(harness)
    turn = await harness.service.send_message(USER, CONVERSATION, "?", now=NOW)
    assert turn.assistant_message is not None
    assert turn.assistant_message.content == "do thing 0"


# --- the request: versioned prompt + snapshot + history ---------------------

async def test_the_request_carries_the_versioned_prompt_and_the_snapshot():
    harness = _harness()
    await _seed_conversation(harness)
    await harness.service.send_message(USER, CONVERSATION, "Ma question", now=NOW)
    request = harness.provider.seen[-1]
    assert request.system == CAREER_CHAT_V1.instructions
    assert request.prompt_name == "career_chat"
    assert request.prompt_version == "1.0"
    assert request.structured_output is None
    current = request.messages[-1]
    assert current.role is MessageRole.USER
    assert "YOUR SITUATION" in current.content
    assert "=== USER MESSAGE ===" in current.content
    assert "Ma question" in current.content


async def test_history_is_replayed_to_the_model_oldest_first():
    harness = _harness()
    await _seed_conversation(harness)
    await harness.service.send_message(USER, CONVERSATION, "Première question", now=NOW)
    await harness.service.send_message(USER, CONVERSATION, "Deuxième question", now=NOW)
    request = harness.provider.seen[-1]
    # prior user turn, prior assistant turn, then the current turn — in order.
    assert len(request.messages) == 3
    assert request.messages[0].role is MessageRole.USER
    assert request.messages[0].content == "Première question"
    assert request.messages[1].role is MessageRole.ASSISTANT
    assert request.messages[1].content == "Bonjour."
    assert request.messages[2].role is MessageRole.USER
    assert "Deuxième question" in request.messages[2].content


# --- streaming and failure --------------------------------------------------

async def test_tokens_stream_before_a_terminal_completed_event():
    reply = _fenced_reply("Voici.", _navigate())
    harness = _harness(provider=_provider(text=reply))
    await _seed_conversation(harness)
    stream = await harness.service.stream_turn(USER, CONVERSATION, "?", now=NOW)
    events = [event async for event in stream]
    assert events[-1].type is ChatStreamEventType.COMPLETED
    tokens = [e.text for e in events if e.type is ChatStreamEventType.TOKEN]
    assert "".join(t for t in tokens if t) == reply  # raw prose+block flows to the client
    assert events[-1].message is not None
    assert events[-1].message.content == "Voici."  # but the stored prose is clean


async def test_a_provider_failure_yields_an_error_and_writes_no_assistant_message():
    provider = _provider(error=LLMError(LLMFailureCode.PROVIDER_INTERNAL_ERROR))
    harness = _harness(provider=provider)
    await _seed_conversation(harness)
    turn = await harness.service.send_message(USER, CONVERSATION, "Bonjour", now=NOW)
    assert turn.assistant_message is None
    assert turn.error_code == LLMFailureCode.PROVIDER_INTERNAL_ERROR.value
    assert turn.run is not None and turn.run.status is LLMRunStatus.FAILED
    # only the user's message was persisted — a failed turn writes no assistant row
    assert len(harness.messages.messages) == 1
    assert harness.proposals.proposals == {}


async def test_a_refusal_with_no_eligible_provider_surfaces_as_an_error():
    # A LOCAL_ONLY policy over a single remote provider leaves nothing eligible.
    provider = _provider(local=False)
    harness = _harness(provider=provider,
                       policy=RoutingPolicy(privacy=PrivacyClass.LOCAL_ONLY))
    await _seed_conversation(harness)
    turn = await harness.service.send_message(USER, CONVERSATION, "Bonjour", now=NOW)
    assert turn.assistant_message is None
    assert turn.error_code is not None
    assert turn.run is None  # no provider ran, so no telemetry run was written
    assert harness.runs.runs == {}
    assert len(harness.messages.messages) == 1  # the user message still persisted


