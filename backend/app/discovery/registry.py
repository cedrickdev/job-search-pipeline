"""Which sources exist, which ones a country may use, and in what order.

Two things live here and nothing else: a map from a stable key to an
implementation, and the filtering rules §8 asks for. In particular this module
contains **no source list**. `bootstrap.py` builds the populated registry and
`adapters/v1_catalog.py` holds the twelve Swiss-relevant adapters, so the
selection algorithm below never has to name a board — which is the difference
between "add a country by writing a pack" and "add a country by editing the
orchestrator" (§13, acceptance criterion 8).

The registry is mutable, unlike almost everything else in the V2 domain. It is
composition state, not a value: `register` is called once per adapter at startup
and `record_health` is called once per source per sweep.
"""
from collections.abc import Iterable, Iterator
from enum import StrEnum

from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.contracts import (
    OpportunitySource,
    SourceHealth,
    SourceMetadata,
)
from backend.app.domain.base import CountryCode
from country_packs.contracts import CountryPack


class SourceRegistryErrorCode(StrEnum):
    """Why a registry call could not be answered (§16)."""

    DUPLICATE_SOURCE = "DUPLICATE_SOURCE"
    UNKNOWN_SOURCE = "UNKNOWN_SOURCE"
    CAPABILITY_NOT_CLAIMED = "CAPABILITY_NOT_CLAIMED"
    # A pack enabled a source that does not serve the pack's own country. Its own
    # code because the symptom is the deceptive one: `sources_for` filters the
    # source out by country, so the binding looks applied and never runs.
    COUNTRY_NOT_SERVED = "COUNTRY_NOT_SERVED"


class SourceRegistryError(Exception):
    """A registry contract was broken — always at composition time, never mid-sweep.

    Loud on purpose: a duplicate key means two adapters answer to one name, and a
    pack expecting a capability the adapter stopped claiming means a country is
    relying on behaviour that no longer exists. Both are bugs to fix before the
    process serves a request, not conditions to degrade around.
    """

    def __init__(self, code: SourceRegistryErrorCode, message: str, *,
                 source_key: str | None = None) -> None:
        self.code = code
        self.source_key = source_key
        subject = f" [{source_key}]" if source_key else ""
        super().__init__(f"{code}{subject}: {message}")


class SourceRegistry:
    """The set of registered sources, and the rules for choosing among them."""

    def __init__(self) -> None:
        self._sources: dict[str, OpportunitySource] = {}
        self._health: dict[str, SourceHealth] = {}

    def register(self, source: OpportunitySource) -> None:
        """Add one source. Its `metadata.source_key` is its identity (§5)."""
        key = source.metadata.source_key
        if key in self._sources:
            raise SourceRegistryError(
                SourceRegistryErrorCode.DUPLICATE_SOURCE,
                "a source with this key is already registered", source_key=key)
        self._sources[key] = source

    def register_all(self, sources: Iterable[OpportunitySource]) -> None:
        for source in sources:
            self.register(source)

    def get(self, source_key: str) -> OpportunitySource:
        try:
            return self._sources[source_key]
        except KeyError as exc:
            raise SourceRegistryError(
                SourceRegistryErrorCode.UNKNOWN_SOURCE,
                "no source is registered under this key", source_key=source_key
            ) from exc

    def metadata_for(self, source_key: str) -> SourceMetadata:
        return self.get(source_key).metadata

    def __contains__(self, source_key: object) -> bool:
        return isinstance(source_key, str) and source_key in self._sources

    def __len__(self) -> int:
        return len(self._sources)

    def __iter__(self) -> Iterator[OpportunitySource]:
        """Every registered source, ordered as `sources_for` would order it."""
        return iter(self._ordered(self._sources.values(), pack=None))

    @property
    def source_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._sources))

    def _priority(self, source: OpportunitySource, pack: CountryPack | None) -> int:
        """The pack's number if it stated one, the adapter's otherwise.

        A country is entitled to reorder its own sweep — Switzerland puts the local
        boards first because that is where its volume is — without changing what
        the adapter says about itself.
        """
        if pack is not None:
            binding = pack.binding_for(source.metadata.source_key)
            if binding is not None and binding.priority is not None:
                return binding.priority
        return source.metadata.priority

    def _ordered(self, sources: Iterable[OpportunitySource],
                 pack: CountryPack | None) -> tuple[OpportunitySource, ...]:
        """Sort by priority, then by key — never by registration order.

        The key tie-break is what makes a sweep reproducible: two sources at the
        same priority must not swap places between runs, or a `limit` that truncates
        the tail truncates a *different* tail each time and the results look flaky
        for no reason.
        """
        return tuple(sorted(sources, key=lambda s: (self._priority(s, pack),
                                                    s.metadata.source_key)))

    def sources_for(
        self,
        *,
        country: CountryCode | None = None,
        required_capabilities: frozenset[SourceCapability] = frozenset(),
        source_keys: tuple[str, ...] = (),
        pack: CountryPack | None = None,
        include_disabled: bool = False,
    ) -> tuple[OpportunitySource, ...]:
        """The sources eligible for one request, in the order to query them (§8).

        Four independent filters, all of them subtractive:

        - `country` — the adapter must serve it. A source declaring no country
          serves all of them, which is what makes `greenhouse` reusable by every
          pack.
        - `pack` — when given, the country's own bindings decide: a source the pack
          does not bind, or binds with `enabled: false`, is out even if it is
          registered and healthy. This is the hook that lets an operator silence a
          board for one country only.
        - `required_capabilities` — *reliably* supported, not merely claimed. An
          advisory claim (jobup's `LOCATION_SEARCH`, which only reranks) is not an
          answer to "I need this filter applied", and treating it as one is how a
          search silently returns the wrong city.
        - `source_keys` — the caller's allow-list, straight off
          `DiscoveryRequest.source_keys`.

        Returns an empty tuple rather than raising when nothing matches: an empty
        source set is a legitimate, reportable outcome (§18), and the orchestrator
        turns it into a `NO_SOURCE_SELECTED` warning.
        """
        selected: list[OpportunitySource] = []
        for source in self._sources.values():
            metadata = source.metadata
            if not include_disabled and not metadata.enabled:
                continue
            if country is not None and not metadata.serves(country):
                continue
            if source_keys and metadata.source_key not in source_keys:
                continue
            if pack is not None and not pack.allows_source(metadata.source_key):
                continue
            if any(not metadata.supports_reliably(capability)
                   for capability in required_capabilities):
                continue
            selected.append(source)
        return self._ordered(selected, pack)

    def record_health(self, health: SourceHealth) -> None:
        """Remember the last thing a source said about itself.

        Kept here so a caller can ask "what is the state of discovery?" without
        re-running a sweep, and so §12's health report survives the request that
        produced it. Last-write-wins, in-process, deliberately not persisted:
        Phase 5 adds no table, and a stale row would be worse than no row.
        """
        self._health[health.source_key] = health

    def health_for(self, source_key: str) -> SourceHealth | None:
        return self._health.get(source_key)

    def health(self) -> tuple[SourceHealth, ...]:
        """Every health record held, in the same deterministic key order."""
        return tuple(self._health[key] for key in sorted(self._health))

    def unusable_source_keys(self) -> tuple[str, ...]:
        """Registered sources whose last word was not usable, for a status page."""
        return tuple(key for key in sorted(self._health)
                     if not self._health[key].is_usable)
