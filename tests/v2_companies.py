# tests/v2_companies.py
"""Shared constructors and fakes for the Phase 6 company tests.

The discovery half exists so a provider test needs no socket and no YAML file.
`CompanyDiscoveryProvider` is a Protocol with three members, which is what makes
that cheap: `FakeCompanyProvider` is thirty lines and neither the registry nor the
orchestrator can tell it from the configured-ATS provider — §18's "the orchestrator
names no provider" exercised rather than asserted.

The persistence half — `an_alias`, `a_career_site`, `a_discovery_record` — builds
domain objects the storage and service suites both write. Each derives its id from
the key the schema is unique on, because that derivation is what makes §23's
repeated pass an update; a builder handing out a fresh uuid4 would let an
idempotence test pass while the second sighting inserted a second row.

Kept apart from `tests/v2_discovery.py` for the reason §6 keeps the two protocols
apart: a company provider answers "which employers exist?" and a source answers
"what is open right now?". A shared builder would have to take a parameter naming
which of the two it was building, which is the overloaded abstraction the phase
order refuses.

`NOW` is imported rather than redefined so a stored posting, a discovered company and
a health record all carry the same instant — several Phase 6 tests assert across two
of those at once.
"""
from collections.abc import Awaitable, Sequence
from datetime import datetime
from typing import NamedTuple

from backend.app.companies.contracts import (
    CompanyDiscoveryCapability,
    CompanyDiscoveryRequest,
    CompanyDiscoveryResult,
    CompanyProviderMetadata,
    CompanyProviderType,
    CompanySeed,
    DiscoveredCompany,
    ProviderFailureCode,
    ProviderHealth,
    ProviderHealthStatus,
)
from backend.app.companies.providers.base import Clock
from backend.app.domain.company import (
    CareerSite,
    CompanyAlias,
    CompanyDiscoveryRecord,
    CompanySeedKind,
    DetectionStatus,
    normalize_company_name,
)
from backend.app.domain.identifiers import (
    CompanyId,
    career_site_id,
    company_alias_id,
    company_discovery_record_id,
)
from backend.app.domain.opportunity import Opportunity
from tests.v2_builders import COMPANY, NOW


def frozen_clock(instant: datetime = NOW) -> Clock:
    """A `Clock` that never moves, so a result's timestamps are assertable."""
    return lambda: instant


def an_alias(company_id: CompanyId = COMPANY, alias: str = "LOGITECH",
             **overrides) -> CompanyAlias:
    """One recorded label, keyed the way the schema keys it.

    The id is derived rather than passed, because that derivation *is* the
    idempotence §23 asks for: the same label sighted twice is one row, and a builder
    handing out a fresh uuid4 would make every rediscovery test pass by accident.
    """
    fields = {
        "id": company_alias_id(company_id, normalize_company_name(alias)),
        "company_id": company_id,
        "alias": alias,
        "source_key": "test_provider",
        "first_seen_at": NOW,
        "last_seen_at": NOW,
    }
    fields.update(overrides)
    return CompanyAlias(**fields)


def a_career_site(company_id: CompanyId = COMPANY,
                  url: str = "https://example.test/jobs", **overrides) -> CareerSite:
    """One careers endpoint, keyed on `(company_id, url)` like its row.

    `last_checked_at` is left unset, as everything Phase 6 writes is: nothing in this
    phase fetches a URL, so a timestamp here would claim a check nobody performed.
    """
    fields = {
        "id": career_site_id(company_id, url),
        "company_id": company_id,
        "url": url,
        "source_key": "test_provider",
        "discovered_at": NOW,
    }
    fields.update(overrides)
    return CareerSite(**fields)


def a_discovery_record(provider_key: str = "test_provider",
                       external_id: str = "fixture sa",
                       **overrides) -> CompanyDiscoveryRecord:
    """One sighting. `company_id` is unset, which is a state §14 makes normal.

    An ambiguous seed stays recorded and unlinked, so the builder's default is the
    unlinked shape and a test that wants a link says so.
    """
    fields = {
        "id": company_discovery_record_id(provider_key, external_id),
        "provider_key": provider_key,
        "external_id": external_id,
        "seed_kind": CompanySeedKind.CONFIGURED,
        "company_name": "Fixture SA",
        "discovered_at": NOW,
    }
    fields.update(overrides)
    return CompanyDiscoveryRecord(**fields)


def a_provider_metadata(provider_key: str = "fake_provider",
                        **overrides) -> CompanyProviderMetadata:
    """Metadata for a provider that claims a healthcheck and nothing else.

    Minimal on purpose, exactly as `v2_discovery.a_metadata` is: every test that
    cares about a capability passes it, so a test asserting on `COUNTRY_FILTER`
    reads as being about `COUNTRY_FILTER`.
    """
    fields = {
        "provider_key": provider_key,
        "display_name": f"{provider_key} (fake)",
        "provider_type": CompanyProviderType.CONFIGURATION,
        "capabilities": frozenset({CompanyDiscoveryCapability.HEALTHCHECK}),
    }
    fields.update(overrides)
    return CompanyProviderMetadata(**fields)


