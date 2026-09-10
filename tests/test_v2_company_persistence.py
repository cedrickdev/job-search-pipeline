# tests/test_v2_company_persistence.py
"""What the Phase 6 tables promise, asked of PostgreSQL.

Three properties, none of which a fake repository could establish.

**Repeating a pass changes nothing** (§23). Every Phase 6 write is an upsert keyed
on a derived uuid5, so the second sighting of an employer, of a label, of a careers
endpoint or of a provider's own identifier updates one row. The tests here run each
write twice and count, because the failure mode — a second row that looks like a
second employer — is invisible to anything that only reads back the last write.

**The database refuses what the domain refuses.** A `model_validator` protects the
rows that go through Python; a backfill, a psql session and the next phase's
importer do not. Every rule that a column group can express is a named CHECK, and
each test here writes an ORM row directly — bypassing the domain on purpose, since
the domain would refuse most of them — and asserts the constraint name a migration
could later drop.

**Provenance outlives what it points at** (§5, §14). A sighting the resolver
refused to attach stays recorded and unlinked; a sighting whose employer is deleted
survives with a null link, while the labels and endpoints that *belong* to that
employer go with it.

The reads are here too, for the same reason: `find_candidates` is a union of four
indexed lookups and `search` is five filters over a page, and both are SQL rather
than Python. The migration itself, the clean run from an empty schema and the
rollback belong to `tests/test_v2_persistence_migrations.py`; the table inventory
and the "no `user_id` on a shared table" rule (§21) belong to
`tests/test_v2_persistence_schema.py`.

Every test runs inside the transaction `db_session` opened and will roll back.
"""
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from backend.app.domain.company import (
    AtsPlatform,
    CareerSiteKind,
    DetectedATS,
    DetectionStatus,
    Evidence,
    SpontaneousApplicationChannel,
    SpontaneousApplicationSupport,
)
from backend.app.domain.identifiers import (
    CareerSiteId,
    CompanyAliasId,
    CompanyDiscoveryRecordId,
    OpportunityId,
)
from backend.app.infrastructure.database.models import (
    CompanyAliasRow,
    CompanyCareerSiteRow,
    CompanyDiscoveryRecordRow,
    CompanyRow,
    OpportunityRow,
)
from backend.app.repositories.contracts import CompanyFilter
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyCareerSiteRepository,
    SqlAlchemyCompanyDiscoveryRepository,
    SqlAlchemyCompanyRepository,
    SqlAlchemyOpportunityRepository,
)
from tests.v2_builders import (
    COMPANY,
    LATER,
    NOW,
    OPPORTUNITY,
    OTHER_COMPANY,
    a_company,
    a_source_record,
    an_opportunity,
)
from tests.v2_companies import a_career_site, a_discovery_record, an_alias

# Every test here is async and pytest-asyncio runs in strict mode (pyproject.toml).
pytestmark = pytest.mark.asyncio

# A sighting older than `NOW`, which is the case the `min`/`max` window handling
# exists for: a seed file read for the first time can report an *earlier* first
# sighting than the one already stored.
EARLIER = datetime(2026, 2, 1, 9, 30, tzinfo=UTC)

# Identities for the rows written directly against the schema. Each is a *second*
# row claiming a key that is already taken, so the derived-id upsert path — which
# would quietly update instead — is out of the way and the unique constraint is
# what answers.
SECOND_ALIAS = CompanyAliasId(UUID("00000000-0000-4000-8000-0000000000d1"))
SECOND_SITE = CareerSiteId(UUID("00000000-0000-4000-8000-0000000000d2"))
SECOND_RECORD = CompanyDiscoveryRecordId(UUID("00000000-0000-4000-8000-0000000000d3"))
SECOND_POSTING = OpportunityId(UUID("00000000-0000-4000-8000-000000000023"))
NAMELESS_POSTING = OpportunityId(UUID("00000000-0000-4000-8000-000000000029"))

AN_EVIDENCE = Evidence(code="ATS_BOARD_URL",
                       detail="the careers URL is a Greenhouse board")

