# tests/test_v2_persistence_repositories.py
"""What the repository contracts promise, asserted against PostgreSQL.

Four properties the application is about to depend on, none of which a unit test
with a fake could establish: an upsert really is an upsert (a retried write
updates one row instead of adding a second), a user-scoped read cannot see
another user's row, an identifier survives storage as a native UUID, and an
instant survives it as the same instant.

Every test runs inside the transaction `db_session` opened and will roll back —
except the three `session_scope` ones at the end. `session_scope` *is* the commit
boundary, so proving that it commits means letting it, and those clean up after
themselves.
"""
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from backend.app.domain.common import Location, Reason, ReasonImpact
from backend.app.domain.identifiers import (
    CandidateProfileId,
    CompanyLocationId,
    EvidenceId,
    MatchEvaluationId,
    OpportunityId,
)
from backend.app.domain.matching import DimensionScore, MatchDimension
from backend.app.infrastructure.database.engine import (
    create_session_factory,
    session_scope,
)
from backend.app.infrastructure.database.models import (
    CandidateProfileRow,
    CompanyLocationRow,
    MatchDimensionScoreRow,
    MatchEvaluationRow,
    OpportunityRow,
    OpportunitySourceRecordRow,
    UserRow,
)
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyCompanyRepository,
    SqlAlchemyMatchEvaluationRepository,
    SqlAlchemyOpportunityRepository,
)
from tests.v2_builders import (
    COMPANY,
    COMPANY_LOCATION,
    EVALUATION,
    GENEVA,
    LATER,
    NOW,
    OPPORTUNITY,
    OTHER_PROFILE,
    OTHER_USER,
    PROFILE,
    USER,
    a_company,
    a_company_location,
    a_source_record,
    an_evaluation,
    an_opportunity,
)
from tests.v2_rows import a_candidate_profile_row, a_user_row

# Every test in this module is async, and pytest-asyncio runs in strict mode
# (pyproject.toml), so the marker is applied once here rather than on 20 functions.
pytestmark = pytest.mark.asyncio

# Identities the shared builders do not need to know about: a second site, a
# second profile for the *same* user, and the two extra evaluations the
# ownership tests compare against.
SECOND_LOCATION = CompanyLocationId(UUID("00000000-0000-4000-8000-000000000036"))
SECOND_PROFILE = CandidateProfileId(UUID("00000000-0000-4000-8000-000000000013"))
SECOND_EVALUATION = MatchEvaluationId(UUID("00000000-0000-4000-8000-000000000042"))
FOREIGN_EVALUATION = MatchEvaluationId(UUID("00000000-0000-4000-8000-000000000043"))
EVIDENCE = EvidenceId(UUID("00000000-0000-4000-8000-000000000071"))

async def _count(session, model) -> int:
    """How many rows of `model` the current transaction can see."""
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


def a_posting(index: int, **overrides):
    """Posting number `index`, distinct in every column the schema makes unique.

    Three unique keys have to differ for two postings to coexist — the primary
    key, `dedup_fingerprint` and `(source_key, external_id)` — and a test that
    varied only one of them would fail for the wrong reason.
    """
    fields = {
        "id": OpportunityId(UUID(f"00000000-0000-4000-8000-0000000001{index:02d}")),
        "source": a_source_record(external_id=f"posting-{index}"),
        "dedup_fingerprint": f"fingerprint-{index}",
        "title": f"Posting {index}",
    }
    fields.update(overrides)
    return an_opportunity(**fields)


@pytest.fixture
def opportunities(db_session):
    return SqlAlchemyOpportunityRepository(db_session)


@pytest.fixture
def companies(db_session):
    return SqlAlchemyCompanyRepository(db_session)


@pytest.fixture
def evaluations(db_session):
    return SqlAlchemyMatchEvaluationRepository(db_session)


@pytest_asyncio.fixture
async def evaluation_prerequisites(db_session, opportunities):
    """The rows an evaluation needs a foreign key to.

    `users` and `candidate_profiles` are written as ORM rows rather than through a
    repository because there is no repository for either: `AuthenticationService` and
    `OnboardingService` own what a user and a profile are, and what these tests need
    is only a valid foreign-key target (`tests/v2_rows.py`).
    """
    db_session.add_all([
        a_user_row(display_name="owner"),
        a_user_row(id=OTHER_USER, display_name="somebody else"),
    ])
    # Flushed before the profiles rather than added alongside them: no
    # `relationship()` joins the two tables — the profile's owner is a plain column
    # — so SQLAlchemy's unit of work has no dependency edge to sort the inserts by
    # and orders them by mapper name, which puts `candidate_profiles` first and
    # violates the foreign key.
    await db_session.flush()
    db_session.add_all([
        a_candidate_profile_row(display_name="graduate"),
        a_candidate_profile_row(id=SECOND_PROFILE, display_name="student"),
        a_candidate_profile_row(id=OTHER_PROFILE, user_id=OTHER_USER,
                                display_name="student"),
    ])
    await opportunities.upsert(an_opportunity())
    await db_session.flush()


