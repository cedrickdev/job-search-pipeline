"""The typed contracts a company discovery provider speaks.

`CompanyDiscoveryProvider` at the bottom is what §6 asks for and, just as
importantly, what it asks *not* to be: it is not `OpportunitySource`. The two
answer different questions — "which employers exist?" against "what is open right
now?" — and one protocol covering both would force every implementation to lie
about half of itself. A greenhouse-shaped module may implement both internally;
the contracts stay apart.

What is deliberately reused rather than reinvented: `SourceHealth`,
`SourceHealthStatus`, `SourceFailureCode` and the whole of
`backend.app.discovery.failures`. §26 asks for Phase 5's health semantics and for
normalized, secret-free failure codes; importing them is how that becomes true by
construction instead of by a second implementation that drifts. `ProviderHealth` is
an alias, not a subclass, for the same reason.

A provider **discovers and returns**. It never writes: §17 puts persistence in an
application service, so nothing in this module mentions a repository or a session.
"""
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Protocol, Self, runtime_checkable

from pydantic import Field, model_validator

from backend.app.discovery.contracts import (
    EnvVarName,
    SourceFailureCode,
    SourceHealth,
    SourceHealthStatus,
    SourceKey,
)
from backend.app.domain.base import (
    CountryCode,
    DomainModel,
    HttpUrlStr,
    NonEmptyStr,
    UtcDatetime,
)
from backend.app.domain.common import Location
from backend.app.domain.company import (
    AtsPlatform,
    CareerSiteKind,
    CompanySeedKind,
    DetectionStatus,
    Evidence,
    SpontaneousApplicationSupport,
)

# Phase 5's health vocabulary, under the names a company-discovery caller expects.
# Aliases rather than new models: an operator's dashboard groups source and provider
# health in one table, and two enums with the same members would make that table
# lie about being one thing.
ProviderHealth = SourceHealth
ProviderHealthStatus = SourceHealthStatus
ProviderFailureCode = SourceFailureCode

# A provider key is an identity with the same rules and the same reason as
# `SourceKey`: `company_discovery_records.provider_key` is stored, and a rename
# would orphan every sighting recorded under the old one.
ProviderKey = SourceKey

# How many companies one provider may return for one request. A ceiling, not a
# target: every Phase 6 provider reads a bounded local source (a config file, the
# opportunities already stored), so the limit exists to stop a future provider from
# handing an unbounded list to a service that would then write all of it.
MAX_COMPANIES_PER_PROVIDER = 500


class CompanyDiscoveryCapability(StrEnum):
    """What a company discovery provider can be asked to do (§18).

    Distinct from `SourceCapability`: nothing here is about postings. Kept short
    on the same principle Phase 5 applied — a member is declared when it is the
    vocabulary a request is written in, and `SPONTANEOUS_APPLICATION_SIGNAL` has no
    holder in Phase 6 because detecting an application form means fetching a page,
    which §9 rules out. It is declared so "no provider can answer that" is a typed
    outcome rather than an empty result.
    """

    # The provider can restrict what it returns to one country.
    COUNTRY_FILTER = "COUNTRY_FILTER"
    # It returns an ATS platform and organization identifier.
    ATS_DETECTION = "ATS_DETECTION"
    # It returns at least one careers endpoint.
    CAREER_SITE_DISCOVERY = "CAREER_SITE_DISCOVERY"
    # It returns the employer's own website.
    WEBSITE_DISCOVERY = "WEBSITE_DISCOVERY"
    # It returns at least one physical location, textual (§22: no geocoding).
    LOCATION_DISCOVERY = "LOCATION_DISCOVERY"
    # No holder in Phase 6. See the class docstring.
    SPONTANEOUS_APPLICATION_SIGNAL = "SPONTANEOUS_APPLICATION_SIGNAL"
    # It can be probed without running a discovery pass.
    HEALTHCHECK = "HEALTHCHECK"


