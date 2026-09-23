# tests/test_v2_llm_contracts.py
"""The typed contract every provider speaks: what a request requires, what a stream is.

The router filters on `LLMRequest.required_capabilities()`, so a request's shape and
the capabilities it demands must not drift — asking for structured output *is*
requiring `STRUCTURED_OUTPUT`. And a stream has a life (one STARTED, one terminal),
which `validate_stream_order` makes checkable. These tests pin both, plus the frozen,
closed nature of the values (a hallucinated key on a parsed response is an error).
"""
import pytest
from pydantic import ValidationError

from backend.app.llm.capabilities import BASELINE_CAPABILITY, Capability
from backend.app.llm.contracts import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    MessageRole,
    StreamEventType,
    StructuredOutputSpec,
    validate_stream_order,
)


def _request(**overrides) -> LLMRequest:
    fields = {"messages": (LLMMessage.user("hi"),)}
    fields.update(overrides)
    return LLMRequest(**fields)


# --- required capabilities are derived from the request shape --------------

def test_a_plain_request_requires_only_text_generation():
    assert _request().required_capabilities() == frozenset({BASELINE_CAPABILITY})


def test_structured_output_requires_the_capability():
    request = _request(structured_output=StructuredOutputSpec(
        name="x", schema={"type": "object"}))
    assert Capability.STRUCTURED_OUTPUT in request.required_capabilities()


def test_a_system_prompt_requires_system_instructions():
    assert Capability.SYSTEM_INSTRUCTIONS in _request(
        system="be terse").required_capabilities()


def test_reasoning_effort_requires_reasoning_control():
    assert Capability.REASONING_CONTROL in _request(
        reasoning_effort="high").required_capabilities()


def test_a_session_resume_requires_the_capability():
    from backend.app.llm.contracts import SessionContext
    request = _request(session=SessionContext(external_session_id="sess-1"))
    assert Capability.SESSION_RESUME in request.required_capabilities()


# --- the values are frozen and closed --------------------------------------

def test_an_unknown_field_on_a_response_is_rejected():
    with pytest.raises(ValidationError):
        LLMResponse(text="hi", hallucinated_field="oops")


def test_a_request_needs_at_least_one_message():
    with pytest.raises(ValidationError):
        LLMRequest(messages=())


def test_a_tool_message_must_carry_its_tool_call_id():
    with pytest.raises(ValidationError):
        LLMRequest(messages=(LLMMessage(role=MessageRole.TOOL, content="result"),))


# --- stream ordering -------------------------------------------------------

def test_a_well_formed_stream_validates():
    events = [
        LLMStreamEvent.started(),
        LLMStreamEvent.text_delta("hello"),
        LLMStreamEvent.completed(LLMResponse(text="hello")),
    ]
    validate_stream_order(events)  # does not raise


def test_a_stream_must_open_with_started():
    with pytest.raises(ValueError):
        validate_stream_order([LLMStreamEvent.text_delta("x"),
                               LLMStreamEvent.completed(LLMResponse(text="x"))])


def test_a_stream_must_end_with_a_terminal_event():
    with pytest.raises(ValueError):
        validate_stream_order([LLMStreamEvent.started(),
                               LLMStreamEvent.text_delta("x")])


def test_a_stream_carries_exactly_one_started():
    with pytest.raises(ValueError):
        validate_stream_order([
            LLMStreamEvent.started(),
            LLMStreamEvent.started(),
            LLMStreamEvent.completed(LLMResponse(text="x")),
        ])


def test_an_empty_delta_is_rejected():
    with pytest.raises(ValidationError):
        LLMStreamEvent(type=StreamEventType.TEXT_DELTA, text="")


def test_an_error_event_must_carry_a_code():
    with pytest.raises(ValidationError):
        LLMStreamEvent(type=StreamEventType.ERROR)