# `Company._the_channel_and_the_flag_agree`, as the table that builds a company
# whose two representations cannot contradict each other.
SPONTANEOUS_FLAG = {
    SpontaneousApplicationSupport.SUPPORTED: True,
    SpontaneousApplicationSupport.NOT_SUPPORTED: False,
    SpontaneousApplicationSupport.UNKNOWN: None,
}


def a_detection(**overrides) -> DetectedATS:
    """A `LIKELY` Greenhouse detection with the evidence §10 requires."""
    fields = {
        "platform": AtsPlatform.GREENHOUSE,
        "organization_id": "acme",
        "status": DetectionStatus.LIKELY,
        "detected_by": "test_provider",
        "evidence": (AN_EVIDENCE,),
    }
    fields.update(overrides)
    return DetectedATS(**fields)


def a_channel(support=SpontaneousApplicationSupport.SUPPORTED,
              **overrides) -> SpontaneousApplicationChannel:
    """A spontaneous-application verdict carrying its basis (§12).

    `UNKNOWN` is built bare, because it is the only value allowed to carry none:
    demanding evidence for "nobody has looked" would mean inventing some.
    """
    fields: dict[str, object] = {"support": support}
    if support is not SpontaneousApplicationSupport.UNKNOWN:
        fields |= {"observed_by": "test_provider", "evidence": (AN_EVIDENCE,)}
    if support is SpontaneousApplicationSupport.SUPPORTED:
        fields["url"] = "https://example.test/spontaneous"
    fields.update(overrides)
    return SpontaneousApplicationChannel(**fields)


def a_company_row(**overrides) -> CompanyRow:
    """The three columns an employer cannot be without, and nothing else.

    Minimal on purpose: each refusal test adds exactly the columns whose
    combination is supposed to be rejected, so a failure names one rule rather
    than whichever of five the database happened to evaluate first.
    """
    columns = {"id": COMPANY, "name": "Fixture SA", "normalized_name": "fixture sa"}
    columns.update(overrides)
    return CompanyRow(**columns)


async def _count(session, model) -> int:
    """How many rows of `model` the current transaction can see."""
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


async def refuses(session, row, constraint: str) -> None:
    """Assert the flush fails, and that `constraint` is what rejected the row.

    Inside a savepoint, so the session survives the violation and a parametrized
    case cannot poison the next one — the same helper, for the same reason, as
    `tests/test_v2_persistence_constraints.py`.
    """
    with pytest.raises(IntegrityError, match=constraint):
        async with session.begin_nested():
            session.add(row)
            await session.flush()


@pytest.fixture
def companies(db_session):
    return SqlAlchemyCompanyRepository(db_session)


@pytest.fixture
def career_sites(db_session):
    return SqlAlchemyCareerSiteRepository(db_session)


@pytest.fixture
def discoveries(db_session):
    return SqlAlchemyCompanyDiscoveryRepository(db_session)


@pytest.fixture
def opportunities(db_session):
    return SqlAlchemyOpportunityRepository(db_session)


@pytest_asyncio.fixture
async def stored_company(companies):
    """One employer, so an alias, an endpoint or a sighting has a key to point at."""
    return await companies.upsert(a_company())


@pytest_asyncio.fixture
async def a_directory(companies, opportunities):
    """Two employers that differ in every column the directory filters on.

    `Beta AG` has no posting, no website and no detected ATS, which is §28's
    acceptance criterion in fixture form: a configured employer with zero active
    opportunities is still a row the directory returns.
    """
    await companies.upsert(a_company(
        name="Alpha SA", country="CH", detected_ats=a_detection(),
        spontaneous_application_channel=a_channel(),
        accepts_spontaneous_applications=True))
    await companies.upsert(a_company(
        locations=(), id=OTHER_COMPANY, name="Beta AG", country="DE",
        website=None, careers_url=None,
        spontaneous_application_channel=a_channel(
            SpontaneousApplicationSupport.UNKNOWN),
        accepts_spontaneous_applications=None))
    await opportunities.upsert(an_opportunity(company_id=COMPANY))


