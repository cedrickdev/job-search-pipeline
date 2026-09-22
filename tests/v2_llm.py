# tests/v2_llm.py
"""Shared doubles for the provider-neutral LLM platform tests.

A `FakeProvider` that honours the `LLMProvider` contract without a transport — it
returns a canned `LLMResponse`, or raises a canned `LLMError`, and records the
requests it saw. It is what lets the registry, router and recorder tests exercise
selection, fallback and telemetry without a subprocess or a socket: the behaviour
under test is the platform's, not a provider's, so the provider is a stub whose only
job is to be chosen (or not) and to succeed or fail on cue.

The CLI-adapter tests do not use this — they run a real subprocess against a fake
binary script (the V1 `test_chat_core.py` pattern), because a CLI adapter's whole job
is the subprocess mechanics a stub would paper over.
"""
from collections.abc import AsyncIterator

from backend.app.llm.capabilities import BASELINE_CAPABILITY, Capability
from backend.app.llm.contracts import (
    FinishReason,
    LLMProviderMetadata,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderTransport,
    TokenUsage,
)
from backend.app.llm.failures import LLMError


class FakeProvider:
    """A stub `LLMProvider` that answers on cue and remembers what it was asked.

    Construct it with a `provider_key`, the `capabilities` it should claim, whether it
    is `local`, and either the `text` it returns or the `error` it raises. `calls`
    counts how many times `generate` ran and `seen` holds the requests, so a test can
    assert the router tried it, in order, exactly as often as it should have.
    """

    def __init__(
        self,
        *,
        provider_key: str = "fake",
        display_name: str = "Fake Provider",
        transport: ProviderTransport = ProviderTransport.OPENAI_COMPATIBLE_API,
        capabilities: frozenset[Capability] | None = None,
        local: bool = False,
        priority: int = 100,
        text: str = "an answer",
        usage: TokenUsage | None = None,
        model: str | None = "fake-model",
        error: LLMError | None = None,
        fail_times: int | None = None,
        health: ProviderHealth | None = None,
    ) -> None:
        caps = set(capabilities) if capabilities is not None else {BASELINE_CAPABILITY}
        if local:
            caps.add(Capability.LOCAL_EXECUTION)
        self._metadata = LLMProviderMetadata(
            provider_key=provider_key,
            display_name=display_name,
            transport=transport,
            capabilities=frozenset(caps),
            default_model=model,
            priority=priority)
        self._text = text
        self._usage = usage or TokenUsage()
        self._model = model
        self._error = error
        # None → always fail (when `error` is set); an int → fail that many times, then
        # succeed, so a test can prove a bounded retry recovers.
        self._fail_times = fail_times
        self._health = health or ProviderHealth(status=ProviderHealthStatus.HEALTHY)
        self.calls = 0
        self.seen: list[LLMRequest] = []

    @property
    def metadata(self) -> LLMProviderMetadata:
        return self._metadata

    def _should_fail(self) -> bool:
        if self._error is None:
            return False
        if self._fail_times is None:
            return True
        return self.calls <= self._fail_times

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.seen.append(request)
        if self._should_fail():
            assert self._error is not None
            raise self._error
        return LLMResponse(
            text=self._text,
            usage=self._usage,
            finish_reason=FinishReason.STOP,
            model=self._model)

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self.calls += 1
        self.seen.append(request)
        yield LLMStreamEvent.started()
        if self._should_fail():
            assert self._error is not None
            yield LLMStreamEvent.errored(self._error.code, self._error.detail)
            return
        yield LLMStreamEvent.text_delta(self._text)
        yield LLMStreamEvent.completed(LLMResponse(
            text=self._text, usage=self._usage,
            finish_reason=FinishReason.STOP, model=self._model))

    async def healthcheck(self) -> ProviderHealth:
        return self._health
