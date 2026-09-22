"""Composition: build a populated provider registry from stored connections (§67).

The only module in `backend.app.llm` besides `factory` that turns configuration into
providers, and the reason `registry.py` and `router.py` name none: this is where a
user's `LLMConnection` rows become the `LLMProvider`s the router filters over. It
mirrors `backend.app.companies.bootstrap` — a registry is built per use, never a
process-wide singleton, because a registry accumulates health as calls run and a
cached one would share that state between users and between tests.

`build_llm_provider_registry` is deliberately connection-driven: hand it the
connections a request is allowed to use (a user's enabled ones, in priority order)
and it returns the registry the router chooses from. A connection that fails to build
into a provider — an API connection with no model, a credential the cipher cannot
read — raises through the factory rather than being silently dropped, because a
provider that vanished would be indistinguishable from one never configured.
"""
from collections.abc import Iterable

import httpx

from backend.app.llm.connection import LLMConnection
from backend.app.llm.factory import LLMProviderFactory
from backend.app.llm.registry import LLMProviderRegistry
from backend.app.llm.secrets import SecretCipher


def build_llm_provider_registry(
    connections: Iterable[LLMConnection],
    *,
    cipher: SecretCipher | None = None,
    http_transport: "httpx.AsyncBaseTransport | None" = None,
    factory: LLMProviderFactory | None = None,
) -> LLMProviderRegistry:
    """Every provider these connections build into, under its stable key.

    The connections are expected in the order the router should prefer (the repository
    returns them by priority); a duplicate provider key — two Claude Code connections,
    say, which both answer to `claude_code` — keeps the first and skips the rest, so a
    user cannot accidentally register one CLI twice. API connections carry a
    per-connection key and never collide.

    `cipher` decrypts a stored credential when a connection has one; a deployment with
    only CLI and keyless local providers may leave it `None`. `http_transport` is the
    `httpx.MockTransport` seam the tests pass so the built API providers make no real
    network call.
    """
    factory = factory or LLMProviderFactory(cipher=cipher,
                                            http_transport=http_transport)
    registry = LLMProviderRegistry()
    for connection in connections:
        provider = factory.create(connection)
        if provider.metadata.provider_key in registry:
            continue
        registry.register(provider)
    return registry
