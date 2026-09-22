# tests/test_v2_llm_prompts.py
"""The prompt registry: versioned, model-independent, renders a provider-neutral request.

A prompt is a stable artifact the way a provider is — held behind a registry, keyed by
name, stamped onto every telemetry run. These tests pin what that buys: a template
renders an `LLMRequest` that names no provider but carries the prompt's provenance and
its structured-output schema; the truth rule the Evidence Guard enforces is *stated* in
every candidate-facing prompt; a declared variable that the caller forgets is a render
error before any provider is reached, not a literal brace sent to a model.
"""
import pytest
from pydantic import ValidationError

from backend.app.llm.capabilities import Capability
from backend.app.llm.contracts import MessageRole, TaskPurpose
from backend.app.llm.prompts import (
    COVER_LETTER_V1,
    RESUME_TAILORING_V1,
    PromptName,
    PromptRegistry,
    PromptRenderError,
    PromptTemplate,
    default_prompt_registry,
)


# --- the shipped registry --------------------------------------------------

def test_the_default_registry_holds_the_two_document_prompts():
    registry = default_prompt_registry()
    assert set(registry.names) == {PromptName.RESUME_TAILORING, PromptName.COVER_LETTER}
    assert PromptName.RESUME_TAILORING in registry
    assert PromptName.COVER_LETTER in registry


def test_getting_an_unregistered_name_is_a_render_error():
    registry = default_prompt_registry()
    with pytest.raises(PromptRenderError):
        registry.get(PromptName.MATCHING)


def test_registering_two_templates_under_one_name_is_refused():
    with pytest.raises(ValueError):
        PromptRegistry((RESUME_TAILORING_V1, RESUME_TAILORING_V1))


def test_contains_is_false_for_a_non_name():
    assert "resume_tailoring" not in default_prompt_registry()


# --- rendering into a provider-neutral request -----------------------------

def test_rendering_produces_a_request_that_names_no_provider_but_stamps_provenance():
    request = RESUME_TAILORING_V1.render(user_content="EVIDENCE + POSTING")
    # provenance for telemetry (§56)
    assert request.prompt_name == "resume_tailoring"
    assert request.prompt_version == "1.0"
    assert request.purpose is TaskPurpose.RESUME_TAILORING
    # the payload is the single user message; the instructions are the system prompt
    assert len(request.messages) == 1
    assert request.messages[0].role is MessageRole.USER
    assert request.messages[0].content == "EVIDENCE + POSTING"
    assert request.system is not None
    # a template picks no model — a connection's default serves (§40)
    assert request.model is None


def test_a_structured_prompt_requires_structured_output_and_carries_its_schema():
    request = RESUME_TAILORING_V1.render(user_content="x")
    assert request.structured_output is not None
    assert request.structured_output.name == "resume_tailoring"
    assert Capability.STRUCTURED_OUTPUT in request.required_capabilities()
    # the schema is the model's target; the layer re-validates against ResumeDocument
    assert request.structured_output.schema_["type"] == "object"


def test_the_cover_letter_schema_demands_evidence_backed_body_paragraphs():
    schema = COVER_LETTER_V1.render(user_content="x").structured_output.schema_
    body = schema["properties"]["body"]
    assert body["items"]["required"] == ["text", "evidence_ids"]
    assert body["items"]["properties"]["evidence_ids"]["minItems"] == 1


# --- the truth rule is stated in every candidate-facing prompt -------------

def test_every_document_prompt_states_the_never_invent_rule():
    for template in (RESUME_TAILORING_V1, COVER_LETTER_V1):
        instructions = template.instructions.lower()
        assert "never invent" in instructions
        assert "evidence" in instructions


def test_the_document_prompts_warn_against_posting_borne_instructions():
    # The prompt-injection defence stated (the guard enforces it regardless).
    for template in (RESUME_TAILORING_V1, COVER_LETTER_V1):
        assert "ignore" in template.instructions.lower()


# --- variables are filled, and a missing one fails before any provider -----

def test_a_declared_variable_is_substituted():
    template = PromptTemplate(
        name=PromptName.INTERVIEW_PREP,
        version="1.0",
        purpose=TaskPurpose.INTERVIEW_PREP,
        instructions="Prepare the candidate for a {role} interview.",
        variables=("role",))
    request = template.render(user_content="evidence",
                              variables={"role": "staff engineer"})
    assert "staff engineer" in request.system
    assert "{role}" not in request.system


def test_a_missing_variable_is_a_render_error():
    template = PromptTemplate(
        name=PromptName.INTERVIEW_PREP,
        version="1.0",
        purpose=TaskPurpose.INTERVIEW_PREP,
        instructions="Prepare for a {role} interview.",
        variables=("role",))
    with pytest.raises(PromptRenderError):
        template.render(user_content="evidence")


def test_declaring_a_variable_the_instructions_never_use_is_refused():
    with pytest.raises(ValidationError):
        PromptTemplate(
            name=PromptName.INTERVIEW_PREP,
            version="1.0",
            purpose=TaskPurpose.INTERVIEW_PREP,
            instructions="No placeholder here.",
            variables=("role",))


# --- the template is a frozen, closed, well-formed value -------------------

def test_a_malformed_version_is_refused():
    with pytest.raises(ValidationError):
        PromptTemplate(
            name=PromptName.CAREER_CHAT,
            version="v1",  # not \\d+.\\d+
            purpose=TaskPurpose.CAREER_CHAT,
            instructions="chat")


def test_an_unknown_field_is_refused():
    with pytest.raises(ValidationError):
        PromptTemplate(
            name=PromptName.CAREER_CHAT,
            version="1.0",
            purpose=TaskPurpose.CAREER_CHAT,
            instructions="chat",
            hallucinated=True)


def test_the_stamp_is_name_slash_version():
    assert RESUME_TAILORING_V1.stamp == "resume_tailoring/1.0"
