# tests/test_v2_company_services.py
"""The application services: what a provider's claim actually becomes.

`backend.app.companies` discovers and decides nothing; `backend.app.services`
decides and writes. Everything asserted here is on the writing side, and three
properties carry most of the file:

**A second pass changes nothing** (§23). The sighting lookup short-circuits, the
derived ids collapse onto the rows they already wrote, and an unchanged source
produces an unchanged database. Several tests below run the same ingestion twice
and then count rows, because "no duplicates" is only credible as a count.

**Enrichment adds and never subtracts** (§13). A provider that does not know a
website must not clear one. Every update test therefore runs the *poorer* claim
second: the interesting failure is not "the richer pass wrote nothing", it is "the
poorer pass erased something".

**A doubtful identity stays doubtful** (§14, §24). `AMBIGUOUS` writes the sighting
and no link, `UNRESOLVED` creates, and nothing anywhere merges two stored employers.
A service that never reaches `AMBIGUOUS` corrupts identity silently, so the paths to
it are pinned as carefully as the happy ones.

The repositories are the fakes from `tests/v2_fakes.py`, whose upserts merge the way
the SQL ones do — `tests/test_v2_company_persistence.py` is what proves that claim
against PostgreSQL, and this suite is what exercises the decisions on top of it.
"""
from uuid import UUID

import pytest

from backend.app.companies.contracts import (
    CompanyDiscoveryRequest,
    DiscoveredCareerSite,
)
from backend.app.companies.orchestrator import CompanyDiscoveryOrchestrator
from backend.app.companies.registry import CompanyProviderRegistry
from backend.app.domain.common import Location
from backend.app.domain.company import (
    AtsPlatform,
    CareerSiteKind,
    CompanyIdentityStatus,
    CompanySeedKind,
    DetectedATS,
    DetectionStatus,
    Evidence,
    SpontaneousApplicationSupport,
    normalize_company_name,
)
from backend.app.domain.identifiers import OpportunityId, new_company_id
from backend.app.repositories.contracts import CompanyFilter
from backend.app.services.company_directory import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CompanyDirectoryService,
)
from backend.app.services.company_discovery import (
    OPPORTUNITY_SEED_PROVIDER,
    CompanyDiscoveryService,
    CompanyIngestionOutcome,
    CompanyResolutionService,
)
from tests.v2_builders import (
    COMPANY,
    LATER,
    NOW,
    OPPORTUNITY,
    OTHER_COMPANY,
    OTHER_OPPORTUNITY,
    a_company,
    an_opportunity,
)
from tests.v2_companies import (
    FakeCompanyProvider,
    a_discovered_company,
    a_provider_metadata,
    a_provider_result,
    a_seed,
    frozen_clock,
)
from tests.v2_fakes import (
    FakeCareerSiteRepository,
    FakeCompanyDiscoveryRepository,
    FakeCompanyRepository,
    FakeOpportunityRepository,
)

pytestmark = pytest.mark.asyncio

PROVIDER = "test_provider"
OTHER_PROVIDER = "other_provider"
THIRD_POSTING = OpportunityId(UUID("00000000-0000-4000-8000-000000000024"))


def an_evidence(code: str = "CONFIGURED_BOARD",
                detail: str = "config/companies.yaml names this board") -> Evidence:
    """One observation. Required by every claim a provider is allowed to make."""
    return Evidence(code=code, detail=detail)


def with_ats(name: str = "Acme SA", *, platform: AtsPlatform = AtsPlatform.GREENHOUSE,
             organization_id: str | None = "acme",
             status: DetectionStatus = DetectionStatus.LIKELY, **overrides):
    """A claim that names a board, with the evidence `DiscoveredCompany` demands."""
    return a_discovered_company(
        name, ats_platform=platform, ats_organization_id=organization_id,
        ats_status=status, ats_evidence=(an_evidence(),), **overrides)


def with_spontaneous(support: SpontaneousApplicationSupport, *,
                     url: str | None = None, **overrides):
    """A claim about unsolicited applications, evidence-backed unless `UNKNOWN`."""
    return a_discovered_company(
        "Acme SA", spontaneous_application=support,
        spontaneous_application_url=url,
        spontaneous_application_evidence=(
            an_evidence("SPONTANEOUS_FORM", "the careers page links an open form"),),
        **overrides)


def a_posting(**overrides):
    """An unlinked posting: the state §27's walkthrough starts from."""
    fields = {"company_name": "Logitech", "company_id": None}
    fields.update(overrides)
    return an_opportunity(**fields)