def a_seed(name: str = "Fixture SA", **overrides) -> CompanySeed:
    """A configured seed, keyed on its normalized name like a real one."""
    fields = {
        "kind": CompanySeedKind.CONFIGURED,
        "name": name,
        "external_id": normalize_company_name(name),
    }
    fields.update(overrides)
    return CompanySeed(**fields)


def a_discovered_company(name: str = "Fixture SA", **overrides) -> DiscoveredCompany:
    """One employer a provider is reporting, with only what it can honestly say.

    Everything optional is left unset: `DiscoveredCompany` treats `None` as "this
    provider does not know", and a builder that filled in a website would make the
    additive-update tests pass for the wrong reason.
    """
    fields = {"seed": a_seed(name), "confidence": DetectionStatus.LIKELY}
    fields.update(overrides)
    return DiscoveredCompany(**fields)


def a_provider_health(provider_key: str = "fake_provider",
                      status: ProviderHealthStatus = ProviderHealthStatus.HEALTHY,
                      **overrides) -> ProviderHealth:
    fields = {"source_key": provider_key, "status": status, "checked_at": NOW}
    if status is not ProviderHealthStatus.HEALTHY:
        fields["reason"] = ProviderFailureCode.SOURCE_UNAVAILABLE
        fields["detail"] = "the source did not answer"
    fields.update(overrides)
    return ProviderHealth(**fields)


def a_provider_result(provider_key: str = "fake_provider",
                      *companies: DiscoveredCompany,
                      **overrides) -> CompanyDiscoveryResult:
    """A result whose health agrees with its provider key, as the model requires."""
    fields = {
        "provider_key": provider_key,
        "companies": companies,
        "health": a_provider_health(provider_key),
        "completed_at": NOW,
    }
    fields.update(overrides)
    return CompanyDiscoveryResult(**fields)


class FakeCompanyProvider:
    """A `CompanyDiscoveryProvider` that answers from a script instead of a source.

    Three behaviours, which are the three the orchestrator has to tell apart: it
    returns a result, it raises (the contract violation §26 says must be contained),
    or it blocks until released (how a concurrency bound is observed without
    sleeping). `requests` records what it was asked, which is how a request's
    `country` and `limit` are checked as far as the provider rather than assumed.
    """

    def __init__(self, metadata: CompanyProviderMetadata | None = None, *,
                 result: CompanyDiscoveryResult | None = None,
                 error: BaseException | None = None,
                 health: ProviderHealth | None = None,
                 on_discover=None) -> None:
        self._metadata = metadata if metadata is not None else a_provider_metadata()
        self._result = result
        self._error = error
        self._health = health
        self._on_discover = on_discover
        self.requests: list[CompanyDiscoveryRequest] = []
        self.healthchecks = 0

    @property
    def metadata(self) -> CompanyProviderMetadata:
        return self._metadata

    async def discover(
        self, request: CompanyDiscoveryRequest) -> CompanyDiscoveryResult:
        self.requests.append(request)
        if self._on_discover is not None:
            await self._on_discover(self)
        if self._error is not None:
            raise self._error
        if self._result is not None:
            return self._result
        return a_provider_result(self._metadata.provider_key)

    async def healthcheck(self) -> ProviderHealth:
        self.healthchecks += 1
        if self._error is not None:
            raise self._error
        return self._health if self._health is not None \
            else a_provider_health(self._metadata.provider_key)


class Board(NamedTuple):
    """One line of `config/companies.yaml`, without the file.

    Structurally a `CompanyBoard` — the Protocol wants a `token` and a `company` —
    which is what lets the configured-ATS provider be tested against two employers
    instead of against a fixture on disk.
    """

    token: str
    company: str


class RecordingLister:
    """An `OpportunityLister` over a list, remembering what it was asked for.

    The recorded limit is the assertion `POSTINGS_PER_PASS` needs: the provider reads
    a fixed number of postings rather than the request's company `limit`, and nothing
    else in the report would show the difference.
    """

    def __init__(self, *postings: Opportunity,
                 error: BaseException | None = None) -> None:
        self._postings = postings
        self._error = error
        self.limits: list[int] = []

    def __call__(self, limit: int) -> Awaitable[Sequence[Opportunity]]:
        self.limits.append(limit)
        return self._answer()

    async def _answer(self) -> Sequence[Opportunity]:
        if self._error is not None:
            raise self._error
        return self._postings
