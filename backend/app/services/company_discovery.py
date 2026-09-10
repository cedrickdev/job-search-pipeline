"""Company discovery: the only place in Phase 6 that decides what gets persisted.

§17 draws the line this module implements. A provider *discovers* — it reads a
configuration file or the stored postings and returns claims. Nothing in
`backend.app.companies` holds a repository or a session. What a claim becomes is
decided here, and the decision is made in one order for every provider:

1. **Has this exact sighting been seen before?** `(provider_key, external_id)` is
   the provider's own identity for the employer, and `company_discovery_records`
   already answers it. A second pass over an unchanged source therefore performs no
   resolution at all and writes nothing new (§23) — and that lookup is also why
   Phase 6 needs no `company_external_ids` table: the sighting *is* the external
   identity.
2. **Otherwise, resolve against what is stored.** `identity_of_claim` derives the
   comparable form, the repository narrows the table to a shortlist on indexed
   evidence, and `resolution.resolve` decides. `MATCHED` enriches, `UNRESOLVED`
   creates, and `AMBIGUOUS` records the sighting unlinked (§14) rather than picking
   one of two employers that already look like duplicates.
3. **Enrichment is additive.** A provider that does not know a company's website
   must never clear the one another provider found, so every update here is
   "fill what is empty, promote what got stronger" and never "replace with what I
   have". That asymmetry is what makes running two providers in either order produce
   the same company (§13, §23).

**The canonical name is never overwritten** (§4). Another source's spelling becomes a
`CompanyAlias` with that source as its provenance; `Company.name` changes in no code
path here. Neither does `Opportunity.company_name`: the posting-link pass writes
`company_id` through `link_company`, which takes no name argument at all (§13).

**No LLM**, by requirement (§25) and by construction — the only comparison reachable
from here is `identity.compare`, which is exact equality of derived keys.
"""
from datetime import datetime
from enum import StrEnum
from typing import Final

from backend.app.companies.ats import detect_from_url
from backend.app.companies.contracts import (
    CompanyDiscoveryRequest,
    DiscoveredCompany,
    ProviderHealth,
    ProviderKey,
)
from backend.app.companies.identity import CompanyIdentity, identity_of, identity_of_claim
from backend.app.companies.orchestrator import (
    CompanyDiscoveryOrchestrator,
    CompanyDiscoveryReport,
)
from backend.app.companies.providers import stored_opportunities
from backend.app.companies.resolution import (
    CompanyResolution,
    ResolutionOutcome,
    has_corroboration,
    name_lookup_keys,
    resolve,
    stored_company_identity_pairs,
)
from backend.app.domain.base import CountryCode, DomainModel, NonEmptyStr
from backend.app.domain.common import Location
from backend.app.domain.company import (
    CareerSite,
    CareerSiteKind,
    Company,
    CompanyAlias,
    CompanyDiscoveryRecord,
    CompanyIdentityStatus,
    CompanyLocation,
    DetectedATS,
    DetectionStatus,
    SpontaneousApplicationChannel,
    SpontaneousApplicationSupport,
    normalize_company_name,
)
from backend.app.domain.identifiers import (
    CompanyId,
    career_site_id,
    company_alias_id,
    company_discovery_record_id,
    new_company_id,
    new_company_location_id,
)
from backend.app.domain.opportunity import Opportunity
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    CareerSiteRepository,
    CompanyDiscoveryRepository,
    CompanyRepository,
    OpportunityRepository,
)
from country_packs.contracts import CountryPack
from country_packs.registry import CountryPackRegistry

# How many stored companies a shortlist may hold. Smaller than `DEFAULT_LIMIT`
# because a shortlist is not a page: it is the set of employers that share indexed
# evidence with one claim, and twenty-five of those already means the name is a word
# rather than an identity. `resolve` reports every one of them as a candidate, so the
# bound is also what keeps an `AMBIGUOUS` answer readable.
MAX_CANDIDATES: Final = 25