# --------------------------------------------------------------------------
# §23 — running the same pass twice
# --------------------------------------------------------------------------


async def test_rediscovering_an_employer_updates_the_row_it_already_has(
        companies, db_session):
    written = await companies.upsert(a_company(name="Fixture Holding SA"))
    assert written.name == "Fixture Holding SA"
    assert await _count(db_session, CompanyRow) == 1


async def test_a_label_seen_again_widens_its_window_instead_of_adding_a_row(
        companies, db_session, stored_company):
    first = await companies.upsert_alias(an_alias())
    again = await companies.upsert_alias(an_alias(first_seen_at=LATER,
                                                 last_seen_at=LATER))
    # The id is derived from `(company_id, normalized_alias)`, so the second
    # sighting is the same row by construction rather than by a lookup that could
    # miss — and `first_seen_at` answers "since when have we called it this?".
    assert again.id == first.id
    assert (again.first_seen_at, again.last_seen_at) == (NOW, LATER)
    assert await _count(db_session, CompanyAliasRow) == 1


async def test_a_label_reported_with_an_older_sighting_moves_first_seen_backwards(
        companies, stored_company):
    """A seed file read for the first time may know about an earlier sighting.

    `min`/`max` rather than "overwrite" precisely for this: the window only ever
    grows, in whichever direction the new evidence points.
    """
    await companies.upsert_alias(an_alias())
    widened = await companies.upsert_alias(an_alias(first_seen_at=EARLIER,
                                                    last_seen_at=EARLIER))
    assert (widened.first_seen_at, widened.last_seen_at) == (EARLIER, NOW)


async def test_a_rediscovered_endpoint_keeps_the_instant_it_was_first_found(
        career_sites, db_session, stored_company):
    first = await career_sites.upsert(a_career_site())
    assert first.last_checked_at is None, \
        "Phase 6 fetches nothing, so nothing may claim to have checked a URL"
    again = await career_sites.upsert(a_career_site(discovered_at=LATER))
    assert again.discovered_at == NOW
    assert await _count(db_session, CompanyCareerSiteRow) == 1


async def test_a_pass_that_did_not_check_an_endpoint_erases_no_check(
        career_sites, stored_company):
    await career_sites.upsert(a_career_site(last_checked_at=LATER))
    unchecked = await career_sites.upsert(a_career_site())
    assert unchecked.last_checked_at == LATER


async def test_one_provider_reporting_one_employer_twice_is_one_sighting(
        discoveries, db_session):
    await discoveries.upsert(a_discovery_record())
    again = await discoveries.upsert(
        a_discovery_record(discovered_at=LATER,
                           confidence=DetectionStatus.CONFIRMED))
    # First sighting wins for `discovered_at` and only for `discovered_at`: what
    # the provider now believes about the employer is this pass's answer.
    assert again.discovered_at == NOW
    assert again.confidence is DetectionStatus.CONFIRMED
    assert await _count(db_session, CompanyDiscoveryRecordRow) == 1


async def test_linking_a_sighting_later_does_not_record_a_second_one(
        discoveries, db_session, stored_company):
    await discoveries.upsert(a_discovery_record())
    linked = await discoveries.upsert(a_discovery_record(company_id=COMPANY))
    assert linked.company_id == COMPANY
    assert await _count(db_session, CompanyDiscoveryRecordRow) == 1


# --------------------------------------------------------------------------
# What the schema refuses
# --------------------------------------------------------------------------


@pytest.mark.parametrize("normalized_name", ["Fixture SA", ""])
async def test_a_comparison_column_holding_a_display_name_is_refused(
        db_session, normalized_name):
    """`normalized_name` is a projection of a domain property, never typed by hand.

    A row where it holds the display name would be invisible to every lookup
    `resolution` performs, which is a duplicate employer nobody can see coming.
    """
    await refuses(db_session, a_company_row(normalized_name=normalized_name),
                  "ck_companies_normalized_name_is_comparison_form")