async def test_an_opportunity_comes_back_exactly_as_it_went_in(opportunities):
    """One assertion over every column group, because equality is the whole point.

    The builder populates salary, workload, location, language requirements and a
    raw payload precisely so that a mapper which dropped one of them fails here.
    `Opportunity` is a frozen Pydantic model, so `==` compares every field.
    """
    written = await opportunities.upsert(an_opportunity())
    assert written == an_opportunity()
    assert await opportunities.get(OPPORTUNITY) == an_opportunity()


async def test_upserting_the_same_id_twice_updates_one_row(db_session, opportunities):
    """The property that makes a failed import safe to re-run.

    Both the opportunity and its source record must be updated in place: a second
    `opportunity_source_records` row would violate `UNIQUE (opportunity_id)`, and
    it is the derived surrogate key that prevents it.
    """
    await opportunities.upsert(an_opportunity())
    await opportunities.upsert(an_opportunity(title="Ingenieure logicielle"))
    stored = await opportunities.get(OPPORTUNITY)
    assert stored is not None
    assert stored.title == "Ingenieure logicielle"
    assert await _count(db_session, OpportunityRow) == 1
    assert await _count(db_session, OpportunitySourceRecordRow) == 1


async def test_get_by_source_answers_have_i_seen_this_posting(opportunities):
    """The idempotency lookup, which does not depend on how the id was derived."""
    await opportunities.upsert(an_opportunity())
    found = await opportunities.get_by_source("test_board", "posting-1")
    assert found is not None
    assert found.id == OPPORTUNITY
    assert await opportunities.get_by_source("test_board", "posting-404") is None
    assert await opportunities.get_by_source("other_board", "posting-1") is None


async def test_get_by_fingerprint_finds_the_posting_whatever_found_it(opportunities):
    await opportunities.upsert(an_opportunity())
    found = await opportunities.get_by_fingerprint("fingerprint-1")
    assert found is not None
    assert found.id == OPPORTUNITY
    assert await opportunities.get_by_fingerprint("fingerprint-unknown") is None


async def test_a_second_id_cannot_claim_a_fingerprint_that_is_taken(opportunities):
    """Two ids for one posting is the duplicate the fingerprint exists to prevent.

    The repository flushes, so the violation arrives at the `upsert` that caused
    it — which is what lets the V1 importer attribute it to one row and carry on.
    """
    await opportunities.upsert(an_opportunity())
    with pytest.raises(IntegrityError, match="uq_opportunities_dedup_fingerprint"):
        await opportunities.upsert(a_posting(2, dedup_fingerprint="fingerprint-1"))


async def test_list_recent_is_newest_first_and_respects_its_cap(opportunities):
    """The shape of the feed, and the cap that keeps it from becoming an outage."""
    for index, discovered_at in enumerate((NOW, LATER, datetime(2026, 3, 3, tzinfo=UTC))):
        await opportunities.upsert(a_posting(index + 1, discovered_at=discovered_at))
    listed = await opportunities.list_recent()
    assert [posting.title for posting in listed] == ["Posting 3", "Posting 2", "Posting 1"]
    assert len(await opportunities.list_recent(limit=2)) == 2


async def test_an_unknown_id_is_none_rather_than_an_error(opportunities):
    """A miss is a value, not an exception — every caller has to handle it anyway."""
    assert await opportunities.get(OPPORTUNITY) is None


async def test_an_identifier_is_stored_as_a_native_uuid(db_session, opportunities):
    """`uuid`, not `char(36)`: asked of PostgreSQL rather than of the metadata.

    The metadata test asserts the declaration; this asserts what the migration
    actually built, and that the value comes back as a `UUID` object instead of
    the string psycopg would hand back for a text column.
    """
    stored = await opportunities.upsert(an_opportunity())
    assert isinstance(stored.id, UUID)
    stored_type = await db_session.execute(
        text("SELECT pg_typeof(id)::text FROM opportunities WHERE id = :id"),
        {"id": str(OPPORTUNITY)})
    assert stored_type.scalar_one() == "uuid"


