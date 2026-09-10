"""Employers an operator asserted directly (§7's manual seed).

The provider that exists so a human can put a company into the system without an
ATS token and without waiting for a posting: "I know Acme hires here, here is their
site". Its seeds come from whatever `bootstrap` was handed — a list built by a CLI, a
list built by a test — and never from an HTTP request body, because §21 keeps
companies shared facts and a request-injected seed would let any authenticated user
write to a table everyone reads.

Two things this provider can say that no other Phase 6 provider can, and both because
a person said them:

- **A website.** `WEBSITE_DISCOVERY` is claimed here alone. A configured ATS line has
  a board, a posting has an application URL; neither has the employer's own domain,
  which is the strongest identity signal `identity.compare` reads.
- **Spontaneous applications.** §12's `SUPPORTED`/`NOT_SUPPORTED` requires evidence,
  and an operator who opened the page and saw the form is evidence. Nothing infers
  it: a seed that says nothing stays `UNKNOWN`, and §12's forbidden inference —
  no active jobs therefore spontaneous applications allowed — has no code path here.

Still no network. A manual seed is read, not verified, so every claim it carries is
`LIKELY` unless the URL itself is a platform's own board address.
"""
from collections.abc import Sequence
from typing import Self

from pydantic import model_validator

from backend.app.companies.ats import AtsDetection, detect_from_urls
from backend.app.companies.contracts import (
    CompanyDiscoveryCapability,
    CompanyDiscoveryRequest,
    CompanyProviderMetadata,
    CompanyProviderType,
    CompanySeed,
    DiscoveredCareerSite,
    DiscoveredCompany,
)
from backend.app.companies.providers.base import (
    Clock,
    LocalCompanyProvider,
    SeedBatch,
    utc_now,
)
from backend.app.domain.base import CountryCode, DomainModel, HttpUrlStr, NonEmptyStr
from backend.app.domain.common import Location
from backend.app.domain.company import (
    CareerSiteKind,
    CompanySeedKind,
    DetectionStatus,
    Evidence,
    SpontaneousApplicationSupport,
    normalize_company_name,
)

PROVIDER_KEY = "manual_seed"

# The evidence code a manual claim carries. One operator assertion, named as such, so
# a reviewer reading `spontaneous_application_evidence` can tell "somebody checked the
# page" from "a crawler matched a pattern".
_EVIDENCE_MANUAL = "MANUAL_OPERATOR_ASSERTION"


class ManualCompanySeed(DomainModel):
    """One employer an operator is asserting, with what they know about it.

    A model rather than a tuple because the validators are the point: a decided
    spontaneous-application verdict has to come with a reason, and an operator handing
    `SUPPORTED` with no note gets an error at composition time instead of an
    unreviewable claim in the database.

    `key` is the seed's stable identity and is what the discovery record is derived
    from, so an operator who fixes a typo in `name` does not create a second record
    (§23). It defaults to the normalized name, which is stable by construction.
    """

    name: NonEmptyStr
    key: NonEmptyStr | None = None
    website: HttpUrlStr | None = None
    careers_url: HttpUrlStr | None = None
    country: CountryCode | None = None
    locations: tuple[Location, ...] = ()
    aliases: tuple[NonEmptyStr, ...] = ()
    spontaneous_application: SpontaneousApplicationSupport = (
        SpontaneousApplicationSupport.UNKNOWN)
    spontaneous_application_url: HttpUrlStr | None = None
    # Why the operator believes what they wrote. Required for a decided verdict, and
    # free text on purpose: it is read by a human reviewing the claim.
    note: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _a_decided_verdict_carries_its_reason(self) -> Self:
        """§12: `SUPPORTED` and `NOT_SUPPORTED` are claims, so they need a reason.

        Refused at composition time rather than sanitized later, because the
        alternative is a stored verdict whose evidence reads "an operator asserted
        that Acme is an employer worth tracking" — which says nothing about the
        application form and is exactly the unreviewable claim §12 forbids.
        """
        if self.spontaneous_application is not SpontaneousApplicationSupport.UNKNOWN \
                and self.note is None:
            raise ValueError(
                f"a {self.spontaneous_application} seed must carry a note saying what "
                "the operator saw; a verdict nobody can review is a guess with a "
                "provenance")
        return self

    @property
    def external_id(self) -> str:
        return self.key or normalize_company_name(self.name)


def metadata() -> CompanyProviderMetadata:
    """What this provider claims. Priority 5 — before everything else.

    An operator's assertion is the one seed a human vouched for, so it should be the
    company other providers resolve against rather than the one that arrives after a
    posting has already created a thinner row. Ordering is not what prevents
    duplicates — resolution is — but it decides which spelling an operator sees first.

    `COUNTRY_FILTER` is claimed: a manual seed may name its country, and a seed that
    does not is simply not returned when a country is requested. That is the honest
    filter, and it is why this provider does not need the
    `COUNTRY_FILTER_NOT_SUPPORTED` warning.
    """
    return CompanyProviderMetadata(
        provider_key=PROVIDER_KEY,
        display_name="Manually seeded employers",
        provider_type=CompanyProviderType.MANUAL,
        capabilities=frozenset({
            CompanyDiscoveryCapability.COUNTRY_FILTER,
            CompanyDiscoveryCapability.WEBSITE_DISCOVERY,
            CompanyDiscoveryCapability.CAREER_SITE_DISCOVERY,
            CompanyDiscoveryCapability.ATS_DETECTION,
            CompanyDiscoveryCapability.LOCATION_DISCOVERY,
            CompanyDiscoveryCapability.SPONTANEOUS_APPLICATION_SIGNAL,
            CompanyDiscoveryCapability.HEALTHCHECK,
        }),
        priority=5,
        notes="Employers asserted by an operator at composition time. Seeds never "
              "come from an HTTP request (§21). Makes no request.")