class CompanyProviderType(StrEnum):
    """What kind of thing the provider reads — descriptive, never dispatched on.

    The same rule as `SourceType`: a report groups by it and an operator reads it.
    The moment the orchestrator branched on this value, §18's "no hard-coded
    provider list" would be back in a different shape.
    """

    CONFIGURATION = "CONFIGURATION"
    STORED_OPPORTUNITIES = "STORED_OPPORTUNITIES"
    ATS_PLATFORM = "ATS_PLATFORM"
    MANUAL = "MANUAL"


class CompanyProviderMetadata(DomainModel):
    """Who a provider is and what it can do (§18).

    `countries` empty means country-agnostic, following the repo-wide convention
    that an empty collection restricts nothing. Every Phase 6 provider is
    country-agnostic: `config/companies.yaml` says nothing about where an employer
    is, and inventing a country for it would be a fabricated fact.

    `enabled` is the provider's own switch — "this implementation is fit to run".
    Whether a country uses it is the registry's question, and the registry needs
    both to say yes.
    """

    provider_key: ProviderKey
    display_name: NonEmptyStr
    provider_type: CompanyProviderType
    countries: tuple[CountryCode, ...] = ()
    capabilities: frozenset[CompanyDiscoveryCapability] = frozenset()
    enabled: bool = True
    # Lower runs first. Ties break on `provider_key`, so ordering is total.
    priority: Annotated[int, Field(ge=0)] = 100
    requires_credentials: bool = False
    # Variable NAMES, never values — the `EnvVarName` pattern makes a credential
    # physically unable to sit here. No Phase 6 provider needs one (they read a
    # config file and the local database), and the field is declared anyway because
    # `companies.failures` passes it to `redact_secrets`, which is what blanks a
    # value that leaked into an exception message.
    credential_env_vars: tuple[EnvVarName, ...] = ()
    documentation_url: HttpUrlStr | None = None
    notes: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _claims_are_coherent(self) -> Self:
        if len(set(self.countries)) != len(self.countries):
            raise ValueError("countries must not repeat a country")
        if self.requires_credentials and not self.credential_env_vars:
            raise ValueError("a provider that requires credentials must name the "
                             "environment variables that carry them")
        return self

    def supports(self, capability: CompanyDiscoveryCapability) -> bool:
        return capability in self.capabilities

    def serves(self, country: str) -> bool:
        """Whether this provider can answer for a country.

        No declared country means every country, for the same reason a Greenhouse
        board is wherever the company is.
        """
        return not self.countries or country in self.countries


