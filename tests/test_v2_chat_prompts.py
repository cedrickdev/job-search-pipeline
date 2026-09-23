# tests/test_v2_chat_prompts.py
"""The career-chat system prompt: versioned, non-structured, and the contract stated.

Phase 13's defining rule is "prose has zero authority", and this prompt is the model's
half of it — the text that tells the model it may only *propose* typed actions in a
fenced block a human then confirms. These tests pin the properties the rest of the
control plane relies on: the prompt is conversational (never a structured-output request
that would force the whole turn to JSON), it is kept out of the document registry, its
grammar names the exact fence and bound the parser will enforce, and it enumerates the
whole closed action vocabulary so the model works from a menu rather than inventing a
verb.
"""
from backend.app.chat.prompts import (
    CAREER_CHAT_V1,
    MAX_PROPOSALS_PER_TURN,
    PROPOSAL_FENCE_TAG,
    career_chat_prompt_registry,
)
from backend.app.domain.chat import ChatActionKind, NavigationTarget
from backend.app.llm.capabilities import Capability
from backend.app.llm.contracts import MessageRole, TaskPurpose
from backend.app.llm.prompts import PromptName, default_prompt_registry


# --- a conversational prompt, not a structured one -------------------------

def test_the_chat_prompt_is_conversational_not_structured():
    """No output_schema: the turn is prose the parser reads a fenced block out of.

    A structured-output prompt would ask the provider for JSON mode and make the whole
    answer machine-only — impossible to stream to a user and read. The proposal grammar
    lives *inside* prose instead, which is why the request must not require
    STRUCTURED_OUTPUT.
    """
    assert CAREER_CHAT_V1.output_schema is None
    request = CAREER_CHAT_V1.render(user_content="hello")
    assert request.structured_output is None
    assert Capability.STRUCTURED_OUTPUT not in request.required_capabilities()


def test_the_chat_prompt_names_its_purpose_and_carries_provenance():
    assert CAREER_CHAT_V1.name is PromptName.CAREER_CHAT
    assert CAREER_CHAT_V1.purpose is TaskPurpose.CAREER_CHAT
    assert CAREER_CHAT_V1.stamp == "career_chat/1.0"
    request = CAREER_CHAT_V1.render(user_content="hello")
    assert request.prompt_name == "career_chat"
    assert request.prompt_version == "1.0"
    # the instructions are the system prompt; the turn payload is the one user message
    assert request.system is not None
    assert len(request.messages) == 1
    assert request.messages[0].role is MessageRole.USER
    assert request.model is None


# --- kept out of the document registry -------------------------------------

def test_the_chat_prompt_has_its_own_registry_apart_from_the_documents():
    """Its registry holds only the chat prompt; the document registry is untouched."""
    chat = career_chat_prompt_registry()
    assert set(chat.names) == {PromptName.CAREER_CHAT}
    assert PromptName.CAREER_CHAT in chat

    documents = default_prompt_registry()
    assert set(documents.names) == {PromptName.RESUME_TAILORING, PromptName.COVER_LETTER}
    assert PromptName.CAREER_CHAT not in documents


# --- the control-plane contract is stated ----------------------------------

def test_the_prompt_states_that_prose_has_no_authority():
    text = CAREER_CHAT_V1.instructions.lower()
    assert "prose has no authority" in text
    # a proposal is a request, never permission; the user confirms
    assert "never permission" in text
    assert "confirm" in text
    # and the platform re-checks before running
    assert "re-check" in text


def test_the_prompt_forbids_claiming_an_action_happened():
    text = CAREER_CHAT_V1.instructions.lower()
    assert "never claim" in text
    assert "you propose" in text


def test_the_prompt_states_the_truth_and_injection_rules():
    text = CAREER_CHAT_V1.instructions.lower()
    assert "never invent" in text
    # posting/company text is untrusted data, not a command to follow
    assert "untrusted" in text
    assert "never a command" in text


# --- the grammar names the exact fence and bound the parser enforces -------

def test_the_grammar_names_the_fence_and_the_per_turn_bound():
    text = CAREER_CHAT_V1.instructions
    assert f"```{PROPOSAL_FENCE_TAG}" in text
    assert '"proposals"' in text
    assert '"summary"' in text and '"action"' in text
    assert '"kind"' in text
    assert str(MAX_PROPOSALS_PER_TURN) in text


def test_the_prompt_tells_the_model_to_use_only_context_ids():
    text = CAREER_CHAT_V1.instructions.lower()
    assert "copied verbatim from the context" in text
    assert "never invent, guess or reformat an id" in text


# --- the whole closed vocabulary is on the menu ----------------------------

def test_every_action_kind_appears_in_the_vocabulary():
    """The union decides validity, but a kind absent from the menu is unreachable."""
    for kind in ChatActionKind:
        assert kind.value in CAREER_CHAT_V1.instructions, kind


def test_every_navigation_target_appears():
    for target in NavigationTarget:
        assert target.value in CAREER_CHAT_V1.instructions, target


def test_the_submit_action_is_marked_irreversible_and_still_gated():
    text = CAREER_CHAT_V1.instructions.lower()
    assert "irreversible" in text
    # the chat is one more caller of the submission gate, never a way around it
    assert "submission gate" in text