# The provider whose sightings the posting-link pass consults, and the only provider
# named anywhere in this module. §18's prohibition is about the orchestrator choosing
# providers, which it still does not; this is the opposite question — which provider's
# derived identity a posting shares — and the answer is the provider that derives its
# identity *from* postings. Injectable, so a deployment that seeds employers from
# postings differently can say so.
OPPORTUNITY_SEED_PROVIDER: Final[str] = stored_opportunities.PROVIDER_KEY


class CompanyIngestionOutcome(StrEnum):
    """What persisting one discovered company concluded.

    `AMBIGUOUS` is a success, not an error: the sighting was recorded and no employer
    was corrupted. `UNUSABLE` is the one case where nothing at all is written — a name
    with no comparison form cannot be stored (`Company` refuses it) and cannot be
    compared, so recording it would only guarantee the same refusal next pass.
    """

    CREATED = "CREATED"
    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    UNUSABLE = "UNUSABLE"


class CompanyIngestion(DomainModel):
    """What became of one `DiscoveredCompany`, and why.

    Returned per claim rather than aggregated, because "the pass created 3 and matched
    12" is not reviewable: an operator looking at an `AMBIGUOUS` outcome needs the
    resolution that produced it, with its candidates. `resolution` is `None` for the
    two paths that never ran one — a sighting already linked to a company, and a name
    that had no comparison form.
    """

    outcome: CompanyIngestionOutcome
    provider_key: ProviderKey
    external_id: NonEmptyStr
    company_name: NonEmptyStr
    company_id: CompanyId | None = None
    resolution: CompanyResolution | None = None
    detail: NonEmptyStr


class OpportunityLinkReport(DomainModel):
    """What one posting-link pass did (§27).

    Counts rather than rows: the pass is bounded and repeatable, so what a caller
    needs is whether it is making progress. The four numbers need not sum to
    `examined` — a posting linked by a concurrent pass between the read and the write
    counts in none of them, which is the honest answer to a race nobody lost.
    """

    examined: int = 0
    linked: int = 0
    ambiguous: int = 0
    unresolved: int = 0

    @property
    def is_exhausted(self) -> bool:
        """Whether the backlog is empty — nothing was left to examine."""
        return self.examined == 0


class CompanyDiscoveryOutcome(DomainModel):
    """One full pass: what the providers said, what was written, what got linked.

    The report is carried whole and not summarized, for the reason
    `CompanyDiscoveryReport` gives itself: a caller that cannot see provider health
    cannot tell "no employer is configured" from "the provider that knows them could
    not read its file", and docs/V2_SPECIFICATION.md §22 lists hiding failed source
    health as a non-goal.
    """

    report: CompanyDiscoveryReport
    ingestions: tuple[CompanyIngestion, ...] = ()
    links: OpportunityLinkReport = OpportunityLinkReport()

    def counted(self, outcome: CompanyIngestionOutcome) -> int:
        return sum(1 for ingestion in self.ingestions
                   if ingestion.outcome is outcome)

    @property
    def created(self) -> int:
        return self.counted(CompanyIngestionOutcome.CREATED)

    @property
    def matched(self) -> int:
        return self.counted(CompanyIngestionOutcome.MATCHED)

    @property
    def ambiguous(self) -> int:
        return self.counted(CompanyIngestionOutcome.AMBIGUOUS)

    @property
    def company_ids(self) -> tuple[CompanyId, ...]:
        """Every company this pass created or enriched, deduplicated, in pass order.

        Deduplicated because two providers reporting one employer is the expected
        case: the same `company_id` arrives twice with two provenances, and both
        sightings are kept (§5) while the company is one.
        """
        return tuple({ingestion.company_id: None for ingestion in self.ingestions
                      if ingestion.company_id is not None})


