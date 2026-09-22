"""One adapter for every OpenAI-compatible endpoint — local server or hosted API.

Ollama, LM Studio, a self-hosted vLLM, a hosted OpenAI-compatible gateway: they all
speak the same `/chat/completions` wire format, so they are one adapter configured
differently, not four (docs/LLM_PROVIDER_ARCHITECTURE.md §14). What differs is the
transport classification and the credential:

- a **local** provider (`LOCAL_OPENAI_COMPATIBLE`) points at a loopback address, is
  keyless, and claims `LOCAL_EXECUTION` — the prompt never leaves the machine;
- a **remote** provider (`OPENAI_COMPATIBLE_API`) points at an https host, carries a
  bearer credential, and does not claim `LOCAL_EXECUTION`.

The `base_url` is validated once at construction by `net_policy.validate_base_url`,
which is what closes the SSRF hole a user-supplied URL opens (§16-18); the
`LOCAL_EXECUTION` capability is then set from the *validated address class*, never
from the connection's label (§89) — an "Ollama" connection whose URL resolves remote
is remote.

One contained `httpx.AsyncClient` is built per call and closed with it (§72): a
pooled client held across requests would outlive the connection's credential and
make a settings change take effect only after a restart. `transport` is a test seam
for `httpx.MockTransport`, the same seam V1's `stream_local_model` uses, so the whole
adapter is testable without a socket.
"""
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from backend.app.llm.capabilities import Capability
from backend.app.llm.contracts import (
    DEFAULT_OUTPUT_CAP_BYTES,
    FinishReason,
    LLMProviderMetadata,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    MessageRole,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderTransport,
    TokenUsage,
)
from backend.app.llm.failures import (
    LLMError,
    LLMFailureCode,
    classify_provider_failure,
)
from backend.app.llm.providers.net_policy import validate_base_url

# Headers a caller must not override: they are set by the client from the request
# body and the target, and letting a "custom header" replace one is how a request is
# smuggled to the wrong host or its length forged (§89). `authorization` is here too
# — the credential is set from the connection's stored secret, never from a free
# header a user typed.
_RESERVED_HEADERS = frozenset({"host", "content-length", "authorization",
                               "content-type"})

_OPENAI_ROLES = {
    MessageRole.SYSTEM: "system",
    MessageRole.USER: "user",
    MessageRole.ASSISTANT: "assistant",
    MessageRole.TOOL: "tool",
}


