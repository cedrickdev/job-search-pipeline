"""Employers named by postings already stored, read as company seeds (§7, §27).

The seed kind §7 calls "opportunity-derived", and the provider that makes §27's
example work: `Opportunity(company_name="Logitech")` becomes a claim about an
employer, which resolution turns into a `company_id` while the posting's own string
stays exactly as the board wrote it. Nothing in this module returns an `Opportunity`,
so there is no code path here that could rewrite one.

**It reads a callable, not a repository.** `OpportunityLister` is one function —
"give me the most recent postings" — bound by `bootstrap` to
`OpportunityRepository.list_recent`. A provider holding a repository would be one
`upsert` away from writing, which §17 puts in an application service; a provider
holding a session would be worse. The narrow callable makes the read the only
operation available.

**No network.** The opportunities are already in the database, put there by Phase 5.

What it can honestly report is thinner than the configured-ATS provider's answer, and
deliberately so: a posting gives an employer name and sometimes an application URL.
The application URL is where an ATS detection can come from — `jobs.lever.co/acme/…`
in an `application_url` is Lever's own address for Acme's board — and that is the only
enrichment done here. A location is reported when the posting had one, textual, with
no geocoding (§22).
"""
from collections.abc import Awaitable, Callable, Sequence

from backend.app.companies.ats import detect_from_urls
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
from backend.app.domain.company import (
    CompanySeedKind,
    DetectionStatus,
    normalize_company_name,
)
from backend.app.domain.opportunity import Opportunity

PROVIDER_KEY = "stored_opportunities"

# "The postings to consider, at most this many." One function rather than a Protocol
# with one method, because that is all it is; `bootstrap` binds it to
# `OpportunityRepository.list_recent`, a test binds it to a list.
OpportunityLister = Callable[[int], Awaitable[Sequence[Opportunity]]]

# How many postings to read per pass, independently of how many companies come out of
# them: hundreds of postings routinely name a few dozen employers, so reading only
# `limit` postings would return far fewer companies than the caller asked for. Bounded
# all the same — `list_recent` is capped and an unbounded scan of the postings table
# is the outage `DEFAULT_LIMIT` exists to prevent.
POSTINGS_PER_PASS = 500


def metadata() -> CompanyProviderMetadata:
    """What this provider claims. Priority 20, after the configured employers.

    `COUNTRY_FILTER` *is* claimed, unlike the configured-ATS provider: a posting
    carries `location.country`, so restricting to `CH` is something this provider can
    genuinely do rather than a filter it would have to fake.

    `LOCATION_DISCOVERY` too, for the same reason and with §22's limit: the location
    is the posting's own text, passed through, never geocoded.
    """
    return CompanyProviderMetadata(
        provider_key=PROVIDER_KEY,
        display_name="Employers named by stored opportunities",
        provider_type=CompanyProviderType.STORED_OPPORTUNITIES,
        capabilities=frozenset({
            CompanyDiscoveryCapability.COUNTRY_FILTER,
            CompanyDiscoveryCapability.ATS_DETECTION,
            CompanyDiscoveryCapability.CAREER_SITE_DISCOVERY,
            CompanyDiscoveryCapability.LOCATION_DISCOVERY,
            CompanyDiscoveryCapability.HEALTHCHECK,
        }),
        priority=20,
        notes="Reads opportunities already stored by discovery. Makes no request and "
              "never modifies a posting.")