async def test_a_domain_column_holding_an_unnormalized_host_is_refused(db_session):
    await refuses(db_session, a_company_row(normalized_domain="Example.Test"),
                  "ck_companies_normalized_domain_is_comparison_form")


async def test_a_country_that_is_not_an_iso_code_is_refused(db_session):
    await refuses(db_session, a_company_row(country="ch"),
                  "ck_companies_country_format")


async def test_a_detection_status_without_a_platform_is_refused(db_session):
    """§10's distinction, as a constraint: a status asserting confidence about a
    platform nobody detected is not a weaker claim, it is an incoherent one."""
    await refuses(db_session,
                  a_company_row(ats_status=DetectionStatus.LIKELY,
                                ats_detected_by="test_provider"),
                  "ck_companies_ats_complete_or_absent")


async def test_a_confirmed_detection_without_an_organization_is_refused(db_session):
    await refuses(
        db_session,
        a_company_row(ats_platform=AtsPlatform.GREENHOUSE,
                      ats_status=DetectionStatus.CONFIRMED,
                      ats_detected_by="test_provider",
                      ats_evidence=[{"code": "ATS_BOARD_URL",
                                     "detail": "redirects to the board"}]),
        "ck_companies_ats_complete_or_absent")


async def test_a_spontaneous_verdict_that_contradicts_the_flag_is_refused(db_session):
    """The boolean the API exposes and the evidence-backed verdict must agree.

    Two representations are legitimate; a row where they disagree would make the
    answer depend on which column the reader happened to pick.
    """
    await refuses(
        db_session,
        a_company_row(spontaneous_support=SpontaneousApplicationSupport.SUPPORTED,
                      accepts_spontaneous_applications=None,
                      spontaneous_observed_by="test_provider",
                      spontaneous_evidence=[{"code": "SPONTANEOUS_FORM",
                                             "detail": "a form is published"}]),
        "ck_companies_spontaneous_support_matches_flag")


async def test_a_decided_verdict_without_evidence_is_refused(db_session):
    await refuses(
        db_session,
        a_company_row(
            spontaneous_support=SpontaneousApplicationSupport.NOT_SUPPORTED,
            accepts_spontaneous_applications=False),
        "ck_companies_spontaneous_support_matches_flag")


async def test_two_employers_cannot_claim_one_ats_organization(
        companies, db_session, stored_company):
    """`boards.greenhouse.io/acme` is one employer's board.

    Two rows claiming it is the duplication `resolve` reports as `AMBIGUOUS`, and
    the partial unique index is what stops it becoming stored fact.
    """
    await companies.upsert(a_company(detected_ats=a_detection()))
    with pytest.raises(IntegrityError,
                       match="uq_companies_ats_platform_organization_id"):
        async with db_session.begin_nested():
            await companies.upsert(a_company(locations=(), id=OTHER_COMPANY,
                                             name="Impostor SA",
                                             detected_ats=a_detection()))


async def test_the_same_organization_id_on_another_platform_is_allowed(
        companies, db_session, stored_company):
    """`acme` on Greenhouse and `acme` on Lever are two employers, not one.

    An organization id is an identity *within* its platform, which is why the
    index covers the pair and why `find_candidates` needs both halves.
    """
    await companies.upsert(a_company(detected_ats=a_detection()))
    await companies.upsert(a_company(
        locations=(), id=OTHER_COMPANY, name="Acme AG",
        detected_ats=a_detection(platform=AtsPlatform.LEVER)))
    assert await _count(db_session, CompanyRow) == 2


async def test_a_second_row_for_one_label_is_refused(
        companies, db_session, stored_company):
    """The derived id is the mechanism; the unique constraint is the backstop.

    Written as a raw row with a fresh id precisely to get past the upsert path: a
    caller that invents its own key must still not be able to file `LOGITECH`
    twice against one employer.
    """
    await companies.upsert_alias(an_alias(alias="Logitech"))
    await refuses(
        db_session,
        CompanyAliasRow(id=SECOND_ALIAS, company_id=COMPANY, alias="LOGITECH",
                        normalized_alias="logitech", source_key="other_provider",
                        first_seen_at=NOW, last_seen_at=NOW),
        "uq_company_aliases_company_id_normalized_alias")


