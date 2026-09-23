"""Which LLM providers exist, and the rules for choosing among them.

The counterpart of `backend.app.companies.registry`, written the same way for the
same reason: this module names **no provider**. The "no hard-coded provider" rule
(docs/LLM_PROVIDER_ARCHITECTURE.md §3) is only durable if neither the registry nor
the router can name `ClaudeCodeProvider()` — `bootstrap.py` builds the populated
registry, and the router filters over whatever it holds.

The registry holds providers under their stable `provider_key` and keeps the last
health each reported, so a settings page can answer "what is the state of the LLM
platform?" without running a generation. Health is in-process and last-write-wins,
deliberately not persisted: a stale "healthy" row would be worse than no row.

Ordering is total — priority, then key — so a fallback chain is reproducible: two
providers at the same priority must not swap places between requests, or which one
serves a task becomes a coin flip.
"""
from collections.abc import Iterable, Iterator
from enum import StrEnum

from backend.app.llm.capabilities import Capability
from backend.app.llm.contracts import (
    LLMProvider,
    LLMProviderMetadata,
    ProviderHealth,
)


class LLMRegistryErrorCode(StrEnum):
    """Why a registry call could not be answered — at composition time, never mid-call."""

    DUPLICATE_PROVIDER = "DUPLICATE_PROVIDER"
    UNKNOWN_PROVIDER = "UNKNOWN_PROVIDER"


class LLMRegistryError(Exception):
    """A registry contract was broken while composing the platform.

    Loud on purpose: a duplicate key means two providers answer to one name, and an
    `LLMRun.provider_key` would then point at whichever was registered second. That
    is a bug to fix before the process serves a request, not a condition to degrade
    around.
    """

    def __init__(self, code: LLMRegistryErrorCode, message: str, *,
                 provider_key: str | None = None) -> None:
        self.code = code
        self.provider_key = provider_key
        subject = f" [{provider_key}]" if provider_key else ""
        super().__init__(f"{code}{subject}: {message}")


class LLMProviderRegistry:
    """The set of registered providers, and the rules for choosing among them."""

    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}
        self._health: dict[str, ProviderHealth] = {}

    def register(self, provider: LLMProvider) -> None:
        """Add one provider. Its `metadata.provider_key` is its identity."""
        key = provider.metadata.provider_key
        if key in self._providers:
            raise LLMRegistryError(
                LLMRegistryErrorCode.DUPLICATE_PROVIDER,
                "a provider with this key is already registered", provider_key=key)
        self._providers[key] = provider

    def register_all(self, providers: Iterable[LLMProvider]) -> None:
        for provider in providers:
            self.register(provider)

    def get(self, provider_key: str) -> LLMProvider:
        try:
            return self._providers[provider_key]
        except KeyError as exc:
            raise LLMRegistryError(
                LLMRegistryErrorCode.UNKNOWN_PROVIDER,
                "no provider is registered under this key",
                provider_key=provider_key) from exc

    def metadata_for(self, provider_key: str) -> LLMProviderMetadata:
        return self.get(provider_key).metadata

    def __contains__(self, provider_key: object) -> bool:
        return isinstance(provider_key, str) and provider_key in self._providers

    def __len__(self) -> int:
        return len(self._providers)

    def __iter__(self) -> Iterator[LLMProvider]:
        """Every registered provider, in the order `candidates` would order it."""
        return iter(self._ordered(self._providers.values()))

    @property
    def provider_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def _ordered(self, providers: Iterable[LLMProvider]) -> tuple[LLMProvider, ...]:
        """Sort by priority, then by key — never by registration order.

        The key tie-break is what makes a fallback chain reproducible: two providers
        at one priority must not swap between requests, or a task that falls back
        lands on a different provider each time.
        """
        return tuple(sorted(providers, key=lambda p: (p.metadata.priority,
                                                       p.metadata.provider_key)))

    def candidates(
        self,
        *,
        required_capabilities: frozenset[Capability] = frozenset(),
        local_only: bool = False,
        provider_keys: tuple[str, ...] = (),
        usable_only: bool = True,
    ) -> tuple[LLMProvider, ...]:
        """The providers eligible for one request, in the order to try them (§28).

        Subtractive filters, each a fact the request or the policy states:

        - `required_capabilities` — every one claimed, so a task needing structured
          output never reaches a provider that cannot produce it;
        - `local_only` — the provider runs on this machine, which is how a
          `LOCAL_ONLY` privacy class keeps a prompt off the network (§12);
        - `provider_keys` — the caller's allow-list, for a `SPECIFIC_CONNECTION_ONLY`
          policy or an operator pinning one provider;
        - `usable_only` — the last health was not a hard failure. On by default;
          `UNKNOWN` counts as usable, so a provider nobody probed is still tried.

        Returns an empty tuple rather than raising when nothing matches: an empty
        candidate set is a legitimate outcome the router turns into a typed refusal,
        not an exception the registry throws.
        """
        selected: list[LLMProvider] = []
        for provider in self._providers.values():
            metadata = provider.metadata
            if not metadata.supports_all(required_capabilities):
                continue
            if local_only and not metadata.local:
                continue
            if provider_keys and metadata.provider_key not in provider_keys:
                continue
            if usable_only and not self.health_for(metadata.provider_key).is_usable:
                continue
            selected.append(provider)
        return self._ordered(selected)

    def record_health(self, provider_key: str, health: ProviderHealth) -> None:
        """Remember the last thing a provider said about itself.

        Last-write-wins, in-process, deliberately not persisted (§37): a status page
        reads it without running a generation, and a stale persisted row would
        mislead more than an unknown would.
        """
        if provider_key not in self._providers:
            raise LLMRegistryError(
                LLMRegistryErrorCode.UNKNOWN_PROVIDER,
                "cannot record health for a provider that is not registered",
                provider_key=provider_key)
        self._health[provider_key] = health

    def health_for(self, provider_key: str) -> ProviderHealth:
        """The last health reported, or `UNKNOWN` if the provider was never probed."""
        return self._health.get(provider_key, ProviderHealth.unknown())

    def health(self) -> dict[str, ProviderHealth]:
        """Every registered provider's current health, in deterministic key order.

        Includes providers never probed, as `UNKNOWN`: a settings page must show a
        registered-but-unprobed provider rather than omit it, which is the difference
        between "not deployed" and "not yet checked".
        """
        return {key: self.health_for(key) for key in self.provider_keys}