@pytest.fixture
def postings() -> FakeOpportunityRepository:
    return FakeOpportunityRepository()


@pytest.fixture
def companies(postings: FakeOpportunityRepository) -> FakeCompanyRepository:
    # Wired to the postings so `has_opportunities` is the cross-table question it is
    # in SQL; without it §28's "employers with no opportunity" filter is vacuous.
    return FakeCompanyRepository(postings)


@pytest.fixture
def sites() -> FakeCareerSiteRepository:
    return FakeCareerSiteRepository()


@pytest.fixture
def records() -> FakeCompanyDiscoveryRepository:
    return FakeCompanyDiscoveryRepository()


@pytest.fixture
def resolution(companies, sites, records, postings) -> CompanyResolutionService:
    return CompanyResolutionService(companies, sites, records, postings)


@pytest.fixture
def directory(companies, sites, records) -> CompanyDirectoryService:
    return CompanyDirectoryService(companies, sites, records)


def a_pass(resolution: CompanyResolutionService,
           *providers: FakeCompanyProvider) -> CompanyDiscoveryService:
    """The §17 composition: orchestrator plus the only writer, and no provider list.

    Built through the registry exactly as `bootstrap` builds it, which is what makes
    the fake providers indistinguishable from the real three as far as the service is
    concerned.
    """
    registry = CompanyProviderRegistry()
    registry.register_all(providers)
    return CompanyDiscoveryService(
        CompanyDiscoveryOrchestrator(registry=registry, clock=frozen_clock()),
        resolution)


# --- §7, §13: what one claim becomes ------------------------------------------

async def test_a_bare_name_becomes_a_seeded_company(resolution, companies, records):
    """A seed with nothing corroborating it is stored, and stored as `SEEDED`.

    §7's "a seed is not automatically a canonical company" is about *identity*, not
    about persistence: the employer is recorded, and the status says how much the
    record is worth. `PROVISIONAL` here would claim a second signal nobody supplied.
    """
    ingestion = await resolution.ingest(a_discovered_company(), provider_key=PROVIDER,
                                        now=NOW)
    assert ingestion.outcome is CompanyIngestionOutcome.CREATED
    assert ingestion.company_id is not None
    assert "created 'Fixture SA' as SEEDED" in ingestion.detail
    stored = await companies.get(ingestion.company_id)
    assert stored is not None
    assert stored.identity_status is CompanyIdentityStatus.SEEDED
    assert stored.website is None
    sighting = await records.get_by_external(PROVIDER, "fixture sa")
    assert sighting is not None
    assert sighting.company_id == ingestion.company_id
    assert sighting.seed_kind is CompanySeedKind.CONFIGURED
    # The name is the canonical one, so it is not also an alias of itself (§4).
    assert companies.company_aliases == {}


async def test_a_seed_with_a_website_is_created_provisional(resolution, companies):
    """A domain is a signal from outside this codebase, so the status rises once."""
    ingestion = await resolution.ingest(
        a_discovered_company("Logitech", website="https://logitech.com"),
        provider_key=PROVIDER, now=NOW)
    stored = await companies.get(ingestion.company_id)
    assert stored.identity_status is CompanyIdentityStatus.PROVISIONAL
    assert stored.normalized_domain == "logitech.com"


async def test_a_name_with_no_comparison_form_is_refused_and_writes_nothing(
        resolution, companies, records):
    """`UNUSABLE` is the one outcome that persists nothing at all.

    A name of pure punctuation cannot be stored (`Company` refuses it) and cannot be
    compared, so recording the sighting would only guarantee the same refusal next
    sweep, with a row to show for it.
    """
    ingestion = await resolution.ingest(
        a_discovered_company(seed=a_seed("—", external_id="dash")),
        provider_key=PROVIDER, now=NOW)
    assert ingestion.outcome is CompanyIngestionOutcome.UNUSABLE
    assert ingestion.company_id is None
    assert "no comparison form" in ingestion.detail
    assert companies.companies == {}
    assert records.records == {}