class CompanyResolutionService:
    """Claim in, canonical company out — or an honest refusal.

    Holds four repositories and no session: §16 keeps SQLAlchemy out of the
    application layer, and every write below goes through a contract that cannot
    reach another table. Nothing here commits — the request boundary owns the
    transaction — so a pass that fails halfway leaves no half-resolved employer.

    `confirmed_alias_sources` is the set of provenance keys whose aliases count as
    *confirmed* evidence of identity (§2). Empty by default, and deliberately so: a
    job board's spelling is an observation, not a confirmation. Composition passes the
    manual-seed provider, because an operator asserting "LOGITECH is Logitech" is
    exactly the manually confirmed alias §2 lists as a strong signal.
    """

    def __init__(self, companies: CompanyRepository, sites: CareerSiteRepository,
                 records: CompanyDiscoveryRepository,
                 opportunities: OpportunityRepository, *,
                 packs: CountryPackRegistry | None = None,
                 confirmed_alias_sources: frozenset[str] = frozenset(),
                 opportunity_seed_provider: str = OPPORTUNITY_SEED_PROVIDER) -> None:
        self._companies = companies
        self._sites = sites
        self._records = records
        self._opportunities = opportunities
        self._packs = packs
        self._confirmed_alias_sources = confirmed_alias_sources
        self._opportunity_seed_provider = opportunity_seed_provider

    async def ingest(self, discovered: DiscoveredCompany, *, provider_key: str,
                     now: datetime) -> CompanyIngestion:
        """Persist one claim as a canonical company, or record why it could not be.

        The three questions in the module docstring, in order. The sighting lookup
        first is what makes a repeated pass cheap *and* idempotent: it resolves
        nothing, creates nothing and updates only what the provider now knows better.

        A company found through the sighting is still passed through the additive
        update, because the second pass may carry evidence the first did not — a
        `CONFIRMED` ATS detection where there was a `LIKELY` one, a location the
        posting had this time.
        """
        seed = discovered.seed
        if not normalize_company_name(seed.name):
            return CompanyIngestion(
                outcome=CompanyIngestionOutcome.UNUSABLE,
                provider_key=provider_key, external_id=seed.external_id,
                company_name=seed.name,
                detail=f"{seed.name!r} has no comparison form once normalized, so it "
                       "can be neither stored nor compared against a stored employer")

        pack = self._pack(discovered.country or seed.country)
        claim = self._claim(discovered, pack=pack)
        resolution: CompanyResolution | None = None
        company = await self._already_known(provider_key, seed.external_id)

        if company is None:
            resolution = await self._resolve(claim, pack=pack)
            if resolution.outcome is ResolutionOutcome.AMBIGUOUS:
                await self._record(discovered, provider_key=provider_key, company=None,
                                   now=now)
                return CompanyIngestion(
                    outcome=CompanyIngestionOutcome.AMBIGUOUS,
                    provider_key=provider_key, external_id=seed.external_id,
                    company_name=seed.name, resolution=resolution,
                    detail=resolution.detail)
            matched = resolution.company_id
            if matched is not None:
                company = await self._companies.get(matched)

        if company is None:
            company = await self._create(discovered, claim, provider_key=provider_key)
            outcome = CompanyIngestionOutcome.CREATED
            detail = (f"created {company.name!r} as {company.identity_status}: "
                      f"{resolution.detail if resolution else 'a new sighting'}")
        else:
            company = await self._update(company, discovered, claim,
                                         provider_key=provider_key)
            outcome = CompanyIngestionOutcome.MATCHED
            detail = resolution.detail if resolution else (
                f"{seed.external_id!r} was already recorded as {company.name!r}")

        await self._aliases(company, discovered, provider_key=provider_key, now=now)
        await self._career_sites(company, discovered, provider_key=provider_key,
                                 now=now)
        await self._record(discovered, provider_key=provider_key, company=company,
                           now=now)
        return CompanyIngestion(
            outcome=outcome, provider_key=provider_key,
            external_id=seed.external_id, company_name=seed.name,
            company_id=company.id, resolution=resolution, detail=detail)

    async def link_opportunities(self, *,
                                 limit: int = DEFAULT_LIMIT) -> OpportunityLinkReport:
        """Point stored postings at the employers they name (§27).

        §27's example, exactly: `Opportunity(company_name="Logitech")` becomes
        `Opportunity.company_id`, and the string the board published stays untouched —
        `link_company` writes one column and has no name argument.

        Two routes to an employer, tried in that order:

        1. **The sighting that created it.** The posting-derived provider keys its
           seeds on the normalized company name, so a posting and the company its own
           group produced share an exact derived key. This is the case a name
           comparison cannot honestly decide — §2 forbids merging on name similarity —
           and it is not a name comparison: it is a stored assertion that this
           provider's identity *is* that company.
        2. **The evidence the posting itself carries.** Its application URL's ATS
           organization, plus the name forms, resolved the same way a seed is. This is
           what links a posting to a company somebody configured or seeded manually.

        Bounded and oldest-first (`list_unlinked`), so repeated passes work through the
        backlog instead of re-reading one page. A posting whose employer is unknown
        stays unlinked and is counted, never guessed.
        """
        postings = await self._opportunities.list_unlinked(limit=limit)
        linked = ambiguous = unresolved = 0
        for posting in postings:
            company_id, is_ambiguous = await self._company_for(posting)
            if company_id is None:
                if is_ambiguous:
                    ambiguous += 1
                else:
                    unresolved += 1
                continue
            if await self._opportunities.link_company(posting.id, company_id):
                linked += 1
        return OpportunityLinkReport(
            examined=len(postings), linked=linked, ambiguous=ambiguous,
            unresolved=unresolved)

    async def _company_for(self, posting: Opportunity) -> tuple[CompanyId | None, bool]:
        """The employer this posting names, and whether the answer was ambiguous.

        A pair rather than a resolution, because the two callers of the boolean want
        opposite things: the counter wants to distinguish "two candidates" from
        "nothing comparable", and the linker wants neither of them linked.
        """
        key = normalize_company_name(posting.company_name)
        if not key:
            # `list_unlinked` excludes an empty name, but not one made of punctuation.
            return None, False
        sighting = await self._records.get_by_external(
            self._opportunity_seed_provider, key)
        if sighting is not None and sighting.company_id is not None:
            return sighting.company_id, False
        resolution = await self._resolve_posting(posting)
        return resolution.company_id, resolution.outcome is ResolutionOutcome.AMBIGUOUS

    async def _resolve_posting(self, posting: Opportunity) -> CompanyResolution:
        """Resolve a posting's employer from the posting's own evidence.

        The application URL is read for its ATS organization and deliberately **not**
        as domain evidence, which is the same call
        `providers/stored_opportunities.py` makes and for the same reason: inventing an
        employer domain out of `jobs.lever.co` — or out of an aggregator's host —
        would assert that the board is the company. The ATS organization inside that
        URL is a different thing: the platform's own identifier for the employer.
        """
        country = posting.location.country if posting.location else None
        detection = detect_from_url(
            posting.application_url,
            detected_by=self._opportunity_seed_provider) \
            if posting.application_url else None
        pack = self._pack(country)
        claim = identity_of_claim(
            posting.company_name, pack=pack, country=country,
            ats_platform=detection.platform if detection else None,
            ats_organization_id=detection.organization_id if detection else None)
        return await self._resolve(claim, pack=pack)

    async def _already_known(self, provider_key: str,
                             external_id: str) -> Company | None:
        """The company a previous pass attached to this exact sighting, if any.

        `None` covers three cases that all mean "resolve it": never seen, seen and
        left unlinked because it was ambiguous, and seen but pointing at a company
        that has since been deleted (`company_id` is `ON DELETE SET NULL`, so the
        record survives the company — §5 keeps the provenance).
        """
        record = await self._records.get_by_external(provider_key, external_id)
        if record is None or record.company_id is None:
            return None
        return await self._companies.get(record.company_id)

    async def _resolve(self, claim: CompanyIdentity, *,
                       pack: CountryPack | None) -> CompanyResolution:
        """Narrow the table to a shortlist, then let `resolution` decide.

        The query and the decision are kept apart on purpose: the repository does what
        an index can do — exact matches on a name form, a domain, an ATS organization —
        and `resolve` weighs what came back. That is what lets the same rules run
        against a list in a unit test and against PostgreSQL in production.

        Each candidate's identity is derived under *its own* country's pack when there
        is one, falling back to the claim's: legal suffixes are a country's rules, and
        comparing a Swiss `SA` against a German pack's list would drop a suffix
        neither country agreed on.
        """
        candidates = await self._companies.find_candidates(
            name_forms=name_lookup_keys(claim), domain=claim.employer_domain,
            ats_platform=claim.ats_platform,
            ats_organization_id=claim.ats_organization_id, limit=MAX_CANDIDATES)
        companies = tuple(candidate.company for candidate in candidates)
        identities = tuple(
            identity_of(
                candidate.company,
                pack=self._pack(candidate.company.country) or pack,
                aliases=[alias.alias for alias in candidate.aliases],
                confirmed_aliases=[alias.alias for alias in candidate.aliases
                                   if alias.source_key in self._confirmed_alias_sources])
            for candidate in candidates)
        return resolve(claim, stored_company_identity_pairs(companies, identities),
                       pack=pack)

    async def _create(self, discovered: DiscoveredCompany, claim: CompanyIdentity, *,
                      provider_key: str) -> Company:
        """A new canonical employer, at the identity status its evidence justifies.

        `PROVISIONAL` when something outside the name corroborates the claim — a
        domain, an ATS organization — and `SEEDED` when the claim is a name and a
        provenance. `VERIFIED` is unreachable from here by design
        (`CompanyIdentityStatus`): it means a human confirmed it or we followed a
        redirect ourselves, and Phase 6 fetches nothing.
        """
        company_id = new_company_id()
        channel = self._channel(discovered, provider_key=provider_key)
        return await self._companies.upsert(Company(
            id=company_id,
            name=discovered.name,
            website=discovered.website,
            careers_url=self._preferred_careers_url(discovered),
            country=discovered.country or discovered.seed.country,
            identity_status=CompanyIdentityStatus.PROVISIONAL
            if has_corroboration(claim) else CompanyIdentityStatus.SEEDED,
            detected_ats=self._detection(discovered, provider_key=provider_key),
            spontaneous_application_channel=channel,
            locations=self._locations(company_id, discovered.locations),
            accepts_spontaneous_applications=_flag(channel)))

    async def _update(self, company: Company, discovered: DiscoveredCompany,
                      claim: CompanyIdentity, *, provider_key: str) -> Company:
        """Add what this provider knows and the stored row does not. Never subtract.

        Every branch is a fill or a promotion, and there is no branch that replaces a
        value with a different one. That is the asymmetry `DiscoveredCompany` documents
        — `None` means "this provider does not know", never "there is none" — and it is
        what makes two providers commutative: running the ATS one before the postings
        one and the other way round produce the same company.

        Returns the stored company untouched when there is nothing to add, so a
        repeated pass over an unchanged source performs no write at all (§23).
        """
        updates: dict[str, object] = {}
        careers_url = self._preferred_careers_url(discovered)
        country = discovered.country or discovered.seed.country
        if company.website is None and discovered.website is not None:
            updates["website"] = discovered.website
        if company.careers_url is None and careers_url is not None:
            updates["careers_url"] = careers_url
        if company.country is None and country is not None:
            updates["country"] = country

        detection = self._detection(discovered, provider_key=provider_key)
        if detection is not None and _supersedes(company.detected_ats, detection):
            updates["detected_ats"] = detection
        channel = self._channel(discovered, provider_key=provider_key)
        if channel is not None and _answers(company.spontaneous_application_channel,
                                            channel):
            updates["spontaneous_application_channel"] = channel
            updates["accepts_spontaneous_applications"] = _flag(channel)

        locations = self._extra_locations(company, discovered)
        if locations:
            updates["locations"] = (*company.locations, *locations)
        if company.identity_status is CompanyIdentityStatus.SEEDED \
                and has_corroboration(claim):
            updates["identity_status"] = CompanyIdentityStatus.PROVISIONAL

        if not updates:
            return company
        return await self._companies.upsert(company.model_copy(update=updates))

    async def _aliases(self, company: Company, discovered: DiscoveredCompany, *,
                       provider_key: str, now: datetime) -> None:
        """Record every label that is not this company's canonical name (§4).

        The discovered name is one of them: a provider that called the employer
        `LOGITECH` while the stored name is `Logitech Europe S.A.` has contributed an
        alias, not a correction. Writing it here rather than overwriting `Company.name`
        is what §4 asks for, and the alias id is derived from the normalized label so
        meeting the same spelling on every sweep records it once.
        """
        for label in sorted({discovered.name, *discovered.aliases}):
            normalized = normalize_company_name(label)
            if not normalized or normalized == company.normalized_name:
                continue
            await self._companies.upsert_alias(CompanyAlias(
                id=company_alias_id(company.id, normalized),
                company_id=company.id, alias=label, source_key=provider_key,
                first_seen_at=now, last_seen_at=now))

    async def _career_sites(self, company: Company, discovered: DiscoveredCompany, *,
                            provider_key: str, now: datetime) -> None:
        """Persist each careers endpoint under the provider that found it (§11).

        `last_checked_at` is left `None` for everything written here, because nothing
        in Phase 6 fetches a URL (§9): a timestamp would claim a check that never
        happened. The id is derived from the URL, so re-detecting the same board
        updates one row instead of appending a duplicate (§23).
        """
        for site in discovered.career_sites:
            await self._sites.upsert(CareerSite(
                id=career_site_id(company.id, site.url),
                company_id=company.id, url=site.url, kind=site.kind,
                platform=site.platform, source_key=provider_key,
                verification_status=site.verification_status, discovered_at=now))

    async def _record(self, discovered: DiscoveredCompany, *, provider_key: str,
                      company: Company | None, now: datetime) -> None:
        """The provenance row: how this provider came to name this employer (§5).

        Written for an ambiguous seed too, with no `company_id`. Keeping it is what
        stops the next pass from rediscovering and re-refusing the same claim with
        nothing to show for it, and it is the trail §24 needs the day somebody merges
        the duplicates by hand.

        `raw` is the seed's own flat string map. `CompanyDiscoveryRecord` refuses a
        credential-shaped key rather than dropping it, so a provider that ever handed
        us a header bag fails here instead of persisting it (§5, §26).
        """
        seed = discovered.seed
        await self._records.upsert(CompanyDiscoveryRecord(
            id=company_discovery_record_id(provider_key, seed.external_id),
            provider_key=provider_key, external_id=seed.external_id,
            seed_kind=seed.kind, company_id=company.id if company else None,
            company_name=seed.name, source_url=seed.source_url,
            discovered_at=now, confidence=discovered.confidence, raw=dict(seed.raw)))

    def _pack(self, country: str | None) -> CountryPack | None:
        """This country's pack, or `None` — which is a supported state (§19).

        `find` rather than `get`: a company in a country nobody wrote a pack for is
        compared without legal-suffix rules, which is conservative (fewer name forms
        agree) rather than an error. Raising here would make one unpacked employer
        able to fail a whole pass.
        """
        if country is None or self._packs is None:
            return None
        return self._packs.find(country)

    def _claim(self, discovered: DiscoveredCompany, *,
               pack: CountryPack | None) -> CompanyIdentity:
        """The comparable form of what a provider just reported."""
        return identity_of_claim(
            discovered.name, pack=pack, website=discovered.website,
            careers_url=self._preferred_careers_url(discovered),
            country=discovered.country or discovered.seed.country,
            ats_platform=discovered.ats_platform,
            ats_organization_id=discovered.ats_organization_id)

    @staticmethod
    def _preferred_careers_url(discovered: DiscoveredCompany) -> str | None:
        """Which endpoint becomes `Company.careers_url`, the convenience field (§11).

        A corporate careers page first, because that is the one a human opens and the
        one whose host belongs to the employer; then whatever the seed declared; then
        any endpoint at all. The full set is persisted as `CareerSite` records either
        way — this only decides the preferred one.
        """
        for site in discovered.career_sites:
            if site.kind is CareerSiteKind.CAREERS_PAGE:
                return site.url
        if discovered.seed.careers_url is not None:
            return discovered.seed.careers_url
        return discovered.career_sites[0].url if discovered.career_sites else None

    @staticmethod
    def _locations(company_id: CompanyId,
                   locations: tuple[Location, ...]) -> tuple[CompanyLocation, ...]:
        """One row per distinct site, deduplicated by value.

        `is_headquarters` is left `False` on all of them: no Phase 6 provider can tell
        which site is the head office, and `Company` allows at most one — asserting it
        from the order a config file happened to list them in would be a fabricated
        fact.
        """
        return tuple(CompanyLocation(id=new_company_location_id(),
                                     company_id=company_id, location=location)
                     for location in dict.fromkeys(locations))

    def _extra_locations(self, company: Company, discovered: DiscoveredCompany,
                         ) -> tuple[CompanyLocation, ...]:
        """The reported sites this company does not already have.

        Compared by `Location` value and not by id, because ids are random here: a
        second pass would otherwise mint a new id for the same city and the reconciling
        upsert would replace the row every time.
        """
        known = {location.location for location in company.locations}
        return self._locations(company.id, tuple(
            location for location in discovered.locations if location not in known))

    @staticmethod
    def _detection(discovered: DiscoveredCompany, *,
                   provider_key: str) -> DetectedATS | None:
        """The ATS group to store, or `None` when the claim cannot be represented.

        Three claims are dropped rather than stored, and all three are contradictions
        a provider should not have produced: a status with no platform, an `UNKNOWN`
        status (a `DetectedATS` states a platform, so it cannot be ignorant of it), and
        a `CONFIRMED` detection that names no organization. The last one follows
        `companies.ats`'s own rule — a detection nothing can act on is worse than no
        detection, because it looks like progress. The company is still stored; only
        the unusable detection is not.
        """
        platform = discovered.ats_platform
        if platform is None:
            return None
        status = discovered.ats_status or DetectionStatus.LIKELY
        if status is DetectionStatus.UNKNOWN:
            return None
        if status is DetectionStatus.CONFIRMED \
                and discovered.ats_organization_id is None:
            return None
        return DetectedATS(
            platform=platform, organization_id=discovered.ats_organization_id,
            status=status, detected_by=provider_key,
            evidence=discovered.ats_evidence)

    @staticmethod
    def _channel(discovered: DiscoveredCompany, *,
                 provider_key: str) -> SpontaneousApplicationChannel | None:
        """The spontaneous-application answer to store, if there is one (§12).

        `None` when the provider reported `UNKNOWN` with no evidence, which is the
        default and means nobody looked. Storing a channel then would turn "not
        examined" into "examined, undecidable" — the exact confusion §12's tri-state
        exists to prevent.

        A `NOT_SUPPORTED` verdict arriving with a form URL keeps the verdict and drops
        the URL: the two contradict each other, the verdict is the half that carries
        evidence, and `SpontaneousApplicationChannel` refuses the pair outright.
        """
        support = discovered.spontaneous_application
        decided = support is not SpontaneousApplicationSupport.UNKNOWN
        evidence = discovered.spontaneous_application_evidence
        if not decided and not evidence:
            return None
        url = None if support is SpontaneousApplicationSupport.NOT_SUPPORTED \
            else discovered.spontaneous_application_url
        return SpontaneousApplicationChannel(
            support=support, url=url, observed_by=provider_key, evidence=evidence)