async def test_an_alias_stored_in_display_form_is_refused(db_session, stored_company):
    await refuses(
        db_session,
        CompanyAliasRow(id=SECOND_ALIAS, company_id=COMPANY, alias="LOGITECH",
                        normalized_alias="LOGITECH", source_key="test_provider",
                        first_seen_at=NOW, last_seen_at=NOW),
        "ck_company_aliases_normalized_alias_is_comparison_form")


async def test_a_sighting_window_that_ends_before_it_starts_is_refused(
        db_session, stored_company):
    await refuses(
        db_session,
        CompanyAliasRow(id=SECOND_ALIAS, company_id=COMPANY, alias="Logitech",
                        normalized_alias="logitech", source_key="test_provider",
                        first_seen_at=LATER, last_seen_at=NOW),
        "ck_company_aliases_seen_window_ordered")


async def test_a_second_row_for_one_endpoint_is_refused(
        career_sites, db_session, stored_company):
    await career_sites.upsert(a_career_site())
    await refuses(
        db_session,
        CompanyCareerSiteRow(id=SECOND_SITE, company_id=COMPANY,
                             url="https://example.test/jobs",
                             kind=CareerSiteKind.CAREERS_PAGE,
                             source_key="other_provider", discovered_at=NOW),
        "uq_company_career_sites_company_id_url")


async def test_an_ats_board_that_names_no_platform_is_refused(
        db_session, stored_company):
    """A board nothing can identify the platform of is a board no plugin can read."""
    await refuses(
        db_session,
        CompanyCareerSiteRow(id=SECOND_SITE, company_id=COMPANY,
                             url="https://boards.greenhouse.io/acme",
                             kind=CareerSiteKind.ATS_BOARD, platform=None,
                             source_key="test_provider", discovered_at=NOW),
        "ck_company_career_sites_ats_board_names_its_platform")


async def test_an_endpoint_checked_before_it_was_found_is_refused(
        db_session, stored_company):
    await refuses(
        db_session,
        CompanyCareerSiteRow(id=SECOND_SITE, company_id=COMPANY,
                             url="https://example.test/jobs",
                             kind=CareerSiteKind.CAREERS_PAGE,
                             source_key="test_provider", discovered_at=LATER,
                             last_checked_at=NOW),
        "ck_company_career_sites_checked_after_discovered")


async def test_one_provider_cannot_file_one_external_identifier_twice(
        discoveries, db_session):
    """§23's external identity uniqueness, independent of how the id was derived."""
    await discoveries.upsert(a_discovery_record())
    await refuses(
        db_session,
        CompanyDiscoveryRecordRow(
            id=SECOND_RECORD, provider_key="test_provider",
            external_id="fixture sa", seed_kind="CONFIGURED",
            company_name="Fixture Holding SA", discovered_at=LATER),
        "uq_company_discovery_records_provider_key_external_id")


async def test_two_providers_may_report_the_same_employer(discoveries, db_session):
    """The key is `(provider, identifier)`, so provenance is per provider.

    Collapsing these two would lose the answer to "how many sources agree?", which
    is the evidence a doubtful identity is later judged on.
    """
    await discoveries.upsert(a_discovery_record())
    await discoveries.upsert(a_discovery_record(provider_key="other_provider"))
    assert await _count(db_session, CompanyDiscoveryRecordRow) == 2


# --------------------------------------------------------------------------
# §5, §14 — provenance outlives what it points at
# --------------------------------------------------------------------------


async def test_a_seed_the_resolver_refused_stays_recorded_and_unlinked(discoveries):
    """§14: an ambiguous seed is kept rather than guessed at.

    Keeping it is what stops the next sweep rediscovering and re-refusing the same
    employer forever, which is why `company_id` is nullable rather than the record
    being dropped.
    """
    stored = await discoveries.upsert(a_discovery_record())
    assert stored.company_id is None
    assert await discoveries.get_by_external("test_provider", "fixture sa") == stored
    assert await discoveries.get_by_external("test_provider", "never heard of") is None