async def test_a_shared_name_alone_never_merges_and_leaves_the_sighting_unlinked(
        resolution, companies, records):
    """§2's prohibition, at the layer that would otherwise write the merge.

    One stored employer shares the claim's name form and nothing else. That is a
    `POSSIBLE_MATCH`, which resolves to `AMBIGUOUS`: no link, no second company, and
    a recorded sighting so the next sweep does not rediscover it from scratch (§14).
    """
    await companies.upsert(a_company(locations=()))
    ingestion = await resolution.ingest(a_discovered_company(), provider_key=PROVIDER,
                                        now=NOW)
    assert ingestion.outcome is CompanyIngestionOutcome.AMBIGUOUS
    assert ingestion.company_id is None
    assert "a name is not an identity" in ingestion.detail
    assert ingestion.resolution.candidate_ids == (COMPANY,)
    assert len(companies.companies) == 1
    sighting = await records.get_by_external(PROVIDER, "fixture sa")
    assert sighting is not None and sighting.company_id is None


async def test_two_stored_employers_on_one_domain_are_left_for_a_human(
        resolution, companies):
    """Two strong matches mean the duplicate predates this claim (§24).

    Picking one would bury the problem under a link that looks deliberate. The claim
    is recorded, both candidates are reported, and nothing is written to either row.
    """
    await companies.upsert(a_company(name="Logitech", website="https://logitech.com",
                                     locations=()))
    await companies.upsert(a_company(id=OTHER_COMPANY, name="Logitech Europe",
                                     website="https://logitech.com", locations=()))
    ingestion = await resolution.ingest(
        a_discovered_company("Logitech", website="https://logitech.com"),
        provider_key=PROVIDER, now=NOW)
    assert ingestion.outcome is CompanyIngestionOutcome.AMBIGUOUS
    assert sorted(ingestion.resolution.candidate_ids) == sorted(
        (COMPANY, OTHER_COMPANY))
    assert "already duplicates" in ingestion.detail
    assert len(companies.companies) == 2


async def test_two_providers_on_one_domain_produce_one_company_and_two_provenances(
        resolution, companies, records):
    """The everyday case §5 and §23 are both about.

    A configured board and a stored posting describe one employer under two labels.
    The domain decides, the second pass matches instead of inserting, and both
    sightings survive — losing one would lose the answer to "how did we find this?".
    """
    first = await resolution.ingest(
        a_discovered_company("Logitech", website="https://logitech.com"),
        provider_key=PROVIDER, now=NOW)
    second = await resolution.ingest(
        a_discovered_company("Logitech Europe S.A.",
                             seed=a_seed("Logitech Europe S.A."),
                             website="https://www.logitech.com/fr/"),
        provider_key=OTHER_PROVIDER, now=LATER)
    assert second.outcome is CompanyIngestionOutcome.MATCHED
    assert second.company_id == first.company_id
    assert "EMPLOYER_DOMAIN" in second.detail
    assert len(companies.companies) == 1
    assert len(records.records) == 2
    assert {record.company_id for record in records.records.values()} == {
        first.company_id}


# --- §23: running the same pass twice -----------------------------------------

async def test_the_same_sighting_twice_resolves_nothing_and_writes_no_second_row(
        resolution, companies, records, sites):
    """The short-circuit that makes a repeated sweep cheap as well as idempotent.

    The second pass finds its own sighting, so it runs no resolution at all — which
    is why `resolution` is `None` on that ingestion — and the discovery record keeps
    the instant it was first seen rather than being re-dated.
    """
    first = await resolution.ingest(a_discovered_company(), provider_key=PROVIDER,
                                    now=NOW)
    second = await resolution.ingest(a_discovered_company(), provider_key=PROVIDER,
                                     now=LATER)
    assert second.outcome is CompanyIngestionOutcome.MATCHED
    assert second.company_id == first.company_id
    assert second.resolution is None
    assert second.detail == "'fixture sa' was already recorded as 'Fixture SA'"
    assert len(companies.companies) == 1
    assert len(records.records) == 1
    assert sites.sites == {}
    sighting = await records.get_by_external(PROVIDER, "fixture sa")
    assert sighting.discovered_at == NOW


async def test_an_unchanged_second_pass_leaves_the_company_exactly_as_it_was(
        resolution, companies):
    """`_update` returns the stored row untouched when it has nothing to add."""
    claim = with_ats(website="https://acme.test")
    first = await resolution.ingest(claim, provider_key=PROVIDER, now=NOW)
    before = await companies.get(first.company_id)
    await resolution.ingest(claim, provider_key=PROVIDER, now=LATER)
    assert await companies.get(first.company_id) == before


# --- §13: enrichment is additive ----------------------------------------------