class CompanyDiscoveryService:
    """One pass: ask the providers, persist what they said, link the postings.

    The composition §17 describes, and the whole of what an endpoint calls. It holds
    the orchestrator (which knows no provider by name) and the resolution service
    (which is the only writer), and it adds the one thing neither can do alone:
    walking the report and deciding, claim by claim, what the database should hold.

    Ingestion is sequential, not gathered. Two reasons, and the first is not a
    preference: the repositories share one `AsyncSession`, and concurrent statements
    on one session are a programming error in SQLAlchemy. The second is that
    resolution reads what earlier claims wrote — two providers reporting one employer
    must produce one company, and that is only true if the second sees the first.
    """

    def __init__(self, orchestrator: CompanyDiscoveryOrchestrator,
                 resolution: CompanyResolutionService) -> None:
        self._orchestrator = orchestrator
        self._resolution = resolution

    async def run(self, request: CompanyDiscoveryRequest, *, now: datetime,
                  link_limit: int = DEFAULT_LIMIT) -> CompanyDiscoveryOutcome:
        """Discover, persist, then point unlinked postings at their employers.

        The link pass runs here rather than in a second endpoint because it is the
        second half of the same operation: the posting-derived provider turns
        `company_name` into a seed, and §27 asks that the posting end up carrying the
        resolved `company_id`. `link_limit=0` skips it, for a caller that wants
        discovery alone.

        A provider that failed is a fact in the report, never an exception (§26), so
        this method persists what the healthy providers said and reports the rest.
        """
        report = await self._orchestrator.run(request)
        ingestions: list[CompanyIngestion] = []
        for result in report.results:
            for company in result.companies:
                ingestions.append(await self._resolution.ingest(
                    company, provider_key=result.provider_key, now=now))
        links = await self._resolution.link_opportunities(limit=link_limit) \
            if link_limit > 0 else OpportunityLinkReport()
        return CompanyDiscoveryOutcome(
            report=report, ingestions=tuple(ingestions), links=links)

    async def health(self, *, country: CountryCode | None = None,
                     ) -> tuple[ProviderHealth, ...]:
        """Probe every provider without running a pass, for a status page."""
        return await self._orchestrator.healthcheck(country=country)


