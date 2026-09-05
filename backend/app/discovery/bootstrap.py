"""Composition: build the registries once, check them, hand them over.

§8 draws a line and this module is the far side of it. `registry.py` holds no
source list and `orchestrator.py` names no board, so *something* still has to put
the thirteen wrapped V1 adapters into a `SourceRegistry` and Switzerland into a
`CountryPackRegistry`. That something is here, and it is the only module in the
package that imports both `adapters.v1_catalog` and `country_packs.ch`.

Nothing here makes a request. Building an adapter binds a callable and a piece of
metadata; loading a pack reads five YAML files. A process calls `build_discovery()`
once at startup, holds the result, and every sweep after that is data flowing
through objects that already exist.

No process-wide singleton, deliberately. `SourceRegistry` accumulates health as
sweeps run — that is the point of `record_health` — and a cached module-level
instance would make that state invisible to tests and shared between them. A
caller that wants one registry for its lifetime holds the object it built; the CLI
builds one per invocation and throws it away.

**`verify_pack_expectations` is the load-time check §16 asks for.** A pack states
what it relies on (`expects_capabilities`) and an adapter states what it can do;
the day someone corrects a claim downwards — because they read the V1 code and
found the location was only reranking — a country that was relying on it must stop
the process at startup rather than silently stop filtering mid-sweep. Three
conditions, all of them a bug in composition and none of them a discovery outcome:
an enabled binding naming a source nobody registered, an enabled binding naming a
source that does not serve the country, and an expectation the adapter does not
claim.

Checked against `supports` and not `supports_reliably`: an advisory claim is still
a claim, and the CH pack's expectation that `jobup` takes a location is exactly
that case — the adapter says so and warns `CAPABILITY_ADVISORY_ONLY` every sweep,
which is honest. A pack that needs a *reliable* filter asks the registry for one
through `required_capabilities`, where advisory claims do not count.
"""
from collections.abc import Mapping, Sequence
from typing import NamedTuple

from backend.app.discovery.adapters.v1_catalog import build_v1_sources
from backend.app.discovery.adapters.v1_sources import Clock, CompanyBoard, utc_now
from backend.app.discovery.orchestrator import (
    MAX_CONCURRENT_SOURCES,
    DiscoveryOrchestrator,
)
from backend.app.discovery.registry import (
    SourceRegistry,
    SourceRegistryError,
    SourceRegistryErrorCode,
)
from country_packs.ch import pack as ch_pack
from country_packs.contracts import SourceBinding
from country_packs.registry import CountryPackRegistry


def build_country_packs() -> CountryPackRegistry:
    """Every pack this deployment serves. Today: Switzerland, and only Switzerland.

    Adding France is one line here plus a `country_packs/fr/` directory —
    acceptance criterion 8, and the reason this function exists at all rather than
    the registry importing `ch` itself.
    """
    return CountryPackRegistry((ch_pack.load(),))


def build_source_registry(
    *,
    packs: CountryPackRegistry,
    boards: Mapping[str, Sequence[CompanyBoard]] | None = None,
    clock: Clock = utc_now,
) -> SourceRegistry:
    """Every wrapped V1 source, registered under its stable key.

    `packs.get` is what the adapters resolve a country with — a `PackResolver` is
    a callable, not a registry, so an adapter cannot enumerate or mutate the packs
    it reads. `boards` and `clock` pass straight through to `build_v1_sources`,
    which is what lets a test register two fake employers and a frozen clock.
    """
    registry = SourceRegistry()
    registry.register_all(build_v1_sources(packs=packs.get, boards=boards,
                                           clock=clock))
    return registry


def verify_pack_expectations(registry: SourceRegistry,
                             packs: CountryPackRegistry) -> None:
    """Fail at startup if a country relies on something no adapter provides (§16).

    Only *enabled* bindings are checked. A disabled binding is allowed to name a
    source that does not exist yet — that is how an operator parks a board they
    intend to add, and refusing it would make the pack harder to write than the
    code it configures.
    """
    for pack in packs.packs:
        for binding in pack.enabled_bindings:
            _verify_binding(registry, pack.country, binding)


def _verify_binding(registry: SourceRegistry, country: str,
                    binding: SourceBinding) -> None:
    """One enabled binding against the registry. Raises, or says nothing at all."""
    if binding.source_key not in registry:
        raise SourceRegistryError(
            SourceRegistryErrorCode.UNKNOWN_SOURCE,
            f"the {country} pack enables a source nobody registered; either "
            "register an adapter for this key or set `enabled: false`",
            source_key=binding.source_key)

    metadata = registry.metadata_for(binding.source_key)
    if not metadata.serves(country):
        raise SourceRegistryError(
            SourceRegistryErrorCode.COUNTRY_NOT_SERVED,
            f"the {country} pack enables a source that serves only "
            f"{', '.join(metadata.countries)}; a sweep would filter it out and the "
            "binding would look like it had taken effect",
            source_key=binding.source_key)

    missing = sorted(capability for capability in binding.expects_capabilities
                     if not metadata.supports(capability))
    if missing:
        raise SourceRegistryError(
            SourceRegistryErrorCode.CAPABILITY_NOT_CLAIMED,
            f"the {country} pack expects {', '.join(missing)} from this source, "
            "which does not claim it; either the pack is asking for behaviour that "
            "was never implemented or a claim was corrected downwards",
            source_key=binding.source_key)


class Discovery(NamedTuple):
    """The three objects a process holds for its lifetime.

    Returned together because they are one composition: the orchestrator holds the
    other two privately, so a caller that also needs to answer "which sources
    exist?" or "what does Switzerland call an apprenticeship?" would otherwise have
    to build a second, divergent set.
    """

    packs: CountryPackRegistry
    registry: SourceRegistry
    orchestrator: DiscoveryOrchestrator


def build_discovery(
    *,
    boards: Mapping[str, Sequence[CompanyBoard]] | None = None,
    clock: Clock = utc_now,
    max_concurrency: int = MAX_CONCURRENT_SOURCES,
) -> Discovery:
    """A checked, ready composition. The one call a process makes at startup.

    The order matters: packs, then sources, then *verify*, and only then the
    orchestrator. A registry that failed its own pack's expectations must never
    reach a caller, whose only remaining way to find out would be to run a sweep
    and read the results.
    """
    packs = build_country_packs()
    registry = build_source_registry(packs=packs, boards=boards, clock=clock)
    verify_pack_expectations(registry, packs)
    return Discovery(
        packs=packs,
        registry=registry,
        orchestrator=DiscoveryOrchestrator(registry=registry, packs=packs,
                                           clock=clock,
                                           max_concurrency=max_concurrency))