async def test_a_provider_that_knows_less_clears_nothing(resolution, companies, sites):
    """The asymmetry the whole phase rests on: `None` means "I do not know".

    The poorer claim runs second on purpose. A service that treated a missing field
    as an assertion of absence would erase the website and the careers URL here, and
    two providers would then produce a different company depending on their order.
    """
    rich = a_discovered_company(
        "Fixture SA", website="https://fixture.test", country="CH",
        career_sites=(DiscoveredCareerSite(url="https://fixture.test/jobs"),))
    created = await resolution.ingest(rich, provider_key=PROVIDER, now=NOW)
    await resolution.ingest(a_discovered_company(), provider_key=PROVIDER, now=LATER)
    stored = await companies.get(created.company_id)
    assert stored.website == "https://fixture.test"
    assert stored.careers_url == "https://fixture.test/jobs"
    assert stored.country == "CH"
    assert len(sites.sites) == 1


async def test_a_later_pass_fills_what_the_first_did_not_know(resolution, companies):
    """A fill is not a replacement: the empty fields take the new values.

    And the identity status follows the evidence — a company created from a bare name
    becomes `PROVISIONAL` the moment a domain corroborates it, which is the only
    promotion Phase 6 performs automatically.
    """
    created = await resolution.ingest(a_discovered_company(), provider_key=PROVIDER,
                                      now=NOW)
    await resolution.ingest(
        a_discovered_company("Fixture SA", website="https://fixture.test",
                             country="CH"),
        provider_key=PROVIDER, now=LATER)
    stored = await companies.get(created.company_id)
    assert stored.website == "https://fixture.test"
    assert stored.country == "CH"
    assert stored.identity_status is CompanyIdentityStatus.PROVISIONAL


async def test_a_confirmed_detection_promotes_a_likely_one(resolution, companies):
    """A promotion of the same platform is the only ATS overwrite there is (§10)."""
    created = await resolution.ingest(with_ats(), provider_key=PROVIDER, now=NOW)
    assert (await companies.get(created.company_id)).detected_ats.status \
        is DetectionStatus.LIKELY
    await resolution.ingest(with_ats(status=DetectionStatus.CONFIRMED),
                            provider_key=PROVIDER, now=LATER)
    detected = (await companies.get(created.company_id)).detected_ats
    assert detected.status is DetectionStatus.CONFIRMED
    assert detected.organization_id == "acme"
    assert detected.detected_by == PROVIDER


async def test_a_second_platform_does_not_displace_the_detected_one(
        resolution, companies):
    """Two providers naming two platforms is a review question, not a race.

    Overwriting would make the stored ATS depend on which provider ran last, which is
    exactly the silent, order-dependent merge §24 keeps out of the automatic path.
    """
    created = await resolution.ingest(with_ats(), provider_key=PROVIDER, now=NOW)
    await resolution.ingest(
        with_ats(platform=AtsPlatform.LEVER, organization_id="acme-lever",
                 status=DetectionStatus.CONFIRMED),
        provider_key=PROVIDER, now=LATER)
    detected = (await companies.get(created.company_id)).detected_ats
    assert detected.platform is AtsPlatform.GREENHOUSE
    assert detected.organization_id == "acme"


async def test_a_new_site_is_appended_and_a_known_one_is_not_repeated(
        resolution, companies):
    """Locations are compared by value, because their ids are minted per pass.

    Comparing by id would append the same city on every sweep and §23 would fail on
    a table nobody was watching.
    """
    lausanne = Location(country="CH", city="Lausanne")
    geneva = Location(country="CH", city="Geneva")
    created = await resolution.ingest(
        a_discovered_company("Fixture SA", locations=(lausanne,)),
        provider_key=PROVIDER, now=NOW)
    await resolution.ingest(a_discovered_company("Fixture SA", locations=(lausanne,)),
                            provider_key=PROVIDER, now=LATER)
    assert len((await companies.get(created.company_id)).locations) == 1
    await resolution.ingest(
        a_discovered_company("Fixture SA", locations=(lausanne, geneva)),
        provider_key=PROVIDER, now=LATER)
    stored = await companies.get(created.company_id)
    assert [site.location.city for site in stored.locations] == ["Lausanne", "Geneva"]
    # No Phase 6 provider can tell which site is the head office (§22).
    assert not any(site.is_headquarters for site in stored.locations)


# --- §4: another source's spelling is an alias, never a correction -------------