class ManualSeedCompanyProvider(LocalCompanyProvider):
    """Whatever employers the process was configured with, as companies."""

    def __init__(self, *, seeds: Sequence[ManualCompanySeed] = (),
                 clock: Clock = utc_now) -> None:
        super().__init__(metadata=metadata(), clock=clock)
        self._seeds_configured = tuple(seeds)

    def _seeds(self, request: CompanyDiscoveryRequest) -> SeedBatch:
        """Every configured seed the request allows, in configuration order.

        Configuration order, not sorted: an operator's list has an order they chose,
        and it is stable across passes, which is all §23 asks for.

        A seed whose name normalizes to nothing is skipped and reported. A duplicate
        `external_id` is skipped too, with its own message: two seeds resolving to the
        same discovery record would make the second silently overwrite the first, and
        an operator who pasted a line twice should hear about it.
        """
        companies: list[DiscoveredCompany] = []
        skipped: list[str] = []
        seen: set[str] = set()

        for seed in self._seeds_configured:
            if request.country is not None and seed.country != request.country:
                continue
            if not normalize_company_name(seed.name):
                skipped.append(
                    f"a manual seed named {seed.name!r} normalizes to nothing, so it "
                    "could never be compared against a stored company")
                continue
            if seed.external_id in seen:
                skipped.append(
                    f"two manual seeds share the identity {seed.external_id!r}; only "
                    "the first was used, because the second would overwrite its "
                    "discovery record")
                continue
            seen.add(seed.external_id)
            companies.append(self._company(seed))

        return SeedBatch(
            companies, skipped=skipped,
            empty_detail="no manual company seed is configured for this deployment")

    def _company(self, seed: ManualCompanySeed) -> DiscoveredCompany:
        """One asserted employer, with its claims carrying the operator's note."""
        observed_at = self._clock()
        urls = [url for url in (seed.careers_url, seed.website) if url]
        detection = detect_from_urls(urls, detected_by=PROVIDER_KEY,
                                     observed_at=observed_at)
        career_sites = self._career_sites(seed, detection)
        evidence = (Evidence(
            code=_EVIDENCE_MANUAL,
            detail=seed.note or f"an operator asserted that {seed.name} is an "
                                "employer worth tracking",
            source_url=seed.spontaneous_application_url or seed.careers_url
            or seed.website,
            observed_at=observed_at),)
        decided = seed.spontaneous_application \
            is not SpontaneousApplicationSupport.UNKNOWN

        return DiscoveredCompany(
            seed=CompanySeed(
                kind=CompanySeedKind.MANUAL,
                name=seed.name,
                external_id=seed.external_id,
                website=seed.website,
                careers_url=seed.careers_url,
                country=seed.country,
                ats_platform=detection.platform if detection else None,
                ats_organization_id=detection.organization_id if detection else None,
                source_url=seed.website or seed.careers_url,
                raw={"asserted_by": PROVIDER_KEY}),
            website=seed.website,
            country=seed.country,
            ats_platform=detection.platform if detection else None,
            ats_organization_id=detection.organization_id if detection else None,
            ats_status=detection.status if detection else None,
            ats_evidence=detection.detected.evidence if detection else (),
            career_sites=career_sites,
            locations=seed.locations,
            aliases=seed.aliases,
            spontaneous_application=seed.spontaneous_application,
            spontaneous_application_url=seed.spontaneous_application_url,
            spontaneous_application_evidence=evidence if decided else (),
            confidence=DetectionStatus.LIKELY)

    def _career_sites(self, seed: ManualCompanySeed,
                      detection: AtsDetection | None,
                      ) -> tuple[DiscoveredCareerSite, ...]:
        """The endpoints this seed names, deduplicated by URL.

        Up to three different things — a careers page, the ATS board a URL turned out
        to address, a spontaneous-application form — which is exactly why §11 asks for
        career sites to be rows rather than one `careers_url` column. Deduplicated
        because an operator who pastes the board URL into `careers_url` should get one
        row, not two rows disagreeing about the kind.
        """
        sites: dict[str, DiscoveredCareerSite] = {}
        if seed.careers_url:
            sites[seed.careers_url] = DiscoveredCareerSite(
                url=seed.careers_url, kind=CareerSiteKind.CAREERS_PAGE,
                verification_status=DetectionStatus.LIKELY)
        if detection is not None:
            sites[detection.board_url] = DiscoveredCareerSite(
                url=detection.board_url, kind=detection.site_kind,
                platform=detection.platform,
                verification_status=detection.status)
        if seed.spontaneous_application_url:
            sites[seed.spontaneous_application_url] = DiscoveredCareerSite(
                url=seed.spontaneous_application_url,
                kind=CareerSiteKind.SPONTANEOUS_APPLICATION,
                verification_status=DetectionStatus.LIKELY)
        return tuple(sites.values())