async def test_deleting_an_employer_takes_its_labels_and_endpoints_with_it(
        companies, career_sites, db_session, stored_company):
    await companies.upsert_alias(an_alias())
    await career_sites.upsert(a_career_site())
    await db_session.execute(delete(CompanyRow).where(CompanyRow.id == COMPANY))
    assert await _count(db_session, CompanyAliasRow) == 0
    assert await _count(db_session, CompanyCareerSiteRow) == 0


async def test_a_sighting_survives_the_employer_it_pointed_at(
        discoveries, db_session, stored_company):
    """`ON DELETE SET NULL`, because a merge must not destroy its own evidence.

    §24 keeps any merge explicit and auditable; deleting the sightings along with
    the row that lost the merge would delete exactly what made the duplication
    visible.
    """
    await discoveries.upsert(a_discovery_record(company_id=COMPANY))
    await db_session.execute(delete(CompanyRow).where(CompanyRow.id == COMPANY))
    # The record is already in the identity map with its old link, and a plain
    # SELECT hands back what was loaded rather than what the database now holds.
    db_session.expire_all()
    survivor = await discoveries.get_by_external("test_provider", "fixture sa")
    assert survivor is not None
    assert survivor.company_id is None
    assert survivor.company_name == "Fixture SA"


async def test_sightings_of_one_employer_come_back_most_recent_first(
        discoveries, stored_company):
    await discoveries.upsert(a_discovery_record(company_id=COMPANY))
    await discoveries.upsert(a_discovery_record(
        provider_key="other_provider", company_id=COMPANY, discovered_at=LATER))
    listed = await discoveries.list_for_company(COMPANY)
    assert [record.provider_key for record in listed] == ["other_provider",
                                                          "test_provider"]


async def test_labels_come_back_oldest_sighting_first(companies, stored_company):
    await companies.upsert_alias(an_alias(alias="LOGITECH"))
    await companies.upsert_alias(an_alias(alias="Logitech Europe S.A.",
                                          first_seen_at=EARLIER,
                                          last_seen_at=EARLIER))
    stored = await companies.aliases(COMPANY)
    assert [alias.alias for alias in stored] == ["Logitech Europe S.A.", "LOGITECH"]


async def test_endpoints_come_back_oldest_discovery_first(
        career_sites, stored_company):
    await career_sites.upsert(a_career_site(url="https://example.test/jobs"))
    await career_sites.upsert(a_career_site(url="https://boards.greenhouse.io/acme",
                                            kind=CareerSiteKind.ATS_BOARD,
                                            platform=AtsPlatform.GREENHOUSE,
                                            discovered_at=EARLIER))
    stored = await career_sites.list_for_company(COMPANY)
    assert [site.url for site in stored] == ["https://boards.greenhouse.io/acme",
                                             "https://example.test/jobs"]
    assert stored[0].platform is AtsPlatform.GREENHOUSE


# --------------------------------------------------------------------------
# §2, §13 — the shortlist identity resolution judges
# --------------------------------------------------------------------------


async def test_no_evidence_produces_no_shortlist(companies, a_directory):
    """A caller holding nothing gets nothing, not the first page of employers.

    Returning rows here would hand `resolve` a list of companies nothing connects
    to the claim, and the first one sharing a country would look like a candidate.
    """
    assert await companies.find_candidates() == ()


async def test_a_normalized_name_shortlists_the_employer_that_holds_it(
        companies, a_directory):
    found = await companies.find_candidates(name_forms=("alpha sa",))
    assert [candidate.company.name for candidate in found] == ["Alpha SA"]


async def test_a_label_shortlists_the_employer_and_comes_back_with_it(
        companies, a_directory):
    """An alias hit is a name hit: the shortlist has to carry the labels too.

    Without them the comparison would fall back to name similarity, which §2
    forbids as a merge reason.
    """
    await companies.upsert_alias(an_alias(company_id=OTHER_COMPANY,
                                          alias="Beta Aktiengesellschaft"))
    found = await companies.find_candidates(name_forms=("beta aktiengesellschaft",))
    assert [candidate.company.name for candidate in found] == ["Beta AG"]
    assert [alias.alias for alias in found[0].aliases] == ["Beta Aktiengesellschaft"]