def validate_custom_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Refuse a custom header that would override a reserved one (§89).

    Raised as `PROVIDER_MISCONFIGURED` at construction, not silently dropped: a user
    who set `Host` expecting it to route somewhere must be told it will not, rather
    than have the request go to the base URL's host and the header ignored.
    """
    cleaned: dict[str, str] = {}
    for name, value in headers.items():
        if name.strip().lower() in _RESERVED_HEADERS:
            raise LLMError(
                LLMFailureCode.PROVIDER_MISCONFIGURED,
                detail=f"the custom header {name!r} is reserved and cannot be set")
        cleaned[name] = value
    return cleaned


class OpenAICompatibleProvider:
    """An OpenAI-compatible chat endpoint, behind the `LLMProvider` contract.

    Configured, not subclassed, for local versus remote: the difference is the
    validated address class and whether a credential is carried, both handled here.
    """

    def __init__(
        self,
        *,
        provider_key: str,
        display_name: str,
        base_url: str,
        default_model: str,
        transport: ProviderTransport,
        api_key: SecretStr | None = None,
        extra_headers: Mapping[str, str] | None = None,
        priority: int = 100,
        http_transport: httpx.AsyncBaseTransport | None = None,
        output_cap_bytes: int = DEFAULT_OUTPUT_CAP_BYTES,
    ) -> None:
        if transport not in (ProviderTransport.OPENAI_COMPATIBLE_API,
                             ProviderTransport.LOCAL_OPENAI_COMPATIBLE):
            raise LLMError(
                LLMFailureCode.PROVIDER_MISCONFIGURED,
                detail="this adapter serves only OpenAI-compatible transports")
        require_local = transport is ProviderTransport.LOCAL_OPENAI_COMPATIBLE
        self._url = validate_base_url(base_url, require_local=require_local)
        # The label says local; the validated address decides. A local transport
        # whose URL resolved non-loopback was already refused by `validate_base_url`,
        # so reaching here means the two agree — but the capability is set from the
        # address, which is the invariant §89 asks for.
        self._local = self._url.is_local
        self._provider_key = provider_key
        self._display_name = display_name
        self._transport = transport
        self._default_model = default_model
        self._api_key = api_key
        self._extra_headers = validate_custom_headers(extra_headers or {})
        self._priority = priority
        self._http_transport = http_transport
        self._output_cap = output_cap_bytes

    @property
    def metadata(self) -> LLMProviderMetadata:
        capabilities = {
            Capability.TEXT_GENERATION,
            Capability.STREAMING,
            Capability.SYSTEM_INSTRUCTIONS,
            Capability.STRUCTURED_OUTPUT,
            Capability.TOKEN_USAGE,
        }
        if self._local:
            capabilities.add(Capability.LOCAL_EXECUTION)
        return LLMProviderMetadata(
            provider_key=self._provider_key,
            display_name=self._display_name,
            transport=self._transport,
            capabilities=frozenset(capabilities),
            default_model=self._default_model,
            priority=self._priority)

    def _secret_values(self) -> tuple[str, ...]:
        """The concrete secrets to blank from any error — the bearer key, if any."""
        if self._api_key is None:
            return ()
        return (self._api_key.get_secret_value(),)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self._extra_headers}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        return headers

    def _payload(self, request: LLMRequest, *, stream: bool) -> dict[str, Any]:
        messages = _render_messages(request)
        payload: dict[str, Any] = {
            "model": request.model or self._default_model,
            "messages": messages,
            "stream": stream,
        }
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.structured_output is not None:
            # The endpoint is *asked* for JSON; the layer re-validates the result
            # independently (§42), so this is a hint, never a guarantee we trust.
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """One non-streaming completion, parsed into an `LLMResponse`.

        A single contained client, built and closed within the call. Any transport or
        HTTP-status failure is normalized through `classify_provider_failure` with the
        credential blanked, so a 401 body that echoed the key cannot leak.
        """
        url = f"{self._url.normalized}/chat/completions"
        payload = self._payload(request, stream=False)
        try:
            async with self._client(request.timeout_seconds) as client:
                response = await client.post(url, json=payload,
                                             headers=self._headers())
        except httpx.HTTPError as exc:
            raise classify_provider_failure(
                exc, secret_values=self._secret_values()) from exc
        if response.status_code != 200:
            raise classify_provider_failure(
                _HttpStatus(response.status_code),
                http_status=response.status_code,
                secret_values=self._secret_values())
        return self._parse_completion(response.json(), request)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """A streaming completion, emitting STARTED, TEXT_DELTAs, then COMPLETED.

        The SSE `data:` lines are parsed the way V1's `parse_sse_delta` parses them,
        with the whole text reassembled for the terminal COMPLETED so a one-shot
        caller draining this gets the same answer `generate` would. An output cap
        stops a runaway stream with `OUTPUT_LIMIT_EXCEEDED`.
        """
        url = f"{self._url.normalized}/chat/completions"
        payload = self._payload(request, stream=True)
        yield LLMStreamEvent.started()
        chunks: list[str] = []
        produced = 0
        try:
            async with self._client(request.timeout_seconds) as client:
                async with client.stream("POST", url, json=payload,
                                         headers=self._headers()) as response:
                    if response.status_code != 200:
                        await response.aread()
                        normalized = classify_provider_failure(
                            _HttpStatus(response.status_code),
                            http_status=response.status_code,
                            secret_values=self._secret_values())
                        yield LLMStreamEvent.errored(normalized.code, normalized.detail)
                        return
                    async for line in response.aiter_lines():
                        produced += len(line)
                        if produced > self._output_cap:
                            yield LLMStreamEvent.errored(
                                LLMFailureCode.OUTPUT_LIMIT_EXCEEDED,
                                "the provider's output exceeded the adapter's limit")
                            return
                        delta = parse_sse_delta(line)
                        if delta:
                            chunks.append(delta)
                            yield LLMStreamEvent.text_delta(delta)
        except httpx.HTTPError as exc:
            normalized = classify_provider_failure(
                exc, secret_values=self._secret_values())
            yield LLMStreamEvent.errored(normalized.code, normalized.detail)
            return
        yield LLMStreamEvent.completed(LLMResponse(
            text="".join(chunks),
            finish_reason=FinishReason.STOP,
            model=request.model or self._default_model))

    async def healthcheck(self) -> ProviderHealth:
        """Probe the endpoint by listing its models, a cheap keyed GET.

        `GET /models` is the OpenAI-compatible endpoint every server implements and
        that costs no tokens. A 401/403 is `AUTH_REQUIRED` — the fix is a credential,
        not a retry; a connection error is `UNAVAILABLE`. The credential is blanked
        from any detail.
        """
        url = f"{self._url.normalized}/models"
        try:
            async with self._client(10.0) as client:
                response = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            normalized = classify_provider_failure(
                exc, secret_values=self._secret_values())
            return ProviderHealth(status=ProviderHealthStatus.UNAVAILABLE,
                                  detail=normalized.detail)
        if response.status_code == 200:
            return ProviderHealth(status=ProviderHealthStatus.HEALTHY)
        if response.status_code in (401, 403):
            return ProviderHealth(status=ProviderHealthStatus.AUTH_REQUIRED,
                                  detail="the provider refused the credential")
        return ProviderHealth(status=ProviderHealthStatus.UNAVAILABLE,
                              detail=f"the provider answered HTTP {response.status_code}")

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._http_transport)

    def _parse_completion(self, body: Mapping[str, Any],
                          request: LLMRequest) -> LLMResponse:
        """Turn a `/chat/completions` JSON body into an `LLMResponse`.

        A body that is not shaped as the protocol promises is a
        `PROVIDER_PROTOCOL_ERROR` (§73) — the output is untrusted, so a missing
        `choices` is a failure, not an empty answer. Structured output is validated by
        the router, not here; this only lifts the text and the usage.
        """
        try:
            choices = body["choices"]
            message = choices[0]["message"]
            text = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                LLMFailureCode.PROVIDER_PROTOCOL_ERROR,
                detail="the completion response was not shaped as expected") from exc
        finish = _finish_reason(choices[0].get("finish_reason"))
        return LLMResponse(
            text=text,
            usage=_usage_from_body(body.get("usage")),
            finish_reason=finish,
            model=body.get("model") or request.model or self._default_model)


def _render_messages(request: LLMRequest) -> list[dict[str, Any]]:
    """The request's system prompt and turns as OpenAI-compatible message dicts."""
    messages: list[dict[str, Any]] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    for message in request.messages:
        entry: dict[str, Any] = {"role": _OPENAI_ROLES[message.role],
                                 "content": message.content}
        if message.role is MessageRole.TOOL and message.tool_call_id is not None:
            entry["tool_call_id"] = message.tool_call_id
        messages.append(entry)
    return messages