async def test_a_different_spelling_becomes_an_alias_and_the_name_stands(
        resolution, companies):
    """§4 in one test: `LOGITECH` is recorded, `Logitech Europe S.A.` is kept.

    The provenance is the provider that used the label, which is what lets an
    operator judge later whether a board's shouting is worth as much as a register's
    spelling.
    """
    created = await resolution.ingest(
        a_discovered_company("Logitech Europe S.A.",
                             seed=a_seed("Logitech Europe S.A."),
                             website="https://logitech.com"),
        provider_key=PROVIDER, now=NOW)
    await resolution.ingest(
        a_discovered_company("LOGITECH", seed=a_seed("LOGITECH"),
                             website="https://logitech.com", aliases=("Logitech SA",)),
        provider_key=OTHER_PROVIDER, now=LATER)
    stored = await companies.get(created.company_id)
    assert stored.name == "Logitech Europe S.A."
    aliases = await companies.aliases(created.company_id)
    # A set: both labels were seen in the same instant, so the row order is the
    # derived id's and asserting one would pin a uuid5 digest rather than a rule.
    assert {alias.alias for alias in aliases} == {"LOGITECH", "Logitech SA"}
    assert {alias.source_key for alias in aliases} == {OTHER_PROVIDER}


async def test_the_same_alias_seen_twice_is_one_row_with_a_widened_window(
        resolution, companies):
    """Rediscovering a label must not make it look newly found (§23)."""
    created = await resolution.ingest(
        a_discovered_company("Fixture SA", aliases=("Fixture Group",)),
        provider_key=PROVIDER, now=NOW)
    await resolution.ingest(
        a_discovered_company("Fixture SA", aliases=("Fixture Group",)),
        provider_key=PROVIDER, now=LATER)
    aliases = await companies.aliases(created.company_id)
    assert len(aliases) == 1
    assert (aliases[0].first_seen_at, aliases[0].last_seen_at) == (NOW, LATER)


# --- §11: careers endpoints -----------------------------------------------------

async def test_every_endpoint_is_kept_and_the_corporate_page_is_the_preferred_one(
        resolution, companies, sites):
    """A company has several endpoints; `careers_url` is a convenience, not the set.

    The board is what a source plugin can fetch and the corporate page is what a human
    opens, so both are persisted and the human one becomes the preferred field.
    """
    created = await resolution.ingest(
        a_discovered_company(
            "Acme SA",
            career_sites=(
                DiscoveredCareerSite(url="https://boards.greenhouse.io/acme",
                                     kind=CareerSiteKind.ATS_BOARD,
                                     platform=AtsPlatform.GREENHOUSE,
                                     verification_status=DetectionStatus.CONFIRMED),
                DiscoveredCareerSite(url="https://acme.test/careers"))),
        provider_key=PROVIDER, now=NOW)
    stored = await companies.get(created.company_id)
    assert stored.careers_url == "https://acme.test/careers"
    persisted = await sites.list_for_company(created.company_id)
    assert len(persisted) == 2
    assert {site.source_key for site in persisted} == {PROVIDER}
    # Nothing in Phase 6 fetches a URL, so no row may claim to have been checked (§9).
    assert all(site.last_checked_at is None for site in persisted)


async def test_re_detecting_the_same_board_updates_one_row(resolution, sites):
    """The id is derived from the URL, so a second sweep refreshes rather than appends."""
    site = DiscoveredCareerSite(url="https://acme.test/careers")
    created = await resolution.ingest(
        a_discovered_company("Acme SA", career_sites=(site,)),
        provider_key=PROVIDER, now=NOW)
    await resolution.ingest(a_discovered_company("Acme SA", career_sites=(site,)),
                            provider_key=PROVIDER, now=LATER)
    persisted = await sites.list_for_company(created.company_id)
    assert len(persisted) == 1
    assert persisted[0].discovered_at == NOW


# --- §12: spontaneous applications are answered, never inferred ----------------

async def test_a_company_nobody_examined_carries_no_channel(resolution, companies):
    """`UNKNOWN` with no evidence stores nothing, which is not the same as `UNKNOWN`.

    A stored channel would say "we looked and could not decide". Leaving it `None`
    says "nobody looked", and §12 exists to keep those two apart — the more so since
    a company with no active posting is the *normal* case, not a signal.
    """
    created = await resolution.ingest(a_discovered_company(), provider_key=PROVIDER,
                                      now=NOW)
    stored = await companies.get(created.company_id)
    assert stored.spontaneous_application_channel is None
    assert stored.accepts_spontaneous_applications is None