class CompanySeed(DomainModel):
    """A claim that an employer exists, before anything has been resolved (§7).

    Five kinds, and the distinction is provenance rather than behaviour: the
    resolver treats them identically, and the discovery record keeps the kind so
    "how did we learn this employer exists?" survives canonicalization.

    **A seed is not a company.** It carries a label somebody used and whatever
    corroborating fact came with it; whether it becomes a row, joins an existing
    row or stays unresolved is `resolution`'s decision (§13, §14).

    `external_id` is the provider's own stable identifier for this seed and is what
    `company_discovery_record_id` is derived from — so it must be stable across
    runs. A provider with nothing better uses the normalized name, which is stable
    by construction.
    """

    kind: CompanySeedKind
    name: NonEmptyStr
    external_id: NonEmptyStr
    website: HttpUrlStr | None = None
    careers_url: HttpUrlStr | None = None
    country: CountryCode | None = None
    ats_platform: AtsPlatform | None = None
    ats_organization_id: NonEmptyStr | None = None
    source_url: HttpUrlStr | None = None
    raw: Mapping[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _an_ats_seed_names_its_platform(self) -> Self:
        if self.kind is CompanySeedKind.ATS_ORGANIZATION \
                and (self.ats_platform is None or self.ats_organization_id is None):
            raise ValueError(
                "an ATS_ORGANIZATION seed must name the platform and the "
                "organization identifier; without both nothing can fetch the board")
        if self.ats_organization_id is not None and self.ats_platform is None:
            raise ValueError("an organization identifier without a platform is "
                             "unusable: identifiers are only unique per platform")
        return self


class DiscoveredCareerSite(DomainModel):
    """One careers endpoint a provider found, before it has a company to belong to.

    The pre-persistence twin of `domain.company.CareerSite`: same fields minus the
    ids, because a provider does not know which canonical company this will attach
    to — that is what resolution decides.
    """

    url: HttpUrlStr
    kind: CareerSiteKind = CareerSiteKind.CAREERS_PAGE
    platform: AtsPlatform | None = None
    verification_status: DetectionStatus = DetectionStatus.LIKELY

    @model_validator(mode="after")
    def _an_ats_board_names_its_platform(self) -> Self:
        """The same rule `CareerSite` enforces, applied one step earlier.

        Catching it here means a provider that mislabels a board fails in its own
        unit test rather than at the moment the service tries to persist it.
        """
        if self.kind is CareerSiteKind.ATS_BOARD and self.platform is None:
            raise ValueError("an ATS_BOARD career site must name its platform")
        return self


class DiscoveredCompany(DomainModel):
    """One employer a provider is reporting, with everything it can honestly say.

    Every field beyond `name` is optional and every optional field left `None`
    means "this provider does not know", never "there is none". The difference
    matters at the service boundary: a provider that returns no website must not
    cause a stored website to be cleared, and §13's idempotence depends on that
    asymmetry being explicit.

    `locations` are textual (§22 — Phase 7 owns geocoding). A provider that has
    coordinates from its own source may pass them through inside `Location.point`,
    with the provenance the seed already carries; nothing here geocodes anything.

    `spontaneous_application` defaults to `UNKNOWN` and no Phase 6 provider sets
    anything else, because deciding it requires fetching a page. The field exists so
    a provider that legitimately knows — an operator's manual seed — has somewhere
    to say it, and `evidence` is what makes that claim reviewable.
    """

    seed: CompanySeed
    website: HttpUrlStr | None = None
    country: CountryCode | None = None
    ats_platform: AtsPlatform | None = None
    ats_organization_id: NonEmptyStr | None = None
    ats_status: DetectionStatus | None = None
    ats_evidence: tuple[Evidence, ...] = ()
    career_sites: tuple[DiscoveredCareerSite, ...] = ()
    locations: tuple[Location, ...] = ()
    aliases: tuple[NonEmptyStr, ...] = ()
    spontaneous_application: SpontaneousApplicationSupport = (
        SpontaneousApplicationSupport.UNKNOWN)
    spontaneous_application_url: HttpUrlStr | None = None
    spontaneous_application_evidence: tuple[Evidence, ...] = ()
    confidence: DetectionStatus = DetectionStatus.LIKELY

    @model_validator(mode="after")
    def _claims_carry_their_evidence(self) -> Self:
        if self.ats_platform is not None and not self.ats_evidence:
            raise ValueError(
                "a reported ATS platform must carry evidence (§10: detection is not "
                "verification)")
        if self.ats_status is not None and self.ats_platform is None:
            raise ValueError("an ATS status without a platform says nothing")
        decided = self.spontaneous_application \
            is not SpontaneousApplicationSupport.UNKNOWN
        if decided and not self.spontaneous_application_evidence:
            raise ValueError(
                f"{self.spontaneous_application} is a claim about an employer, so it "
                "must carry evidence; use UNKNOWN when the provider has not looked")
        return self

    @property
    def name(self) -> str:
        return self.seed.name


class CompanyDiscoveryRequest(DomainModel):
    """What one company discovery pass is being asked for (§18).

    Deliberately small, and deliberately carrying no seeds: the seeds a run uses
    come from configuration and from what is already stored, not from an HTTP
    caller. §21 keeps companies shared facts, so letting a request inject employers
    would let any authenticated user write to a table everyone reads.

    `country` is both a filter on providers and a hint passed to those that can use
    it. `provider_keys` is the caller's allow-list, for an operator re-running one
    provider after fixing it.
    """

    country: CountryCode | None = None
    provider_keys: tuple[ProviderKey, ...] = ()
    limit: Annotated[int, Field(gt=0, le=MAX_COMPANIES_PER_PROVIDER)] = 100

    @model_validator(mode="after")
    def _provider_keys_do_not_repeat(self) -> Self:
        if len(set(self.provider_keys)) != len(self.provider_keys):
            raise ValueError("provider_keys must not repeat a provider")
        return self

    def allows_provider(self, provider_key: str) -> bool:
        return not self.provider_keys or provider_key in self.provider_keys


class CompanyDiscoveryWarningCode(StrEnum):
    """Why a company discovery answer is narrower than the question.

    The same idea as `DiscoveryWarningCode` and a separate enum for the same reason
    the protocols are separate: `KEYWORD_IGNORED` means nothing here, and
    `NO_PROVIDER_SELECTED` has no counterpart there. Every member is a fact a
    provider or the orchestrator *knows* at the moment it happens.
    """

    # The registry matched nothing. §18's counterpart to `NO_SOURCE_SELECTED`: an
    # empty result must be explicit, not indistinguishable from "no employers exist".
    NO_PROVIDER_SELECTED = "NO_PROVIDER_SELECTED"
    # A provider cannot restrict by country and returned everything it has.
    COUNTRY_FILTER_NOT_SUPPORTED = "COUNTRY_FILTER_NOT_SUPPORTED"
    # More companies were available than `limit` allowed through.
    LIMIT_TRUNCATED = "LIMIT_TRUNCATED"
    # A seed was unusable — no name once normalized, an unreadable configuration
    # line. Dropping it silently is how a provider appears to shrink for no reason.
    SEED_SKIPPED = "SEED_SKIPPED"
    # The provider read a partly broken source: some of its entries are missing.
    PARTIAL_RESULTS = "PARTIAL_RESULTS"
    # Nothing was configured for this provider at all — `config/companies.yaml`
    # ships empty, so this is the *expected* state of a fresh deployment and must
    # not look like a failure.
    NOTHING_CONFIGURED = "NOTHING_CONFIGURED"


class CompanyDiscoveryWarning(DomainModel):
    """One reason a result is narrower or shorter than the request.

    `detail` is written for an operator and never carries an exception string: the
    same secret-leak argument as `SourceHealth.detail`, and the same
    `discovery.failures.redact_secrets` pass covers it.
    """

    code: CompanyDiscoveryWarningCode
    detail: NonEmptyStr
    provider_key: ProviderKey | None = None


class CompanyDiscoveryResult(DomainModel):
    """What one provider returned from one pass.

    `health` is required, not optional: a provider that returned nothing has to say
    whether that is because there is nothing to find or because it could not look.
    Phase 5 learned this the hard way from V1's single boolean, and §26 asks for the
    same semantics here.
    """

    provider_key: ProviderKey
    companies: tuple[DiscoveredCompany, ...] = ()
    warnings: tuple[CompanyDiscoveryWarning, ...] = ()
    health: ProviderHealth
    completed_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _the_result_speaks_for_its_own_provider(self) -> Self:
        if self.health.source_key != self.provider_key:
            raise ValueError(
                f"health reports {self.health.source_key!r} but this is "
                f"{self.provider_key!r}'s result")
        stray = sorted({warning.provider_key for warning in self.warnings
                        if warning.provider_key is not None} - {self.provider_key})
        if stray:
            raise ValueError(f"warnings attributed to another provider: {stray}")
        return self

    @property
    def is_usable(self) -> bool:
        return self.health.is_usable


@runtime_checkable
class CompanyDiscoveryProvider(Protocol):
    """A source of company *existence*, as distinct from company vacancies (§6).

    Three members, matching `OpportunitySource`'s shape so an operator's mental
    model transfers, and nothing more. A provider:

    - **describes itself** through `metadata`, which is what the registry filters
      and orders on;
    - **discovers**, returning a `CompanyDiscoveryResult` and raising nothing a
      caller has to catch for correctness — a failure is `health`, because one
      provider failing must not cancel the others (§26);
    - **can be probed** through `healthcheck`, so a status page does not have to run
      a discovery pass.

    Like `OpportunitySource`, this Protocol has a non-method member, so `isinstance`
    works and `issubclass` raises `TypeError`.
    """

    @property
    def metadata(self) -> CompanyProviderMetadata: ...

    async def discover(
        self, request: CompanyDiscoveryRequest) -> CompanyDiscoveryResult: ...

    async def healthcheck(self) -> ProviderHealth: ...