async def test_a_domain_shortlists_the_employer_that_publishes_it(
        companies, a_directory):
    found = await companies.find_candidates(domain="example.test")
    assert [candidate.company.name for candidate in found] == ["Alpha SA"]


async def test_an_ats_organization_shortlists_only_within_its_platform(
        companies, a_directory):
    assert [candidate.company.name for candidate in await companies.find_candidates(
        ats_platform=AtsPlatform.GREENHOUSE, ats_organization_id="acme")] == \
        ["Alpha SA"]
    assert await companies.find_candidates(ats_platform=AtsPlatform.LEVER,
                                           ats_organization_id="acme") == ()


async def test_an_organization_id_without_its_platform_is_not_a_lookup(
        companies, a_directory):
    """Half of a composite identity is no identity, so it is not evidence at all."""
    assert await companies.find_candidates(ats_organization_id="acme") == ()


async def test_an_employer_matched_by_several_signals_is_one_candidate(
        companies, a_directory):
    """A union, not a join: four agreeing signals are still one employer."""
    await companies.upsert_alias(an_alias(alias="Alpha"))
    found = await companies.find_candidates(
        name_forms=("alpha sa", "alpha"), domain="example.test",
        ats_platform=AtsPlatform.GREENHOUSE, ats_organization_id="acme")
    assert len(found) == 1
    assert found[0].company.name == "Alpha SA"


# --------------------------------------------------------------------------
# §20, §28 — the directory
# --------------------------------------------------------------------------


async def test_an_employer_with_no_posting_is_still_in_the_directory(
        companies, a_directory):
    """§28's acceptance criterion, end to end.

    `Beta AG` was configured, has never had an opportunity and is returned by the
    directory anyway — the whole point of making companies first-class rather than
    a string on a posting.
    """
    page = await companies.search(CompanyFilter())
    assert [company.name for company in page.companies] == ["Alpha SA", "Beta AG"]
    assert page.total == 2

    without = await companies.search(CompanyFilter(has_opportunities=False))
    assert [company.name for company in without.companies] == ["Beta AG"]
    with_postings = await companies.search(CompanyFilter(has_opportunities=True))
    assert [company.name for company in with_postings.companies] == ["Alpha SA"]


async def test_the_directory_matches_a_fragment_of_the_comparison_form(
        companies, a_directory):
    """Searched on the normalized column, so `alpha s.a.` finds `Alpha SA`."""
    page = await companies.search(CompanyFilter(text="alpha s.a."))
    assert [company.name for company in page.companies] == ["Alpha SA"]


async def test_the_directory_matches_a_label_as_well_as_a_name(
        companies, a_directory):
    await companies.upsert_alias(an_alias(company_id=OTHER_COMPANY,
                                          alias="Beta Aktiengesellschaft"))
    page = await companies.search(CompanyFilter(text="aktiengesellschaft"))
    assert [company.name for company in page.companies] == ["Beta AG"]


async def test_a_query_made_only_of_punctuation_matches_nothing(
        companies, a_directory):
    """It has no comparison form, and `contains("")` would match every employer."""
    page = await companies.search(CompanyFilter(text="!!!"))
    assert page.companies == ()
    assert page.total == 0


async def test_a_percent_sign_is_a_character_and_not_a_wildcard(
        companies, a_directory):
    page = await companies.search(CompanyFilter(text="%"))
    assert page.companies == ()