def parse_sse_delta(line: str) -> str | None:
    """Extract assistant text from one OpenAI-compatible streaming line.

    Returns `None` for non-data lines, the `[DONE]` sentinel, and empty/role-only
    deltas — the same rule as V1's `server/chat.py::parse_sse_delta`, so the two
    surfaces agree on what a delta is.
    """
    if not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    try:
        content = obj["choices"][0]["delta"].get("content")
    except (KeyError, IndexError, TypeError):
        return None
    # The parsed body is untrusted JSON (§73): `content` is whatever the provider put
    # there. Only a string is a text delta; anything else (a null, a number) is "no
    # delta here", not a value to coerce.
    return content if isinstance(content, str) else None


def _usage_from_body(usage: object) -> TokenUsage:
    """Token counts from a completion body, keeping the unknown null (§58)."""
    if not isinstance(usage, Mapping):
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
        completion_tokens=_int_or_none(usage.get("completion_tokens")),
        total_tokens=_int_or_none(usage.get("total_tokens")))


def _int_or_none(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _finish_reason(value: object) -> FinishReason:
    mapping = {
        "stop": FinishReason.STOP,
        "length": FinishReason.LENGTH,
        "tool_calls": FinishReason.TOOL_CALLS,
        "content_filter": FinishReason.CONTENT_FILTERED,
    }
    return mapping.get(value, FinishReason.UNKNOWN) if isinstance(value, str) \
        else FinishReason.UNKNOWN


class _HttpStatus(Exception):
    """A carrier so `classify_provider_failure` reads a status off an HTTP response.

    A non-200 completion is not an `httpx` exception — the request succeeded, the
    server refused — so this hands the status to the classifier the same way a raised
    error would, without inventing a message the classifier might forward.
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