@pytest.mark.parametrize(
    ("support", "url", "flag"),
    [(SpontaneousApplicationSupport.SUPPORTED, "https://acme.test/spontaneous", True),
     (SpontaneousApplicationSupport.NOT_SUPPORTED, None, False)])
async def test_a_decided_answer_is_stored_with_its_flag(resolution, companies,
                                                        support, url, flag):
    """The tri-state and the boolean are written together and cannot disagree."""
    created = await resolution.ingest(with_spontaneous(support, url=url),
                                      provider_key=PROVIDER, now=NOW)
    stored = await companies.get(created.company_id)
    assert stored.spontaneous_application_channel.support is support
    assert stored.spontaneous_application_channel.observed_by == PROVIDER
    assert stored.spontaneous_application_channel.evidence
    assert stored.accepts_spontaneous_applications is flag


async def test_a_decision_answers_an_unknown_and_then_is_not_retracted(
        resolution, companies):
    """A provider that could not find the form may not undo one somebody saw.

    An undecided channel is replaced by a decided one; a decided one stands. The
    alternative — newest wins — would let the two providers flip the answer on
    alternate sweeps, with evidence on both sides.
    """
    created = await resolution.ingest(
        with_spontaneous(SpontaneousApplicationSupport.UNKNOWN),
        provider_key=PROVIDER, now=NOW)
    await resolution.ingest(
        with_spontaneous(SpontaneousApplicationSupport.SUPPORTED,
                         url="https://acme.test/spontaneous"),
        provider_key=PROVIDER, now=LATER)
    stored = await companies.get(created.company_id)
    assert stored.spontaneous_application_channel.support \
        is SpontaneousApplicationSupport.SUPPORTED
    await resolution.ingest(
        with_spontaneous(SpontaneousApplicationSupport.NOT_SUPPORTED),
        provider_key=PROVIDER, now=LATER)
    stored = await companies.get(created.company_id)
    assert stored.spontaneous_application_channel.support \
        is SpontaneousApplicationSupport.SUPPORTED
    assert stored.accepts_spontaneous_applications is True


# --- §27: pointing stored postings at their employers ---------------------------

async def test_a_posting_is_linked_to_the_company_its_own_sighting_created(
        resolution, companies, postings):
    """§27's walkthrough, end to end, with the posting's own string untouched (§13).

    `Opportunity(company_name="Logitech")` → resolver → `Company(id=…)` →
    `Opportunity.company_id`. The link is made on a stored assertion — this provider's
    derived identity *is* that company — and not on a name comparison, which §2 would
    forbid.
    """
    await postings.upsert(a_posting())
    created = await resolution.ingest(
        a_discovered_company("Logitech",
                             seed=a_seed("Logitech",
                                         kind=CompanySeedKind.OPPORTUNITY)),
        provider_key=OPPORTUNITY_SEED_PROVIDER, now=NOW)
    report = await resolution.link_opportunities()
    assert (report.examined, report.linked) == (1, 1)
    linked = await postings.get(OPPORTUNITY)
    assert linked.company_id == created.company_id
    assert linked.company_name == "Logitech"
    assert await companies.get(created.company_id) is not None


async def test_a_second_link_pass_finds_nothing_left_and_changes_nothing(
        resolution, postings):
    """Idempotence at the linking layer: the backlog empties and stays empty."""
    await postings.upsert(a_posting())
    await resolution.ingest(
        a_discovered_company("Logitech", seed=a_seed("Logitech")),
        provider_key=OPPORTUNITY_SEED_PROVIDER, now=NOW)
    first = await resolution.link_opportunities()
    second = await resolution.link_opportunities()
    assert first.linked == 1
    assert second.examined == 0
    assert second.is_exhausted


async def test_a_posting_links_on_the_ats_organization_in_its_apply_url(
        resolution, companies, postings):
    """The second route: the posting's own evidence resolves it (§13).

    The application URL is read for the platform's identifier for the employer, and
    deliberately not as domain evidence — `jobs.lever.co` is Lever's host, and treating
    it as the company's would make every Lever employer one company.
    """
    await companies.upsert(a_company(
        name="Acme Group", locations=(),
        detected_ats=DetectedATS(platform=AtsPlatform.LEVER, organization_id="acme",
                                 detected_by=PROVIDER, evidence=(an_evidence(),))))
    await postings.upsert(a_posting(
        company_name="ACME", application_url="https://jobs.lever.co/acme/123"))
    report = await resolution.link_opportunities()
    assert (report.examined, report.linked) == (1, 1)
    assert (await postings.get(OPPORTUNITY)).company_id == COMPANY