@pytest.mark.parametrize("filters, expected", [
    (CompanyFilter(country="DE"), "Beta AG"),
    (CompanyFilter(country="CH"), "Alpha SA"),
    (CompanyFilter(ats_platform=AtsPlatform.GREENHOUSE), "Alpha SA"),
    (CompanyFilter(spontaneous_support=SpontaneousApplicationSupport.SUPPORTED),
     "Alpha SA"),
    (CompanyFilter(spontaneous_support=SpontaneousApplicationSupport.UNKNOWN),
     "Beta AG"),
])
async def test_each_filter_narrows_the_directory_to_the_employer_it_describes(
        companies, a_directory, filters, expected):
    """`UNKNOWN` is a filterable answer, which is why §12 is tri-state and not a
    nullable boolean: "nobody has looked" is a queryable state of the directory."""
    page = await companies.search(filters)
    assert [company.name for company in page.companies] == [expected]
    assert page.total == 1


async def test_a_page_reports_how_many_matched_and_not_how_many_it_returned(
        companies, a_directory):
    """§20 keeps the list bounded, so the count is what makes "1–1 of 2" sayable."""
    first = await companies.search(CompanyFilter(), limit=1)
    assert [company.name for company in first.companies] == ["Alpha SA"]
    assert first.total == 2
    second = await companies.search(CompanyFilter(), limit=1, offset=1)
    assert [company.name for company in second.companies] == ["Beta AG"]
    assert second.total == 2


async def test_a_company_comes_back_with_its_sites(companies, a_directory):
    stored = await companies.get(COMPANY)
    assert stored is not None
    assert [location.location.city for location in stored.locations] == ["Lausanne"]
    assert stored.detected_ats == a_detection()
    assert stored.spontaneous_application_channel == a_channel()


# --------------------------------------------------------------------------
# §13, §27 — postings as seeds, and the link back
# --------------------------------------------------------------------------


async def test_a_posting_that_names_an_employer_it_is_not_linked_to_is_a_seed(
        opportunities, stored_company):
    await opportunities.upsert(an_opportunity())
    assert [posting.id for posting in await opportunities.list_unlinked()] == \
        [OPPORTUNITY]
    await opportunities.link_company(OPPORTUNITY, COMPANY)
    assert await opportunities.list_unlinked() == ()


async def test_a_posting_with_no_employer_name_is_never_offered_as_a_seed(
        opportunities, db_session, stored_company):
    """There is nothing to resolve, and returning it would put an unresolvable row
    at the head of every pass forever."""
    await opportunities.upsert(an_opportunity())
    db_session.add(OpportunityRow(id=NAMELESS_POSTING, company_name="",
                                  title="Anonymous posting", discovered_at=EARLIER))
    await db_session.flush()
    assert [posting.id for posting in await opportunities.list_unlinked()] == \
        [OPPORTUNITY]


async def test_the_backlog_comes_back_oldest_first(opportunities, stored_company):
    """So repeated bounded passes work through it instead of re-reading one page."""
    await opportunities.upsert(an_opportunity(discovered_at=LATER))
    await opportunities.upsert(an_opportunity(
        id=SECOND_POSTING, source=a_source_record(external_id="posting-2"),
        dedup_fingerprint="fingerprint-2", discovered_at=EARLIER))
    assert [posting.id for posting in await opportunities.list_unlinked()] == \
        [SECOND_POSTING, OPPORTUNITY]


async def test_linking_a_posting_leaves_the_name_the_board_published(
        opportunities, stored_company):
    """§13: the posting's original company name remains provenance.

    A property of the UPDATE rather than a rule somebody has to remember — there
    is no argument to `link_company` through which the name could be overwritten.
    """
    await opportunities.upsert(an_opportunity(company_name="LOGITECH"))
    assert await opportunities.link_company(OPPORTUNITY, COMPANY) is True
    linked = await opportunities.get(OPPORTUNITY)
    assert linked is not None
    assert linked.company_id == COMPANY
    assert linked.company_name == "LOGITECH"


async def test_resolving_the_same_posting_twice_changes_nothing(
        opportunities, stored_company):
    """§23 for the link itself: the second pass reports no change rather than
    rewriting the value that is already there."""
    await opportunities.upsert(an_opportunity())
    assert await opportunities.link_company(OPPORTUNITY, COMPANY) is True
    assert await opportunities.link_company(OPPORTUNITY, COMPANY) is False
