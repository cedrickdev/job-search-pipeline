"""Composition: build the company provider registry once, hand it over.

The only module in `backend.app.companies` that names a provider, and the reason
`registry.py` and `orchestrator.py` can be read end to end without finding one (§18,
acceptance criterion 9). Adding a fourth provider is a line here.

Nothing here makes a request. Building a provider binds a callable and a piece of
metadata; the configured-ATS provider does not even read its file until a pass runs.

No process-wide singleton, for the same reason `discovery.bootstrap` refuses one:
`CompanyProviderRegistry` accumulates health as passes run — that is what
`record_health` is for — and a cached module-level instance would make that state
shared between tests and invisible in production.

`build_companies` takes the two things a deployment differs on: where the ATS boards
come from (V1's file by default) and how to list stored opportunities. The second is
`None` by default and the provider is then simply not registered — a CLI that wants
company discovery without a database gets the other two, and the orchestrator reports
which providers ran, so the difference is visible rather than silent.
"""
from collections.abc import Sequence
from typing import NamedTuple

from backend.app.companies.orchestrator import (
    MAX_CONCURRENT_PROVIDERS,
    CompanyDiscoveryOrchestrator,
)
from backend.app.companies.providers.base import Clock, utc_now
from backend.app.companies.providers.configured_ats import (
    BoardMap,
    ConfiguredAtsCompanyProvider,
)
from backend.app.companies.providers.manual_seed import (
    ManualCompanySeed,
    ManualSeedCompanyProvider,
)
from backend.app.companies.providers.stored_opportunities import (
    POSTINGS_PER_PASS,
    OpportunityLister,
    StoredOpportunityCompanyProvider,
)
from backend.app.companies.registry import CompanyProviderRegistry


def build_company_provider_registry(
    *,
    boards: BoardMap | None = None,
    opportunities: OpportunityLister | None = None,
    manual_seeds: Sequence[ManualCompanySeed] = (),
    postings_per_pass: int = POSTINGS_PER_PASS,
    clock: Clock = utc_now,
) -> CompanyProviderRegistry:
    """Every company discovery provider this deployment has, under its stable key.

    `manual_seeds` is empty by default and the provider is registered anyway: an
    operator's list being empty is a state the provider reports as
    `NOTHING_CONFIGURED`, and a provider that vanished when its source was empty would
    make "nothing configured" indistinguishable from "not deployed" on a status page.

    `opportunities` is different — a missing lister is not an empty source, it is the
    absence of a database — so that provider is registered only when there is
    something for it to read.
    """
    registry = CompanyProviderRegistry()
    registry.register(ManualSeedCompanyProvider(seeds=manual_seeds, clock=clock))
    registry.register(ConfiguredAtsCompanyProvider(boards=boards, clock=clock))
    if opportunities is not None:
        registry.register(StoredOpportunityCompanyProvider(
            opportunities=opportunities, postings_per_pass=postings_per_pass,
            clock=clock))
    return registry


class CompanyDiscovery(NamedTuple):
    """The two objects a process holds for its lifetime.

    Returned together because the orchestrator holds the registry privately: a caller
    that also needs to answer "which providers exist?" or "what did each last say
    about itself?" would otherwise have to build a second, divergent one.
    """

    registry: CompanyProviderRegistry
    orchestrator: CompanyDiscoveryOrchestrator


def build_company_discovery(
    *,
    boards: BoardMap | None = None,
    opportunities: OpportunityLister | None = None,
    manual_seeds: Sequence[ManualCompanySeed] = (),
    postings_per_pass: int = POSTINGS_PER_PASS,
    clock: Clock = utc_now,
    max_concurrency: int = MAX_CONCURRENT_PROVIDERS,
) -> CompanyDiscovery:
    """A ready composition. The one call a process makes at startup.

    No counterpart to `discovery.bootstrap.verify_pack_expectations`, and the reason
    is §19: a country pack configures which *sources* a country sweeps, and it says
    nothing about company providers — a provider is eligible when it serves the
    country, which `CompanyProviderMetadata.serves` decides on its own. What the pack
    does contribute to Phase 6 is legal suffixes and domain preferences, read by
    `identity` and `resolution` at comparison time, so there is no composition-time
    claim to check here.
    """
    registry = build_company_provider_registry(
        boards=boards, opportunities=opportunities, manual_seeds=manual_seeds,
        postings_per_pass=postings_per_pass, clock=clock)
    return CompanyDiscovery(
        registry=registry,
        orchestrator=CompanyDiscoveryOrchestrator(
            registry=registry, clock=clock, max_concurrency=max_concurrency))