async def test_a_posting_no_employer_matches_stays_unlinked_and_is_counted(
        resolution, postings):
    """Unknown is reported, never guessed: the posting keeps a null `company_id`."""
    await postings.upsert(a_posting(company_name="Nobody SA"))
    report = await resolution.link_opportunities()
    assert (report.examined, report.linked, report.unresolved) == (1, 0, 1)
    assert (await postings.get(OPPORTUNITY)).company_id is None


async def test_a_posting_two_employers_could_be_is_counted_ambiguous(
        resolution, companies, postings):
    """§14 at the posting layer: two candidates on a name means neither is chosen.

    Counted apart from `unresolved` because the two call for different actions — one
    is "create the employer", the other is "a human should look".
    """
    await companies.upsert(a_company(name="Acme SA", website="https://acme-one.test",
                                     locations=()))
    await companies.upsert(a_company(id=OTHER_COMPANY, name="Acme SA",
                                     website="https://acme-two.test", locations=()))
    await postings.upsert(a_posting(company_name="Acme SA", application_url=None))
    report = await resolution.link_opportunities()
    assert (report.linked, report.ambiguous, report.unresolved) == (0, 1, 0)
    assert (await postings.get(OPPORTUNITY)).company_id is None


async def test_the_link_pass_is_bounded_and_works_oldest_first(resolution, postings):
    """A bounded pass makes progress through the backlog instead of re-reading it."""
    await postings.upsert(a_posting())
    await postings.upsert(a_posting(id=OTHER_OPPORTUNITY, company_name="Nobody SA"))
    await postings.upsert(a_posting(id=THIRD_POSTING, company_name="Nobody Else SA"))
    report = await resolution.link_opportunities(limit=1)
    assert report.examined == 1


# --- §17, §26: one full pass ----------------------------------------------------

async def test_a_pass_persists_what_the_providers_reported(resolution, companies,
                                                           records, postings):
    """Discover, then persist, then link — the whole of what an endpoint calls."""
    await postings.upsert(a_posting())
    provider = FakeCompanyProvider(
        a_provider_metadata("first_provider"),
        result=a_provider_result(
            "first_provider",
            a_discovered_company("Logitech", seed=a_seed("Logitech"),
                                 website="https://logitech.com")))
    outcome = await a_pass(resolution, provider).run(CompanyDiscoveryRequest(),
                                                     now=NOW)
    assert outcome.created == 1
    assert len(outcome.company_ids) == 1
    assert outcome.report.is_complete
    assert len(companies.companies) == 1
    assert len(records.records) == 1
    # The posting names Logitech but no *posting-derived* sighting exists, so it
    # resolves on its own evidence — of which it has none beyond the name.
    assert outcome.links.examined == 1


async def test_one_provider_failing_costs_the_others_nothing(resolution, companies):
    """§26: a broken provider is a fact in the report, not an exception.

    The healthy provider's employer is written, the failure is named, and the pass is
    reported as incomplete so nobody reads the short list as "there is nothing else".
    """
    healthy = FakeCompanyProvider(
        a_provider_metadata("first_provider"),
        result=a_provider_result("first_provider", a_discovered_company()))
    broken = FakeCompanyProvider(a_provider_metadata("broken_provider"),
                                 error=RuntimeError("the source did not answer"))
    outcome = await a_pass(resolution, healthy, broken).run(CompanyDiscoveryRequest(),
                                                            now=NOW)
    assert outcome.created == 1
    assert outcome.report.unusable_provider_keys == ("broken_provider",)
    assert not outcome.report.is_complete
    assert len(companies.companies) == 1


async def test_two_providers_reporting_one_employer_write_one_company(
        resolution, companies, records):
    """Ingestion is sequential precisely so the second provider sees the first.

    Gathering the claims would let two passes both find nothing stored and both
    create the employer, which is the duplicate §23 forbids.
    """
    claim = a_discovered_company("Logitech", seed=a_seed("Logitech"),
                                 website="https://logitech.com")
    first = FakeCompanyProvider(a_provider_metadata("first_provider"),
                                result=a_provider_result("first_provider", claim))
    second = FakeCompanyProvider(
        a_provider_metadata("second_provider"),
        result=a_provider_result(
            "second_provider",
            a_discovered_company("LOGITECH", seed=a_seed("LOGITECH", external_id="lg"),
                                 website="https://logitech.com")))
    outcome = await a_pass(resolution, first, second).run(CompanyDiscoveryRequest(),
                                                          now=NOW)
    assert (outcome.created, outcome.matched) == (1, 1)
    assert len(outcome.company_ids) == 1
    assert len(companies.companies) == 1
    assert len(records.records) == 2