async def test_an_aware_non_utc_instant_is_stored_as_the_same_instant(db_session):
    """The timezone policy, end to end: 10:30 in Zurich is 09:30 UTC.

    Written as an ORM row because the domain's `UtcDatetime` already normalizes on
    the way in, so a test that went through `Opportunity` would be asserting on
    Pydantic. What is under test here is the column type: whatever zone a caller
    hands to psycopg, what comes back out is UTC and compares equal to what went
    in — which is what makes a stored instant unambiguous.
    """
    zurich = datetime(2026, 3, 1, 10, 30, tzinfo=ZoneInfo("Europe/Zurich"))
    db_session.add(a_user_row(created_at=zurich, updated_at=zurich))
    await db_session.flush()
    db_session.expunge_all()
    result = await db_session.execute(select(UserRow).where(UserRow.id == USER))
    row = result.scalar_one()
    assert row.created_at == zurich
    assert row.created_at.tzinfo is UTC
    assert row.created_at == datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


async def test_a_naive_datetime_is_refused_at_the_driver_boundary(db_session):
    """V1's bug, made unrepresentable.

    `datetime.now()` without a zone is what V1 stores; here it never reaches the
    database at all. The `ValueError` surfaces wrapped in `StatementError` because
    SQLAlchemy raises it while binding parameters, with the statement attached.
    """
    naive = datetime(2026, 3, 1, 10, 30)
    db_session.add(a_user_row(created_at=naive, updated_at=naive))
    with pytest.raises(StatementError, match="refusing to store a naive datetime"):
        await db_session.flush()


async def test_a_company_comes_back_with_its_sites(companies):
    written = await companies.upsert(a_company())
    assert written == a_company()
    assert await companies.get(COMPANY) == a_company()


async def test_upserting_a_company_reconciles_the_sites_it_no_longer_has(
        db_session, companies):
    """A site dropped from the domain object is deleted, not orphaned.

    This is the one place where an upsert deletes something, so the contract says
    it returns what is now stored: a caller learns that the branch it omitted is
    gone. `delete-orphan` on the relationship is what performs it.
    """
    branch = a_company_location(id=SECOND_LOCATION, is_headquarters=False,
                                location=Location(country="CH", city="Geneve",
                                                  point=GENEVA))
    await companies.upsert(a_company(a_company_location(), branch))
    assert await _count(db_session, CompanyLocationRow) == 2

    remaining = await companies.upsert(a_company(a_company_location()))
    assert [site.id for site in remaining.locations] == [COMPANY_LOCATION]
    assert await _count(db_session, CompanyLocationRow) == 1


async def test_a_site_that_stays_keeps_its_identity_and_takes_the_new_values(
        db_session, companies):
    """Matched by id, so an update is an UPDATE — not a delete and an insert.

    A site re-inserted under a new primary key would break every map marker and
    every future reference to it, and `created_at` is the witness: it is set by
    the database on insert and must not move when the row is merely updated.
    """
    await companies.upsert(a_company())
    original = await db_session.execute(
        select(CompanyLocationRow.created_at)
        .where(CompanyLocationRow.id == COMPANY_LOCATION))
    written_at = original.scalar_one()

    moved = a_company_location(location=Location(country="CH", city="Renens"))
    stored = await companies.upsert(a_company(moved))
    assert stored.locations[0].location.city == "Renens"
    unchanged = await db_session.execute(
        select(CompanyLocationRow.created_at)
        .where(CompanyLocationRow.id == COMPANY_LOCATION))
    assert unchanged.scalar_one() == written_at


async def test_an_evaluation_and_its_reasons_survive_the_round_trip(
        evaluation_prerequisites, evaluations):
    """Including the evidence ids inside the JSONB payload.

    `Reason.evidence_ids` is what makes a score auditable, and JSON has no UUID
    type: the mapper serializes them as strings and validates them back. A test
    that only compared codes would not notice them coming back as `str`.
    """
    reason = Reason(code="SKILLS_MATCH", detail="Python and PostgreSQL",
                    impact=ReasonImpact.POSITIVE, evidence_ids=(EVIDENCE,))
    evaluation = an_evaluation(
        reasons=(reason,), evidence_confidence=0.8, evaluator_key="deterministic_v1",
        dimensions=(DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.92,
                                   weight=0.75, reasons=(reason,)),))
    assert await evaluations.upsert(evaluation) == evaluation

    stored = await evaluations.get(USER, EVALUATION)
    assert stored == evaluation
    assert stored.reasons[0].evidence_ids == (EVIDENCE,)
    assert isinstance(stored.reasons[0].evidence_ids[0], UUID)


async def test_another_users_evaluation_is_reported_as_absent(
        evaluation_prerequisites, evaluations):
    """Not found and not yours are deliberately indistinguishable.

    A caller able to tell them apart could enumerate another user's rows by id,
    which is the cross-user leak docs/ENGINEERING_STANDARDS.md §Security forbids.
    """
    await evaluations.upsert(an_evaluation())
    assert await evaluations.get(USER, EVALUATION) is not None
    assert await evaluations.get(OTHER_USER, EVALUATION) is None
    assert await evaluations.get_for_pair(OTHER_USER, PROFILE, OPPORTUNITY) is None