class StoredOpportunityCompanyProvider(LocalCompanyProvider):
    """The employers the postings table already knows about."""

    def __init__(self, *, opportunities: OpportunityLister,
                 postings_per_pass: int = POSTINGS_PER_PASS,
                 clock: Clock = utc_now) -> None:
        if postings_per_pass < 1:
            raise ValueError("postings_per_pass must be at least 1")
        super().__init__(metadata=metadata(), clock=clock)
        self._opportunities = opportunities
        self._postings_per_pass = postings_per_pass

    async def _collect(self, request: CompanyDiscoveryRequest) -> SeedBatch:
        """Override rather than `_seeds`: this is the one provider that awaits.

        The base class's `_seeds` is synchronous because a file read is, and adding
        `async` there would suggest a request the other two providers never make. This
        provider reads storage, so it takes the async seam and does its own grouping
        in `_batch`, which stays a pure function of the postings it is given — the
        part worth testing without a repository.
        """
        postings = await self._opportunities(self._postings_per_pass)
        return self._batch(postings, request)

    def _batch(self, postings: Sequence[Opportunity],
               request: CompanyDiscoveryRequest) -> SeedBatch:
        """Group postings by employer, keeping the first spelling seen.

        Grouped on the *normalized* name, so `Logitech` and `LOGITECH` in two postings
        are one claim; presented under the first spelling encountered, so the label an
        operator sees is one a board actually published. §4's rule that another
        source's label must not overwrite the canonical name is why the alternatives
        are carried as `aliases` rather than replacing anything: resolution decides
        which is canonical, this provider only reports what it saw.

        Deterministic for a stable posting order, which `list_recent` gives
        (`discovered_at`, then id): §23 needs the same pass twice to produce the same
        companies in the same order.
        """
        grouped: dict[str, list[Opportunity]] = {}
        skipped: list[str] = []
        for posting in postings:
            if request.country is not None and (
                    posting.location is None
                    or posting.location.country != request.country):
                continue
            key = normalize_company_name(posting.company_name)
            if not key:
                skipped.append(
                    f"a posting from {posting.source.source_key!r} names an employer "
                    "whose name normalizes to nothing, so it could never be compared "
                    "against a stored company")
                continue
            grouped.setdefault(key, []).append(posting)

        companies = tuple(self._company(postings_for)
                          for postings_for in grouped.values())
        return SeedBatch(
            companies, skipped=skipped,
            empty_detail="no stored opportunity names an employer this provider could "
                         "use; run a discovery sweep first")

    def _company(self, postings: Sequence[Opportunity]) -> DiscoveredCompany:
        """One employer, from every posting that named it.

        The ATS detection comes from the postings' application URLs and is `CONFIRMED`
        when one of them is on a platform's own host — the strong-evidence case §10
        describes. Everything else stays `None`: a posting does not carry its
        employer's website, and inventing one from the application URL's domain would
        assert `jobs.lever.co` is Logitech's site.
        """
        first = postings[0]
        observed_at = self._clock()
        urls = [posting.application_url for posting in postings
                if posting.application_url]
        detection = detect_from_urls(urls, detected_by=PROVIDER_KEY,
                                     observed_at=observed_at)
        aliases = tuple(sorted({
            posting.company_name for posting in postings
            if posting.company_name != first.company_name}))
        locations = tuple({
            posting.location: None for posting in postings
            if posting.location is not None})
        country = next((location.country for location in locations
                        if location.country is not None), None)

        return DiscoveredCompany(
            seed=CompanySeed(
                kind=CompanySeedKind.OPPORTUNITY,
                name=first.company_name,
                # The normalized name, not a posting id: this seed is the *employer*,
                # and keying it on one posting would make a second discovery record
                # appear for every new vacancy the same company publishes (§23).
                external_id=normalize_company_name(first.company_name),
                country=country,
                ats_platform=detection.platform if detection else None,
                ats_organization_id=detection.organization_id if detection else None,
                source_url=first.source.source_url,
                raw={"posting_count": str(len(postings)),
                     "source_keys": ",".join(sorted({
                         posting.source.source_key for posting in postings}))}),
            country=country,
            ats_platform=detection.platform if detection else None,
            ats_organization_id=detection.organization_id if detection else None,
            ats_status=detection.status if detection else None,
            ats_evidence=detection.detected.evidence if detection else (),
            career_sites=((DiscoveredCareerSite(
                url=detection.board_url, kind=detection.site_kind,
                platform=detection.platform,
                verification_status=detection.status),) if detection else ()),
            locations=locations,
            aliases=aliases,
            # `LIKELY` and not `CONFIRMED` even with a confirmed ATS detection: what
            # is confirmed is that *some* employer publishes on that board, and the
            # employer name still came from a posting's free-text field.
            confidence=DetectionStatus.LIKELY)