async def test_a_link_limit_of_zero_skips_the_posting_pass(resolution, postings):
    """A caller that wants discovery alone gets discovery alone."""
    await postings.upsert(a_posting())
    provider = FakeCompanyProvider(
        a_provider_metadata("first_provider"),
        result=a_provider_result("first_provider", a_discovered_company()))
    outcome = await a_pass(resolution, provider).run(CompanyDiscoveryRequest(),
                                                     now=NOW, link_limit=0)
    assert outcome.links.examined == 0
    assert (await postings.get(OPPORTUNITY)).company_id is None


async def test_health_probes_without_running_a_pass(resolution):
    """A status page must not have to write to the database to render."""
    provider = FakeCompanyProvider(a_provider_metadata("first_provider"))
    health = await a_pass(resolution, provider).health()
    assert [record.source_key for record in health] == ["first_provider"]
    assert provider.healthchecks == 1
    assert provider.requests == []


# --- §20, §28: the read side ----------------------------------------------------

async def test_an_employer_with_no_opportunity_is_still_in_the_directory(
        directory, companies, postings):
    """§28's acceptance criterion, and the filter that makes it visible.

    A company with zero active postings is returned like any other; whether a caller
    wants only the ones with postings is the caller's question, never a condition the
    directory imposes.
    """
    await companies.upsert(a_company(name="Alpha SA", locations=()))
    await companies.upsert(a_company(id=OTHER_COMPANY, name="Beta AG", locations=()))
    await postings.upsert(a_posting(company_id=COMPANY))
    assert (await directory.search()).total == 2
    quiet = await directory.search(CompanyFilter(has_opportunities=False))
    assert [company.name for company in quiet.companies] == ["Beta AG"]
    busy = await directory.search(CompanyFilter(has_opportunities=True))
    assert [company.name for company in busy.companies] == ["Alpha SA"]


async def test_a_page_is_never_unbounded_whatever_a_caller_asks_for(
        directory, companies):
    """§20's "do not return unbounded company lists", as the backstop it is.

    The route validates its own query parameter; this clamp is what holds for a CLI,
    a test and any other caller that never passes through HTTP.
    """
    for index in range(MAX_PAGE_SIZE + 1):
        await companies.upsert(a_company(id=new_company_id(),
                                         name=f"Employer {index:03d}", locations=()))
    page = await directory.search(limit=10_000)
    assert len(page.companies) == MAX_PAGE_SIZE
    assert page.total == MAX_PAGE_SIZE + 1
    assert len((await directory.search()).companies) == DEFAULT_PAGE_SIZE
    assert len((await directory.search(limit=0)).companies) == 1


async def test_a_negative_offset_reads_the_first_page(directory, companies):
    """Clamped rather than refused: a programmatic caller gets a page, not a traceback."""
    await companies.upsert(a_company(name="Alpha SA", locations=()))
    await companies.upsert(a_company(id=OTHER_COMPANY, name="Beta AG", locations=()))
    assert await directory.search(offset=-5) == await directory.search(offset=0)


async def test_the_detail_assembles_the_labels_endpoints_and_provenance(
        resolution, directory):
    """§29's safe structured provenance, before the response model narrows it.

    Four reads rather than a join, because the endpoints and the sightings are not
    aggregate children of the company — they are written independently so one provider
    cannot delete another's findings.
    """
    created = await resolution.ingest(
        a_discovered_company(
            "Acme SA", aliases=("ACME",), website="https://acme.test",
            career_sites=(DiscoveredCareerSite(url="https://acme.test/careers"),)),
        provider_key=PROVIDER, now=NOW)
    detail = await directory.get(created.company_id)
    assert detail.company.name == "Acme SA"
    assert [alias.alias for alias in detail.aliases] == ["ACME"]
    assert [site.url for site in detail.career_sites] == ["https://acme.test/careers"]
    assert detail.provider_keys == (PROVIDER,)
    assert detail.discoveries[0].external_id == normalize_company_name("Acme SA")


async def test_an_unknown_company_is_none_rather_than_an_error(directory):
    """"No such company" is an ordinary answer to a UUID somebody typed."""
    assert await directory.get(new_company_id()) is None
