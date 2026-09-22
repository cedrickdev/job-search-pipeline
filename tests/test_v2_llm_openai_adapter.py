# tests/test_v2_llm_openai_adapter.py
"""The OpenAI-compatible adapter, exercised through `httpx.MockTransport` (no socket).

One adapter serves every OpenAI-compatible endpoint — a local server or a hosted
gateway — so these tests pin the wire behaviour that must hold for all of them:
the request carries the bearer credential and the right body, a completion parses
into an `LLMResponse` with usage kept null where the server reported none, a stream
reassembles its deltas, and an HTTP error normalizes to a typed, secret-free
`LLMError`. `MockTransport` is the seam the adapter accepts, so nothing here opens a
connection.
"""
import json

import httpx
import pytest
from pydantic import SecretStr

from backend.app.llm.capabilities import Capability
from backend.app.llm.contracts import (
    LLMMessage,
    LLMRequest,
    ProviderHealthStatus,
    ProviderTransport,
)
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.providers.openai_compatible import (
    OpenAICompatibleProvider,
    parse_sse_delta,
)

pytestmark = pytest.mark.asyncio


def _request(**overrides) -> LLMRequest:
    fields = {"messages": (LLMMessage.user("hi"),)}
    fields.update(overrides)
    return LLMRequest(**fields)


def _completion_body(text: str = "hello", *, usage: dict | None = None) -> dict:
    body = {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "model": "served-model"}
    if usage is not None:
        body["usage"] = usage
    return body


def _remote(handler, *, api_key: str | None = "sk-topsecret-value",
            base_url: str = "https://gateway.example.invalid/v1",
            transport: ProviderTransport = ProviderTransport.OPENAI_COMPATIBLE_API,
            **overrides) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        provider_key="conn_gw",
        display_name="Gateway",
        base_url=base_url,
        default_model="external-model",
        transport=transport,
        api_key=SecretStr(api_key) if api_key is not None else None,
        http_transport=httpx.MockTransport(handler),
        **overrides)


# --- construction / metadata ----------------------------------------------

async def test_a_local_transport_claims_local_execution():
    provider = _remote(lambda r: httpx.Response(200),
                       base_url="http://127.0.0.1:11434/v1", api_key=None,
                       transport=ProviderTransport.LOCAL_OPENAI_COMPATIBLE)
    assert Capability.LOCAL_EXECUTION in provider.metadata.capabilities


async def test_a_remote_transport_does_not_claim_local_execution():
    provider = _remote(lambda r: httpx.Response(200))
    assert Capability.LOCAL_EXECUTION not in provider.metadata.capabilities


async def test_a_custom_header_overriding_a_reserved_one_is_refused():
    with pytest.raises(LLMError) as caught:
        _remote(lambda r: httpx.Response(200),
                extra_headers={"Authorization": "Bearer forged"})
    assert caught.value.code is LLMFailureCode.PROVIDER_MISCONFIGURED


# --- generate --------------------------------------------------------------

async def test_generate_sends_the_bearer_credential_and_parses_the_answer():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion_body(
            "the answer", usage={"prompt_tokens": 12, "completion_tokens": 4,
                                 "total_tokens": 16}))

    provider = _remote(handler)
    response = await provider.generate(_request())
    assert response.text == "the answer"
    assert response.usage.total_tokens == 16
    assert seen["auth"] == "Bearer sk-topsecret-value"
    assert seen["url"].endswith("/chat/completions")
    assert seen["body"]["stream"] is False


async def test_a_keyless_local_call_sends_no_authorization_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_completion_body())

    provider = _remote(handler, base_url="http://127.0.0.1:11434/v1", api_key=None,
                       transport=ProviderTransport.LOCAL_OPENAI_COMPATIBLE)
    await provider.generate(_request())
    assert seen["auth"] is None


async def test_unreported_usage_stays_null():
    provider = _remote(lambda r: httpx.Response(200, json=_completion_body()))
    response = await provider.generate(_request())
    assert response.usage.prompt_tokens is None
    assert response.usage.total_tokens is None


async def test_a_401_normalizes_to_auth_required_without_leaking_the_key():
    def handler(request: httpx.Request) -> httpx.Response:
        # A hostile body that echoes the key back.
        return httpx.Response(401, text="invalid key sk-topsecret-value")

    provider = _remote(handler)
    with pytest.raises(LLMError) as caught:
        await provider.generate(_request())
    assert caught.value.code is LLMFailureCode.PROVIDER_AUTH_REQUIRED
    assert "sk-topsecret-value" not in caught.value.detail


async def test_a_malformed_body_is_a_protocol_error():
    provider = _remote(lambda r: httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(LLMError) as caught:
        await provider.generate(_request())
    assert caught.value.code is LLMFailureCode.PROVIDER_PROTOCOL_ERROR


async def test_a_transport_error_is_normalized():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    provider = _remote(handler)
    with pytest.raises(LLMError) as caught:
        await provider.generate(_request())
    assert caught.value.code is LLMFailureCode.PROVIDER_UNAVAILABLE


# --- stream ----------------------------------------------------------------

async def test_stream_reassembles_its_deltas():
    lines = [
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        "data: [DONE]",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="\n".join(lines))

    provider = _remote(handler)
    events = [e async for e in provider.stream(_request())]
    deltas = [e.text for e in events if e.type.value == "TEXT_DELTA"]
    assert "".join(deltas) == "Hello"
    completed = [e for e in events if e.type.value == "COMPLETED"]
    assert completed and completed[0].response.text == "Hello"


async def test_stream_yields_an_error_event_on_a_bad_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    provider = _remote(handler)
    events = [e async for e in provider.stream(_request())]
    errors = [e for e in events if e.type.value == "ERROR"]
    assert errors and errors[0].error_code == LLMFailureCode.PROVIDER_INTERNAL_ERROR


# --- healthcheck -----------------------------------------------------------

async def test_healthcheck_is_healthy_on_a_200_models_list():
    provider = _remote(lambda r: httpx.Response(200, json={"data": []}))
    health = await provider.healthcheck()
    assert health.status is ProviderHealthStatus.HEALTHY


async def test_healthcheck_is_auth_required_on_a_401():
    provider = _remote(lambda r: httpx.Response(401))
    health = await provider.healthcheck()
    assert health.status is ProviderHealthStatus.AUTH_REQUIRED


# --- the SSE delta parser --------------------------------------------------

async def test_parse_sse_delta_reads_content():
    assert parse_sse_delta('data: {"choices":[{"delta":{"content":"x"}}]}') == "x"


async def test_parse_sse_delta_ignores_done_and_non_data():
    assert parse_sse_delta("data: [DONE]") is None
    assert parse_sse_delta(": comment") is None
    assert parse_sse_delta('data: {"choices":[{"delta":{"role":"assistant"}}]}') is None
