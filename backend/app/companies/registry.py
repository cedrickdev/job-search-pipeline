"""Which company discovery providers exist, which a country may use, in what order.

The company-side counterpart of `backend.app.discovery.registry`, and written the
same way for the same reason: this module contains **no provider list**. §18 asks
that the orchestrator not be coupled to `GreenhouseCompanyProvider()`, and the only
durable way to honour that is for neither the registry nor the orchestrator to be
able to name one — `bootstrap.py` builds the populated registry.

Deliberately a separate class from `SourceRegistry` rather than a generic one
shared by both. They filter on different things: a source is chosen by capability
and by a pack's `SourceBinding`, a provider by capability and by country. A generic
registry parameterised over both would have to accept a pack it ignores half the
time, and the type that made that safe would be harder to read than the fifty lines
duplicated here.

Mutable, like its Phase 5 twin: `register` is called once per provider at startup,
`record_health` once per provider per pass.
"""
from collections.abc import Iterable, Iterator
from enum import StrEnum

from backend.app.companies.contracts import (
    CompanyDiscoveryCapability,
    CompanyDiscoveryProvider,
    CompanyProviderMetadata,
    ProviderHealth,
)
from backend.app.domain.base import CountryCode


class ProviderRegistryErrorCode(StrEnum):
    """Why a registry call could not be answered."""

    DUPLICATE_PROVIDER = "DUPLICATE_PROVIDER"
    UNKNOWN_PROVIDER = "UNKNOWN_PROVIDER"


class ProviderRegistryError(Exception):
    """A registry contract was broken — at composition time, never mid-pass.

    Loud on purpose: a duplicate key means two providers answer to one name, and
    `company_discovery_records.provider_key` would then point at whichever one
    happened to be registered second. That is a bug to fix before the process serves
    a request, not a condition to degrade around.
    """

    def __init__(self, code: ProviderRegistryErrorCode, message: str, *,
                 provider_key: str | None = None) -> None:
        self.code = code
        self.provider_key = provider_key
        subject = f" [{provider_key}]" if provider_key else ""
        super().__init__(f"{code}{subject}: {message}")


class CompanyProviderRegistry:
    """The set of registered providers, and the rules for choosing among them."""

    def __init__(self) -> None:
        self._providers: dict[str, CompanyDiscoveryProvider] = {}
        self._health: dict[str, ProviderHealth] = {}

    def register(self, provider: CompanyDiscoveryProvider) -> None:
        """Add one provider. Its `metadata.provider_key` is its identity."""
        key = provider.metadata.provider_key
        if key in self._providers:
            raise ProviderRegistryError(
                ProviderRegistryErrorCode.DUPLICATE_PROVIDER,
                "a provider with this key is already registered", provider_key=key)
        self._providers[key] = provider

    def register_all(self, providers: Iterable[CompanyDiscoveryProvider]) -> None:
        for provider in providers:
            self.register(provider)

    def get(self, provider_key: str) -> CompanyDiscoveryProvider:
        try:
            return self._providers[provider_key]
        except KeyError as exc:
            raise ProviderRegistryError(
                ProviderRegistryErrorCode.UNKNOWN_PROVIDER,
                "no provider is registered under this key",
                provider_key=provider_key) from exc

    def metadata_for(self, provider_key: str) -> CompanyProviderMetadata:
        return self.get(provider_key).metadata

    def __contains__(self, provider_key: object) -> bool:
        return isinstance(provider_key, str) and provider_key in self._providers

    def __len__(self) -> int:
        return len(self._providers)

    def __iter__(self) -> Iterator[CompanyDiscoveryProvider]:
        """Every registered provider, ordered as `providers_for` would order it."""
        return iter(self._ordered(self._providers.values()))

    @property
    def provider_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def _ordered(self, providers: Iterable[CompanyDiscoveryProvider],
                 ) -> tuple[CompanyDiscoveryProvider, ...]:
        """Sort by priority, then by key — never by registration order.

        The key tie-break is what makes a pass reproducible: two providers at the
        same priority must not swap places between runs, or a `limit` that truncates
        the tail truncates a *different* tail each time and §23's idempotence
        becomes a coin flip.
        """
        return tuple(sorted(providers, key=lambda p: (p.metadata.priority,
                                                      p.metadata.provider_key)))

    def providers_for(
        self,
        *,
        country: CountryCode | None = None,
        required_capabilities: frozenset[CompanyDiscoveryCapability] = frozenset(),
        provider_keys: tuple[str, ...] = (),
        include_disabled: bool = False,
    ) -> tuple[CompanyDiscoveryProvider, ...]:
        """The providers eligible for one request, in the order to run them (§18).

        Three subtractive filters, plus the enabled switch:

        - `country` — the provider must serve it. Declaring none serves all, which is
          what makes the configured-ATS provider reusable by every pack.
        - `required_capabilities` — every one of them claimed. No `supports_reliably`
          distinction here, unlike Phase 5: no company provider accepts a parameter
          it honours only as a hint, and inventing the distinction before there is a
          case for it would be decoration.
        - `provider_keys` — the caller's allow-list, straight off
          `CompanyDiscoveryRequest.provider_keys`.

        Returns an empty tuple rather than raising when nothing matches: an empty
        provider set is a legitimate, reportable outcome, and the orchestrator turns
        it into a `NO_PROVIDER_SELECTED` warning.
        """
        selected: list[CompanyDiscoveryProvider] = []
        for provider in self._providers.values():
            metadata = provider.metadata
            if not include_disabled and not metadata.enabled:
                continue
            if country is not None and not metadata.serves(country):
                continue
            if provider_keys and metadata.provider_key not in provider_keys:
                continue
            if any(not metadata.supports(capability)
                   for capability in required_capabilities):
                continue
            selected.append(provider)
        return self._ordered(selected)

    def record_health(self, health: ProviderHealth) -> None:
        """Remember the last thing a provider said about itself.

        Kept here so a status page can ask "what is the state of company discovery?"
        without running a pass. Last-write-wins, in-process, deliberately not
        persisted: Phase 6 adds no health table, and a stale row would be worse than
        no row.
        """
        self._health[health.source_key] = health

    def health_for(self, provider_key: str) -> ProviderHealth | None:
        return self._health.get(provider_key)

    def health(self) -> tuple[ProviderHealth, ...]:
        """Every health record held, in the same deterministic key order."""
        return tuple(self._health[key] for key in sorted(self._health))

    def unusable_provider_keys(self) -> tuple[str, ...]:
        """Registered providers whose last word was not usable, for a status page."""
        return tuple(key for key in sorted(self._health)
                     if not self._health[key].is_usable)