async def test_re_evaluating_a_pair_updates_the_dimension_rows(
        db_session, evaluation_prerequisites, evaluations):
    """Six scores re-computed must not become twelve rows.

    The child key is derived from `(evaluation, dimension)`, so a second
    evaluation of the same pair lands on the same rows — and a dimension that is
    no longer computed is deleted rather than left behind as a stale score.
    """
    both = (DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.92),
            DimensionScore(dimension=MatchDimension.LOCATION_FIT, score=0.40))
    await evaluations.upsert(an_evaluation(dimensions=both))
    assert await _count(db_session, MatchDimensionScoreRow) == 2

    stored = await evaluations.upsert(an_evaluation(dimensions=both[:1]))
    assert [entry.dimension for entry in stored.dimensions] == [MatchDimension.SKILLS_FIT]
    assert await _count(db_session, MatchDimensionScoreRow) == 1
    assert await _count(db_session, MatchEvaluationRow) == 1


async def test_a_users_list_holds_only_their_own_evaluations(
        evaluation_prerequisites, evaluations):
    """The Phase 4 authorization filter, already true in Phase 2.

    Two users evaluate the same posting, which is exactly the situation a shared
    `opportunities` table creates — and the reason `list_for_user` takes the owner
    as its first argument rather than reading it from ambient state.
    """
    mine = an_evaluation()
    newer = an_evaluation(id=SECOND_EVALUATION, candidate_profile_id=SECOND_PROFILE,
                          evaluated_at=LATER)
    theirs = an_evaluation(id=FOREIGN_EVALUATION, user_id=OTHER_USER,
                           candidate_profile_id=OTHER_PROFILE)
    for evaluation in (mine, newer, theirs):
        await evaluations.upsert(evaluation)

    assert [row.id for row in await evaluations.list_for_user(USER)] == [
        SECOND_EVALUATION, EVALUATION]
    assert [row.id for row in await evaluations.list_for_user(OTHER_USER)] == [
        FOREIGN_EVALUATION]
    assert len(await evaluations.list_for_user(USER, limit=1)) == 1


@pytest_asyncio.fixture
async def committing_factory(db_engine):
    """A session factory whose commits really commit — and get cleaned up.

    Every other test in this module runs inside a transaction that is rolled back,
    which is precisely what would make `session_scope`'s commit unobservable. So
    these three get their own factory, and this fixture empties what they wrote:
    the test database is the suite's to empty (`DatabaseSettings.for_tests`), and a
    committed row left behind would be visible to every later `list_recent`.
    """
    factory = create_session_factory(db_engine)
    try:
        yield factory
    finally:
        async with db_engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE opportunities, companies, users CASCADE"))


async def test_session_scope_commits_what_the_block_wrote(committing_factory):
    """The one place in the V2 backend that commits, doing so."""
    async with session_scope(committing_factory) as session:
        await SqlAlchemyOpportunityRepository(session).upsert(an_opportunity())
    async with session_scope(committing_factory, commit=False) as session:
        assert await SqlAlchemyOpportunityRepository(session).get(OPPORTUNITY) is not None


async def test_an_exception_leaves_nothing_behind(committing_factory):
    """"Import 400 rows or none of them", which is why repositories never commit.

    The write has already been flushed when the block raises — the row exists as
    far as PostgreSQL is concerned — and it still has to disappear.
    """
    class Interrupted(RuntimeError):
        """Anything at all going wrong halfway through a unit of work."""

    with pytest.raises(Interrupted):
        async with session_scope(committing_factory) as session:
            await SqlAlchemyOpportunityRepository(session).upsert(an_opportunity())
            raise Interrupted
    async with session_scope(committing_factory, commit=False) as session:
        assert await SqlAlchemyOpportunityRepository(session).get(OPPORTUNITY) is None


async def test_commit_false_exercises_the_real_schema_and_keeps_nothing(
        committing_factory):
    """`import-v1 --dry-run`, which is this and nothing else.

    Every constraint, every CHECK and every column type is exercised against the
    real database — the INSERT runs and the row is readable inside the
    transaction — and then none of it is kept. That is what makes a rehearsal
    trustworthy: it fails on the same things the real run would.
    """
    async with session_scope(committing_factory, commit=False) as session:
        repository = SqlAlchemyOpportunityRepository(session)
        await repository.upsert(an_opportunity())
        assert await repository.get(OPPORTUNITY) is not None
    async with session_scope(committing_factory, commit=False) as session:
        assert await SqlAlchemyOpportunityRepository(session).get(OPPORTUNITY) is None