def _flag(channel: SpontaneousApplicationChannel | None) -> bool | None:
    """The boolean `Company.accepts_spontaneous_applications` must hold beside it.

    Set together with the channel and never independently: `Company` refuses a row
    where the two disagree, and the point of that validator is that a reader may use
    either field and get the same answer.
    """
    if channel is None:
        return None
    return {
        SpontaneousApplicationSupport.SUPPORTED: True,
        SpontaneousApplicationSupport.NOT_SUPPORTED: False,
        SpontaneousApplicationSupport.UNKNOWN: None,
    }[channel.support]


def _supersedes(stored: DetectedATS | None, fresh: DetectedATS) -> bool:
    """Whether a fresh ATS detection should replace the stored one.

    Only ever a promotion of the *same* platform: a `LIKELY` detection becoming
    `CONFIRMED`, or one that gains the organization identifier it was missing. Two
    providers naming two different platforms is left as it stands, because an employer
    has one canonical ATS and a disagreement is a review question — overwriting would
    make the answer depend on which provider ran last (§24: merging stays explicit).
    """
    if stored is None:
        return True
    if stored.platform is not fresh.platform:
        return False
    if stored.organization_id is None and fresh.organization_id is not None:
        return True
    return stored.status is DetectionStatus.LIKELY \
        and fresh.status is DetectionStatus.CONFIRMED


def _answers(stored: SpontaneousApplicationChannel | None,
             fresh: SpontaneousApplicationChannel) -> bool:
    """Whether a fresh spontaneous-application claim should replace the stored one.

    Only when it decides something the stored one did not. A stored `SUPPORTED` is
    never overwritten by another provider's `NOT_SUPPORTED`: both carry evidence, and
    silently preferring the newer one would let a provider that could not find the
    form retract a form somebody else saw (§12).
    """
    return stored is None or (not stored.is_decided and fresh.is_decided)
