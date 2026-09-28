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
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from backend.app.core.tokens import digest_of, new_token
from backend.app.domain.account_export import AccountExportStatus
from backend.app.domain.common import Location, Reason, ReasonImpact
from backend.app.domain.chat import ChatActionProposalStatus, ChatMessageRole
from backend.app.domain.application import ApplicationState
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.outcome import OutcomeKind, OutcomeStatus
from backend.app.domain.role import RoleFamily, RoleFamilyProvenance
from backend.app.domain.strategy_change import StrategyChangeProposalStatus
from backend.app.domain.eligibility import (
    DeterminationSource,
    EligibilityCheck,
    EligibilityRequirement,
    EligibilityStatus,
    RuleAuthority,
)
from backend.app.domain.identifiers import (
    AccountExportId,
    CandidateProfileId,
    CompanyLocationId,
    ConversationId,
    EligibilityResultId,
    EvidenceId,
    MatchEvaluationId,
    OpportunityId,
    new_user_session_id,
)
from backend.app.domain.matching import DimensionScore, MatchDimension
from backend.app.domain.entitlement import EntitlementKey
from backend.app.domain.subscription import SubscriptionStatus
from backend.app.domain.subscription_event import SubscriptionEventOutcome
from backend.app.domain.usage import UsageSourceType
from backend.app.domain.user import UserSession
from backend.app.infrastructure.database.engine import (
    create_session_factory,
    session_scope,
)
from backend.app.infrastructure.database.models import (
    AccountExportRow,
    ApplicationOutcomeRow,
    ApplicationRow,
    CandidateProfileRow,
    CareerRecommendationEvidenceRow,
    CareerRecommendationRow,
    ChatActionExecutionRow,
    ChatActionProposalRow,
    ChatMessageRow,
    CompanyLocationRow,
    ConversationRow,
    EligibilityCheckRow,
    EligibilityResultRow,
    MatchDimensionScoreRow,
    MatchEvaluationRow,
    OpportunityRow,
    OpportunitySourceRecordRow,
    PlanEntitlementRow,
    PlanRow,
    RoleClassificationRow,
    StrategyChangeExecutionRow,
    StrategyChangeProposalRow,
    SubscriptionRow,
    SubscriptionEventRow,
    UsageEventRow,
    UserRow,
)
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyAccountExportRepository,
    SqlAlchemyApplicationDecisionRepository,
    SqlAlchemyApplicationOutcomeRepository,
    SqlAlchemyApplicationPolicyRepository,
    SqlAlchemyCandidateDocumentRepository,
    SqlAlchemyCareerRecommendationRepository,
    SqlAlchemyChatActionExecutionRepository,
    SqlAlchemyChatActionProposalRepository,
    SqlAlchemyChatMessageRepository,
    SqlAlchemyCompanyRepository,
    SqlAlchemyConversationRepository,
    SqlAlchemyEligibilityResultRepository,
    SqlAlchemyMatchEvaluationRepository,
    SqlAlchemyOpportunityRepository,
    SqlAlchemyPlanRepository,
    SqlAlchemyRoleClassificationRepository,
    SqlAlchemySessionRepository,
    SqlAlchemyStrategyChangeExecutionRepository,
    SqlAlchemyStrategyChangeProposalRepository,
    SqlAlchemySubscriptionRepository,
    SqlAlchemySubscriptionEventRepository,
    SqlAlchemyUsageEventRepository,
    SqlAlchemyUserRepository,
)
from tests.v2_builders import (
    ACCOUNT_EXPORT,
    APPLICATION,
    COMPANY,
    COMPANY_LOCATION,
    CONVERSATION,
    ELIGIBILITY,
    EVALUATION,
    GENEVA,
    LATER,
    NOW,
    OPPORTUNITY,
    OTHER_CONVERSATION,
    OTHER_OPPORTUNITY,
    OTHER_PROFILE,
    OTHER_RECOMMENDATION,
    OTHER_STRATEGY_PROPOSAL,
    OTHER_SUBSCRIPTION,
    OTHER_SUBSCRIPTION_EVENT,
    OTHER_USER,
    POLICY,
    PRO_PLAN,
    PROFILE,
    RECOMMENDATION,
    RUN,
    STRATEGY_PROPOSAL,
    SUBSCRIPTION,
    SUBSCRIPTION_EVENT,
    USER,
    a_career_recommendation,
    a_chat_action_execution,
    a_chat_action_proposal,
    a_chat_message,
    a_check,
    a_company,
    a_company_location,
    a_conversation,
    a_decision,
    a_plan,
    a_policy,
    a_recommendation_evidence,
    a_rendered_document,
    a_role_classification,
    a_source_record,
    a_strategy_change_execution,
    a_strategy_change_proposal,
    a_subscription,
    a_subscription_event,
    a_usage_event,
    an_account_export,
    an_eligibility_result,
    an_entitlement,
    an_evaluation,
    an_opportunity,
    an_application_outcome,
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
SECOND_ELIGIBILITY = EligibilityResultId(UUID("00000000-0000-4000-8000-000000000046"))
FOREIGN_ELIGIBILITY = EligibilityResultId(UUID("00000000-0000-4000-8000-000000000047"))
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


@pytest.fixture
def eligibility_results(db_session):
    return SqlAlchemyEligibilityResultRepository(db_session)


async def test_an_eligibility_result_and_its_checks_survive_the_round_trip(
        evaluation_prerequisites, eligibility_results):
    """The whole verdict, read back by id and by the pair it covers.

    The second gate is a REVIEW_REQUIRED pack rule — the §59 case — so its
    authority, detail and the reason a non-ELIGIBLE gate must carry are all on the
    path this asserts. `get_for_pair` is the lookup the assessment service uses to
    find a prior verdict before re-running one.
    """
    verdict = an_eligibility_result(
        a_check(),
        EligibilityCheck(
            requirement=EligibilityRequirement.PERMIT_HOURS_CAP,
            status=EligibilityStatus.REVIEW_REQUIRED,
            determined_by=DeterminationSource.COUNTRY_PACK_RULE,
            authority=RuleAuthority.OPERATOR_CONFIG,
            detail="Swiss student permit caps paid work at 15h/week.",
            reasons=(Reason(code="PERMIT_HOURS_CAP_REVIEW", detail="unverified cap",
                            impact=ReasonImpact.NEGATIVE),)))
    assert await eligibility_results.upsert(verdict) == verdict
    assert await eligibility_results.get(USER, ELIGIBILITY) == verdict
    assert await eligibility_results.get_for_pair(USER, PROFILE, OPPORTUNITY) == verdict


async def test_another_users_eligibility_result_is_reported_as_absent(
        evaluation_prerequisites, eligibility_results):
    """Not found and not yours are indistinguishable, the same as for evaluations.

    A caller able to tell them apart could enumerate another user's verdicts by id
    — the cross-user leak docs/ENGINEERING_STANDARDS.md §Security forbids — and
    whether someone may apply is exactly the kind of row that must not leak.
    """
    await eligibility_results.upsert(an_eligibility_result())
    assert await eligibility_results.get(USER, ELIGIBILITY) is not None
    assert await eligibility_results.get(OTHER_USER, ELIGIBILITY) is None
    assert await eligibility_results.get_for_pair(
        OTHER_USER, PROFILE, OPPORTUNITY) is None


async def test_re_evaluating_a_pair_updates_the_check_rows(
        db_session, evaluation_prerequisites, eligibility_results):
    """Two gates re-evaluated as one must not become three rows.

    The child key is `(result, ordinal)`, so re-evaluating the same pair lands on
    the same rows and a gate no longer produced is deleted rather than left behind
    as a stale check under a verdict that no longer rests on it.
    """
    await eligibility_results.upsert(an_eligibility_result(
        a_check(),
        a_check(requirement=EligibilityRequirement.LANGUAGE_MINIMUM,
                status=EligibilityStatus.INELIGIBLE)))
    assert await _count(db_session, EligibilityCheckRow) == 2

    stored = await eligibility_results.upsert(an_eligibility_result(a_check()))
    assert [check.requirement for check in stored.checks] == [
        EligibilityRequirement.WORK_AUTHORIZATION]
    assert await _count(db_session, EligibilityCheckRow) == 1
    assert await _count(db_session, EligibilityResultRow) == 1


async def test_a_users_list_holds_only_their_own_eligibility_results(
        evaluation_prerequisites, eligibility_results):
    """The authorization filter and the newest-first order, on the second axis.

    Two users get a verdict on the same posting — what a shared `opportunities`
    table guarantees — so `list_for_user` takes the owner as its first argument
    rather than reading it from ambient state, exactly as the evaluation list does.
    """
    mine = an_eligibility_result()
    newer = an_eligibility_result(id=SECOND_ELIGIBILITY,
                                  candidate_profile_id=SECOND_PROFILE,
                                  determined_at=LATER)
    theirs = an_eligibility_result(id=FOREIGN_ELIGIBILITY, user_id=OTHER_USER,
                                   candidate_profile_id=OTHER_PROFILE)
    for verdict in (mine, newer, theirs):
        await eligibility_results.upsert(verdict)

    assert [row.id for row in await eligibility_results.list_for_user(USER)] == [
        SECOND_ELIGIBILITY, ELIGIBILITY]
    assert [row.id for row in await eligibility_results.list_for_user(OTHER_USER)] == [
        FOREIGN_ELIGIBILITY]
    assert len(await eligibility_results.list_for_user(USER, limit=1)) == 1


async def test_the_stored_verdict_carries_a_denormalized_status_column(
        db_session, evaluation_prerequisites, eligibility_results):
    """The column a list ranks by holds the derived verdict, not a per-check status.

    `EligibilityResult.status` is worst-of the checks and is a property, not a
    field; the repository persists a denormalized copy so a list can filter and rank
    without loading every check. This reads the column straight from the table to
    prove the copy is the aggregate the domain computed — a passing gate beside an
    ineligible one still stores INELIGIBLE.
    """
    await eligibility_results.upsert(an_eligibility_result(
        a_check(status=EligibilityStatus.ELIGIBLE),
        a_check(requirement=EligibilityRequirement.LANGUAGE_MINIMUM,
                status=EligibilityStatus.INELIGIBLE)))
    stored = await db_session.execute(select(EligibilityResultRow.status).where(
        EligibilityResultRow.id == ELIGIBILITY))
    assert stored.scalar_one() == EligibilityStatus.INELIGIBLE


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


# --- Phase 13 career chat ---------------------------------------------------
# A conversation and its turns need only a `users` row to point at; the derived
# ids mean a proposal attaches to the assistant turn at sequence 1 without the
# test spelling the id out.


@pytest.fixture
def conversations(db_session):
    return SqlAlchemyConversationRepository(db_session)


@pytest.fixture
def chat_messages(db_session):
    return SqlAlchemyChatMessageRepository(db_session)


@pytest.fixture
def proposals(db_session):
    return SqlAlchemyChatActionProposalRepository(db_session)


@pytest.fixture
def executions(db_session):
    return SqlAlchemyChatActionExecutionRepository(db_session)


@pytest_asyncio.fixture
async def chat_users(db_session):
    """The two accounts every chat scoping test compares — the owner and a stranger."""
    db_session.add_all([
        a_user_row(display_name="owner"),
        a_user_row(id=OTHER_USER, display_name="somebody else"),
    ])
    await db_session.flush()


@pytest_asyncio.fixture
async def chat_thread(chat_users, conversations, chat_messages):
    """A conversation with a user turn (0) and an assistant turn (1) a proposal hangs off.

    The assistant turn carries no `llm_run_id`, which the `user_has_no_run` CHECK
    permits (it only forbids a *user* turn from carrying one) and which keeps the
    thread free of a foreign key to `llm_runs` no proposal test needs.
    """
    await conversations.upsert(a_conversation())
    await chat_messages.upsert(a_chat_message(sequence=0))
    await chat_messages.upsert(a_chat_message(
        sequence=1, role=ChatMessageRole.ASSISTANT, content="Voici ce que je propose."))


async def test_a_conversation_comes_back_scoped_to_its_owner(chat_users, conversations):
    """Round trip by id, and a stranger reading it gets `None`, not the row.

    A conversation is user data, so `get` takes the owner first and a caller naming
    another account's thread cannot tell "absent" from "not yours" — the same
    cross-user rule the evaluation and eligibility reads hold.
    """
    stored = await conversations.upsert(a_conversation())
    assert stored == a_conversation()
    assert await conversations.get(USER, CONVERSATION) == a_conversation()
    assert await conversations.get(OTHER_USER, CONVERSATION) is None


async def test_upserting_a_conversation_twice_updates_one_row(
        db_session, chat_users, conversations):
    """Renaming a thread or marking it read updates the row rather than adding one."""
    await conversations.upsert(a_conversation())
    await conversations.upsert(a_conversation(title="Ma recherche d'emploi",
                                              is_archived=True))
    stored = await conversations.get(USER, CONVERSATION)
    assert stored is not None
    assert stored.title == "Ma recherche d'emploi" and stored.is_archived is True
    assert await _count(db_session, ConversationRow) == 1


async def test_the_conversation_list_is_newest_first_and_hides_archived_threads(
        chat_users, conversations):
    """The sidebar order, and the archive filter that keeps a closed thread out of it.

    `list_for_user` orders by the last activity descending, and excludes archived
    threads unless asked — an archived one is hidden, never deleted, so its audit
    trail survives and `include_archived` can still reach it.
    """
    latest = datetime(2026, 3, 3, 9, 30, tzinfo=UTC)
    await conversations.upsert(a_conversation(last_message_at=NOW))
    await conversations.upsert(a_conversation(id=OTHER_CONVERSATION,
                                              last_message_at=LATER))
    archived = ConversationId(UUID("00000000-0000-4000-8000-0000000000c3"))
    await conversations.upsert(a_conversation(id=archived, is_archived=True,
                                              last_message_at=latest))

    active = await conversations.list_for_user(USER)
    assert [row.id for row in active] == [OTHER_CONVERSATION, CONVERSATION]
    everything = await conversations.list_for_user(USER, include_archived=True)
    assert [row.id for row in everything] == [archived, OTHER_CONVERSATION, CONVERSATION]


async def test_a_thread_with_no_turns_yet_sorts_by_its_updated_at(
        chat_users, conversations):
    """`last_message_at` is `None` until the first turn, so the list coalesces to
    `updated_at` — a freshly opened thread sorts by when it was created, ahead of an
    older thread whose last turn predates it, rather than sinking to the bottom."""
    await conversations.upsert(a_conversation(last_message_at=LATER))
    fresh = datetime(2026, 3, 4, 9, 30, tzinfo=UTC)
    await conversations.upsert(a_conversation(id=OTHER_CONVERSATION,
                                              last_message_at=None,
                                              created_at=fresh, updated_at=fresh))
    listed = await conversations.list_for_user(USER)
    assert [row.id for row in listed] == [OTHER_CONVERSATION, CONVERSATION]


async def test_latest_sequence_is_the_max_turn_the_owner_can_see(
        chat_thread, chat_messages):
    """The counter the service numbers the next turn from, without loading the thread.

    A conversation with turns 0 and 1 has a latest sequence of 1; one the owner has
    no turn in — either empty or another account's — is `None`, so the first turn a
    caller writes is numbered 0.
    """
    assert await chat_messages.latest_sequence(USER, CONVERSATION) == 1
    assert await chat_messages.latest_sequence(OTHER_USER, CONVERSATION) is None
    assert await chat_messages.latest_sequence(USER, OTHER_CONVERSATION) is None


async def test_messages_come_back_in_sequence_order_scoped_to_owner(
        chat_thread, chat_messages):
    """The transcript, oldest turn first, and only the owner's.

    `list_for_conversation` orders by `sequence` and scopes on the message's own
    `user_id`, so a stranger asking for the same conversation id gets an empty
    transcript rather than someone else's turns.
    """
    turns = await chat_messages.list_for_conversation(USER, CONVERSATION)
    assert [turn.sequence for turn in turns] == [0, 1]
    assert [turn.role for turn in turns] == [ChatMessageRole.USER,
                                             ChatMessageRole.ASSISTANT]
    newest_first = await chat_messages.list_for_conversation(
        USER, CONVERSATION, newest_first=True)
    assert [turn.sequence for turn in newest_first] == [1, 0]
    assert await chat_messages.list_for_conversation(OTHER_USER, CONVERSATION) == ()


async def test_a_proposal_is_reported_as_absent_to_another_user(chat_thread, proposals):
    """The scoped `get` the executor runs before acting.

    A confirmation naming another account's proposal must read as absent — that is
    what stops the chat from being a way to drive someone else's platform, the whole
    point of "proposal is not permission".
    """
    proposal = await proposals.upsert(a_chat_action_proposal())
    assert await proposals.get(USER, proposal.id) == proposal
    assert await proposals.get(OTHER_USER, proposal.id) is None


async def test_confirming_a_proposal_updates_the_one_row(
        db_session, chat_thread, proposals):
    """A confirm moves `status` off `PROPOSED` on the row already there.

    Keyed on the derived id, so re-finalizing the turn or recording the confirm
    updates the single proposal rather than adding a second — `is_open` flips to
    `False` and the count stays one.
    """
    proposal = await proposals.upsert(a_chat_action_proposal())
    assert proposal.is_open is True
    confirmed = await proposals.upsert(a_chat_action_proposal(
        status=ChatActionProposalStatus.EXECUTED))
    assert confirmed.status is ChatActionProposalStatus.EXECUTED
    assert confirmed.is_open is False
    assert await _count(db_session, ChatActionProposalRow) == 1


async def test_the_proposal_list_scopes_to_owner(chat_thread, proposals):
    """The conversation's proposals, and only for the account that owns them."""
    await proposals.upsert(a_chat_action_proposal())
    mine = await proposals.list_for_conversation(USER, CONVERSATION)
    assert [proposal.action.kind for proposal in mine] == [
        a_chat_action_proposal().action.kind]
    assert await proposals.list_for_conversation(OTHER_USER, CONVERSATION) == ()


async def test_an_execution_is_written_once_per_proposal(
        db_session, chat_thread, proposals, executions):
    """A double-confirm collapses onto one audit row rather than running twice.

    The execution id is derived from the proposal alone, so a second upsert lands on
    the same row — the invariant that makes a repeated confirmation idempotent at the
    storage layer, beneath whatever the executor does.
    """
    await proposals.upsert(a_chat_action_proposal())
    await executions.upsert(a_chat_action_execution())
    await executions.upsert(a_chat_action_execution(detail="second confirm ignored"))
    assert await _count(db_session, ChatActionExecutionRow) == 1
    proposal = a_chat_action_proposal()
    stored = await executions.get(USER, proposal.id)
    assert stored is not None
    assert await executions.get(OTHER_USER, proposal.id) is None


# --- Phase 15 outcome tracking and the career-intelligence loop --------------
# Outcomes are real-world hiring facts kept well apart from the Phase 12 execution
# lifecycle (§2, §84): they hang off an `applications` row but touch no
# `ApplicationState`. Recommendations and their evidence are written whole and never
# mutated; a proposal is the one link an approval may flip off `PROPOSED`.


@pytest.fixture
def outcomes(db_session):
    return SqlAlchemyApplicationOutcomeRepository(db_session)


@pytest.fixture
def role_classifications(db_session):
    return SqlAlchemyRoleClassificationRepository(db_session)


@pytest.fixture
def recommendations(db_session):
    return SqlAlchemyCareerRecommendationRepository(db_session)


@pytest.fixture
def strategy_proposals(db_session):
    return SqlAlchemyStrategyChangeProposalRepository(db_session)


@pytest.fixture
def strategy_executions(db_session):
    return SqlAlchemyStrategyChangeExecutionRepository(db_session)


@pytest_asyncio.fixture
async def career_prerequisites(db_session):
    """The foreign-key targets the career loop hangs off: two accounts, a profile and
    two opportunities (a second so the by-role list has two rows to order)."""
    db_session.add_all([a_user_row(display_name="owner"),
                        a_user_row(id=OTHER_USER, display_name="somebody else")])
    await db_session.flush()
    db_session.add(a_candidate_profile_row(display_name="candidate"))
    await db_session.flush()
    postings = SqlAlchemyOpportunityRepository(db_session)
    await postings.upsert(an_opportunity())
    await postings.upsert(an_opportunity(
        id=OTHER_OPPORTUNITY, title="Data analyst",
        source=a_source_record(external_id="posting-2"),
        dedup_fingerprint="fingerprint-2"))
    await db_session.flush()


@pytest_asyncio.fixture
async def outcome_application(db_session, career_prerequisites):
    """A persisted `applications` row (id `APPLICATION`) an outcome can point at.

    Its FK chain — a policy and a decision — is seeded through their repositories, then
    the application itself is inserted directly at the id the outcome builder targets, so
    the hiring-process fact has a live row to reference without the execution engine.
    """
    policy = await SqlAlchemyApplicationPolicyRepository(db_session).upsert(a_policy())
    decision = await SqlAlchemyApplicationDecisionRepository(db_session).upsert(
        a_decision(policy_id=policy.id))
    db_session.add(ApplicationRow(
        id=APPLICATION, user_id=USER, candidate_profile_id=PROFILE,
        decision_id=decision.id, channel=ApplicationChannel.BROWSER.value,
        state=ApplicationState.PLANNED.value, idempotency_key="outcome-fixture-key",
        opportunity_id=OPPORTUNITY, policy_id=policy.id,
        created_at=NOW, updated_at=NOW))
    await db_session.flush()


async def test_an_outcome_is_reported_as_absent_to_another_user(
        outcome_application, outcomes):
    """A hiring fact is user data: a stranger naming its id reads `None`, not the row."""
    outcome = await outcomes.upsert(an_application_outcome())
    assert await outcomes.get(USER, outcome.id) == outcome
    assert await outcomes.get(OTHER_USER, outcome.id) is None


async def test_recording_the_same_milestone_twice_updates_one_row(
        db_session, outcome_application, outcomes):
    """The id derives from `(application_id, outcome_key)`, so a re-record collapses.

    A double-click or a retried request recording the same `INTERVIEW` at the same instant
    lands on the one row rather than inventing a second milestone.
    """
    await outcomes.upsert(an_application_outcome())
    await outcomes.upsert(an_application_outcome(detail="same milestone, retried"))
    assert await _count(db_session, ApplicationOutcomeRow) == 1


async def test_retracting_an_outcome_is_a_status_flip_not_a_delete(
        db_session, outcome_application, outcomes):
    """A mistake is corrected by flipping the row to `RETRACTED`, never by deleting it.

    The retraction keeps the same id, so the upsert lands on the row already there — the
    audit survives, and a funnel simply stops counting it (§9, §64).
    """
    outcome = await outcomes.upsert(an_application_outcome())
    await outcomes.upsert(outcome.retracted(at=LATER))
    assert await _count(db_session, ApplicationOutcomeRow) == 1
    stored = await outcomes.get(USER, outcome.id)
    assert stored is not None and stored.status is OutcomeStatus.RETRACTED


async def test_the_application_timeline_is_oldest_first(outcome_application, outcomes):
    """One application's outcomes come back oldest-first — the order a timeline reads."""
    await outcomes.upsert(an_application_outcome(kind=OutcomeKind.SCREEN, occurred_at=NOW))
    await outcomes.upsert(
        an_application_outcome(kind=OutcomeKind.INTERVIEW, occurred_at=LATER))
    timeline = await outcomes.list_for_application(USER, APPLICATION)
    assert [o.kind for o in timeline] == [OutcomeKind.SCREEN, OutcomeKind.INTERVIEW]


async def test_the_user_outcome_list_is_most_recently_occurred_first(
        outcome_application, outcomes):
    """The analytics input is most-recently-occurred first, and only the owner's."""
    await outcomes.upsert(an_application_outcome(kind=OutcomeKind.SCREEN, occurred_at=NOW))
    await outcomes.upsert(
        an_application_outcome(kind=OutcomeKind.INTERVIEW, occurred_at=LATER))
    listed = await outcomes.list_for_user(USER)
    assert [o.kind for o in listed] == [OutcomeKind.INTERVIEW, OutcomeKind.SCREEN]
    assert await outcomes.list_for_user(OTHER_USER) == ()


# PHASE15_REPOSITORIES_PART1


async def test_a_classification_is_scoped_to_the_user_who_made_it(
        career_prerequisites, role_classifications):
    """A role verdict is per-user: another account has not classified the opportunity."""
    stored = await role_classifications.upsert(a_role_classification())
    assert await role_classifications.get(USER, OPPORTUNITY) == stored
    assert await role_classifications.get(OTHER_USER, OPPORTUNITY) is None


async def test_re_classifying_an_opportunity_updates_the_one_row(
        db_session, career_prerequisites, role_classifications):
    """Keyed on `(user_id, opportunity_id)`, so a manual correction overwrites the row.

    A deterministic verdict the user then corrects by hand does not accrete a second row —
    the `MANUAL` provenance and its family replace the deterministic ones in place.
    """
    await role_classifications.upsert(a_role_classification())
    await role_classifications.upsert(a_role_classification(
        role_family=RoleFamily.DATA_AND_ANALYTICS,
        provenance=RoleFamilyProvenance.MANUAL, updated_at=LATER))
    assert await _count(db_session, RoleClassificationRow) == 1
    stored = await role_classifications.get(USER, OPPORTUNITY)
    assert stored is not None
    assert stored.provenance is RoleFamilyProvenance.MANUAL
    assert stored.role_family is RoleFamily.DATA_AND_ANALYTICS


async def test_the_classification_list_is_most_recently_updated_first(
        career_prerequisites, role_classifications):
    """The by-role list surfaces the freshest verdict first — most-recently-updated order."""
    await role_classifications.upsert(a_role_classification(updated_at=NOW))
    await role_classifications.upsert(a_role_classification(
        opportunity_id=OTHER_OPPORTUNITY, role_family=RoleFamily.DATA_AND_ANALYTICS,
        updated_at=LATER))
    listed = await role_classifications.list_for_user(USER)
    assert [c.opportunity_id for c in listed] == [OTHER_OPPORTUNITY, OPPORTUNITY]


async def test_a_recommendation_and_its_evidence_round_trip_scoped_to_owner(
        career_prerequisites, recommendations):
    """Add writes the aggregate whole; a scoped read brings back its evidence, or `None`.

    The evidence is `lazy="raise"`, so a forgotten eager-load would raise rather than
    silently drop the citations — the "evidence or nothing" guarantee survives the trip.
    """
    recommendation = a_career_recommendation(evidence=(
        a_recommendation_evidence(ordinal=0),
        a_recommendation_evidence(
            ordinal=1, dimension_key=RoleFamily.SOFTWARE_ENGINEERING.value,
            numerator=3, denominator=40, sample_size=40,
            detail="Software Engineering : 3 réponses sur 40 candidatures.")))
    stored = await recommendations.add(recommendation)
    assert stored == recommendation
    read_back = await recommendations.get(USER, recommendation.id)
    assert read_back == recommendation
    assert len(read_back.evidence) == 2
    assert await recommendations.get(OTHER_USER, recommendation.id) is None


async def test_the_recommendation_list_is_most_recently_created_first(
        career_prerequisites, recommendations):
    """Two *logically distinct* snapshots coexist — a recommendation is added, never taken over.

    They must be distinct: `(user_id, fingerprint)` is unique, so two recommendations resting on
    the same account, kind, window and cited evidence are one row, not two — the dedup the engine
    relies on, now enforced by the database. Here the second cites a different slice, so it is its
    own logical recommendation and both persist, newest first.
    """
    await recommendations.add(a_career_recommendation(created_at=NOW))
    await recommendations.add(a_career_recommendation(
        id=OTHER_RECOMMENDATION,
        evidence=(a_recommendation_evidence(
            recommendation_id=OTHER_RECOMMENDATION,
            dimension_key=RoleFamily.SOFTWARE_ENGINEERING.value,
            numerator=3, denominator=40, sample_size=40,
            detail="Software Engineering : 3 réponses sur 40 candidatures."),),
        created_at=LATER))
    listed = await recommendations.list_for_user(USER)
    assert [r.id for r in listed] == [OTHER_RECOMMENDATION, RECOMMENDATION]


async def test_a_strategy_proposal_is_reported_as_absent_to_another_user(
        career_prerequisites, strategy_proposals):
    """The scoped `get` the executor runs before applying a change.

    A confirmation naming another account's proposal reads as absent and is refused, rather
    than trusted because the request carried the id — the spine's only mutation stays owned.
    """
    proposal = await strategy_proposals.upsert(a_strategy_change_proposal())
    assert await strategy_proposals.get(USER, proposal.id) == proposal
    assert await strategy_proposals.get(OTHER_USER, proposal.id) is None


async def test_confirming_a_strategy_proposal_updates_the_one_row(
        db_session, career_prerequisites, strategy_proposals):
    """An approval flips `status` off `PROPOSED` on the row already there, not a new one."""
    proposal = await strategy_proposals.upsert(a_strategy_change_proposal())
    assert proposal.is_open is True
    executed = await strategy_proposals.upsert(proposal.executed(at=LATER))
    assert executed.status is StrategyChangeProposalStatus.EXECUTED
    assert executed.is_open is False
    assert await _count(db_session, StrategyChangeProposalRow) == 1


async def test_open_only_lists_the_still_confirmable_proposals(
        career_prerequisites, strategy_proposals):
    """`open_only` restricts to `PROPOSED` — a pending-suggestions surface and expiry sweep.

    A confirmed proposal drops out of the open list while staying in the full one, so its
    audit is never lost; the full list is most-recently-updated first.
    """
    await strategy_proposals.upsert(a_strategy_change_proposal(updated_at=NOW))
    await strategy_proposals.upsert(a_strategy_change_proposal(
        id=OTHER_STRATEGY_PROPOSAL,
        status=StrategyChangeProposalStatus.EXECUTED, updated_at=LATER))
    open_only = await strategy_proposals.list_for_user(USER, open_only=True)
    assert [p.id for p in open_only] == [STRATEGY_PROPOSAL]
    everything = await strategy_proposals.list_for_user(USER)
    assert [p.id for p in everything] == [OTHER_STRATEGY_PROPOSAL, STRATEGY_PROPOSAL]


async def test_a_strategy_execution_is_written_once_per_proposal(
        db_session, career_prerequisites, strategy_proposals, strategy_executions):
    """A double-confirm collapses onto one audit row — the executor's idempotency.

    The execution id derives from the proposal alone, so a second attempt upserts the same
    row rather than recording two, and a stranger cannot read it by the proposal's id.
    """
    await strategy_proposals.upsert(a_strategy_change_proposal())
    await strategy_executions.upsert(a_strategy_change_execution())
    await strategy_executions.upsert(
        a_strategy_change_execution(detail="second confirm ignored"))
    assert await _count(db_session, StrategyChangeExecutionRow) == 1
    stored = await strategy_executions.get(USER, STRATEGY_PROPOSAL)
    assert stored is not None and stored.succeeded
    assert await strategy_executions.get(OTHER_USER, STRATEGY_PROPOSAL) is None


# --------------------------------------------------------------------------
# Phase 16 — the commercial repositories. `plans` is a shared catalogue with no
# `user_id`; `subscriptions` and `usage_events` are user-scoped like every entity.
# --------------------------------------------------------------------------


@pytest.fixture
def plans(db_session):
    return SqlAlchemyPlanRepository(db_session)


@pytest.fixture
def subscriptions(db_session):
    return SqlAlchemySubscriptionRepository(db_session)


@pytest.fixture
def usage_events(db_session):
    return SqlAlchemyUsageEventRepository(db_session)


@pytest.fixture
def subscription_events(db_session):
    return SqlAlchemySubscriptionEventRepository(db_session)


@pytest_asyncio.fixture
async def commercial_accounts(db_session):
    """Two accounts and the `pro` plan: the foreign keys a subscription needs.

    A plan is shared and carries no `user_id`; a subscription references both a plan and
    a user, and a usage event references a user — so two accounts let the scoping tests
    prove one account cannot read the other's commercial rows.
    """
    db_session.add_all([a_user_row(display_name="owner"),
                        a_user_row(id=OTHER_USER, display_name="somebody else")])
    await db_session.flush()
    await SqlAlchemyPlanRepository(db_session).upsert(a_plan())
    await db_session.flush()


async def test_a_plan_and_its_entitlements_come_back_by_id_and_by_slug(plans):
    """The catalogue read, whole-object: the entitlement children round-trip with it.

    A plan is shared, so neither `get` nor `get_by_slug` is scoped to a user — the same
    exception the opportunity catalogue makes. Equality covers the entitlement collection,
    so a ceiling dropped on the way through the child rows fails here.
    """
    written = await plans.upsert(a_plan())
    assert written == a_plan()
    assert await plans.get(PRO_PLAN) == a_plan()
    assert await plans.get_by_slug("pro") == a_plan()


async def test_a_plan_is_found_by_its_provider_price_id_for_the_webhook_path(plans):
    """`get_by_external_price_id` maps a provider's price back onto a `Plan` — the webhook lookup.

    A subscription webhook names the provider's price, never the platform's slug, so applying one
    means mapping that price onto a plan. A price the catalogue seeded resolves to its plan; a
    price no plan carries returns `None`, which the webhook service records as an unmapped event
    rather than guessing a tier.
    """
    await plans.upsert(a_plan())
    found = await plans.get_by_external_price_id("price_fixture_pro")
    assert found is not None and found.id == PRO_PLAN
    assert await plans.get_by_external_price_id("price_unmapped") is None


async def test_re_seeding_a_plan_updates_one_row_and_reconciles_its_entitlements(
        db_session, plans):
    """Re-seeding the catalogue updates the plan in place and drops removed ceilings.

    The id derives from the slug, so a second seed is an UPDATE, not a duplicate; the
    entitlements are matched by key, so an entitlement no longer granted is deleted by the
    delete-orphan cascade rather than lingering as a ceiling the plan no longer offers.
    """
    await plans.upsert(a_plan())
    await plans.upsert(a_plan(name="Plan Pro (révisé)", entitlements=(
        an_entitlement(key=EntitlementKey.APPLICATION_SUBMISSIONS, limit=250),)))
    assert await _count(db_session, PlanRow) == 1
    assert await _count(db_session, PlanEntitlementRow) == 1
    stored = await plans.get(PRO_PLAN)
    assert stored is not None and stored.name == "Plan Pro (révisé)"
    assert stored.entitlement_for(EntitlementKey.APPLICATION_SUBMISSIONS).limit == 250
    assert stored.entitlement_for(EntitlementKey.LLM_TOKENS) is None


async def test_list_active_orders_the_free_tier_first_then_by_price(plans):
    """The pricing surface's order: cheapest first, the free tier (NULL price) at the head.

    `nulls_first` puts the free plan ahead of every priced one and price ascending orders
    the rest, so the page reads free → pro → scale regardless of insertion order.
    """
    await plans.upsert(a_plan(slug="scale", price_amount_cents=4900))
    await plans.upsert(a_plan())
    await plans.upsert(a_plan(slug="free", price_amount_cents=None, currency=None,
                             billing_interval=None, entitlements=(
                                 an_entitlement(
                                     key=EntitlementKey.APPLICATION_SUBMISSIONS, limit=5),)))
    listed = await plans.list_active()
    assert [p.slug for p in listed] == ["free", "pro", "scale"]


async def test_list_active_hides_retired_plans_and_public_only_hides_private_ones(plans):
    """`is_active` gates new checkouts; `public_only` gates the public pricing surface.

    A retired plan (`is_active=False`) never appears — a stale checkout cannot pick it —
    while a private-but-active plan (`is_public=False`) is offered to a direct link but
    hidden from the public page.
    """
    await plans.upsert(a_plan())
    await plans.upsert(a_plan(slug="hidden", is_public=False))
    await plans.upsert(a_plan(slug="retired", is_active=False))
    everything_active = await plans.list_active()
    assert {p.slug for p in everything_active} == {"pro", "hidden"}
    public = await plans.list_active(public_only=True)
    assert {p.slug for p in public} == {"pro"}


async def test_a_subscription_is_reported_as_absent_to_another_user(
        commercial_accounts, subscriptions):
    """A subscription is user data: a stranger naming its id reads `None`, not the row."""
    written = await subscriptions.upsert(a_subscription())
    assert await subscriptions.get(USER, written.id) == written
    assert await subscriptions.get(OTHER_USER, written.id) is None


async def test_find_by_id_reads_across_accounts_for_the_webhook_path(
        commercial_accounts, subscriptions):
    """`find_by_id` is the one unscoped read — a webhook has no session and must find the row.

    A provider event arrives with no user, so the webhook path looks the subscription up by the
    id its handle derives to, across the whole table, to learn which account already owns it —
    where the user-facing `get` returns `None` for a stranger, this returns the row regardless of
    owner. It only reads: it never widens anyone's access, and no request handler acting for a
    user reaches it.
    """
    await subscriptions.upsert(a_subscription())
    found = await subscriptions.find_by_id(SUBSCRIPTION)
    assert found is not None and found.user_id == USER
    assert await subscriptions.find_by_id(OTHER_SUBSCRIPTION) is None


async def test_a_redelivered_webhook_updates_the_one_subscription_row(
        db_session, commercial_accounts, subscriptions):
    """Every event about one subscription lands on one row — webhook idempotency.

    The id derives from the provider handle, so a later event upserts the row already
    there rather than colliding on `uq_subscriptions_provider_external_subscription_id`.
    """
    await subscriptions.upsert(a_subscription())
    await subscriptions.upsert(a_subscription(status=SubscriptionStatus.PAST_DUE,
                                              updated_at=LATER))
    assert await _count(db_session, SubscriptionRow) == 1
    stored = await subscriptions.get(USER, SUBSCRIPTION)
    assert stored is not None and stored.status is SubscriptionStatus.PAST_DUE


async def test_get_current_excludes_canceled_and_returns_the_most_recent(
        commercial_accounts, subscriptions):
    """The account's live subscription: the newest non-`CANCELED` row.

    A canceled subscription is history, not the current relationship, so it is excluded;
    among the rest the most recently updated wins, and the clock-based access decision is
    left to the resolver, not the query.
    """
    await subscriptions.upsert(a_subscription(
        status=SubscriptionStatus.CANCELED, updated_at=LATER,
        cancel_at_period_end=False))
    await subscriptions.upsert(a_subscription(
        id=OTHER_SUBSCRIPTION, external_subscription_id="sub_test_0002",
        status=SubscriptionStatus.ACTIVE, updated_at=NOW))
    current = await subscriptions.get_current(USER)
    assert current is not None and current.id == OTHER_SUBSCRIPTION


async def test_the_subscription_list_scopes_to_its_owner(
        commercial_accounts, subscriptions):
    """`list_for_user` returns one account's subscriptions and never another's."""
    await subscriptions.upsert(a_subscription())
    await subscriptions.upsert(a_subscription(
        id=OTHER_SUBSCRIPTION, external_subscription_id="sub_test_0002",
        user_id=OTHER_USER))
    assert [s.id for s in await subscriptions.list_for_user(USER)] == [SUBSCRIPTION]
    assert [s.id for s in await subscriptions.list_for_user(OTHER_USER)] == \
        [OTHER_SUBSCRIPTION]


# --------------------------------------------------------------------------
# Phase 16 M5 — the account-export lifecycle repository. User-scoped like every entity,
# and mutated in place as an export moves PENDING → READY/FAILED → EXPIRED.
# --------------------------------------------------------------------------

SECOND_EXPORT = AccountExportId(UUID("00000000-0000-4000-8000-000000000112"))
FOREIGN_EXPORT = AccountExportId(UUID("00000000-0000-4000-8000-000000000113"))
THIRD_EXPORT = AccountExportId(UUID("00000000-0000-4000-8000-000000000114"))


@pytest.fixture
def account_exports(db_session):
    return SqlAlchemyAccountExportRepository(db_session)


@pytest_asyncio.fixture
async def export_accounts(db_session):
    """Two accounts: the foreign-key target every export row cascades from."""
    db_session.add_all([a_user_row(display_name="owner"),
                        a_user_row(id=OTHER_USER, display_name="somebody else")])
    await db_session.flush()


async def test_an_export_comes_back_exactly_as_it_went_in(export_accounts, account_exports):
    """One assertion over every column, because a READY export populates them all."""
    written = await account_exports.upsert(an_account_export())
    assert written == an_account_export()
    assert await account_exports.get(USER, ACCOUNT_EXPORT) == an_account_export()


async def test_an_export_is_reported_as_absent_to_another_user(
        export_accounts, account_exports):
    """An export is user data: a stranger naming its id reads `None`, not the row (§68)."""
    await account_exports.upsert(an_account_export())
    assert await account_exports.get(OTHER_USER, ACCOUNT_EXPORT) is None


async def test_a_lifecycle_transition_updates_the_one_row(
        db_session, export_accounts, account_exports):
    """PENDING → READY lands on the one row: an export is mutated in place, not appended.

    The id is stable across the transition, so producing an archive updates the request row rather
    than inventing a second — the property `account_export_to_row`'s in-place update depends on.
    """
    pending = an_account_export(status=AccountExportStatus.PENDING, storage_key=None,
                                byte_size=None, completed_at=None)
    await account_exports.upsert(pending)
    ready = pending.completed(storage_key="exports/u/e.json", byte_size=64,
                              expires_at=LATER + timedelta(days=7), as_of=LATER)
    await account_exports.upsert(ready)
    assert await _count(db_session, AccountExportRow) == 1
    stored = await account_exports.get(USER, ACCOUNT_EXPORT)
    assert stored is not None and stored.status is AccountExportStatus.READY


async def test_the_export_list_is_most_recently_updated_first_and_scoped(
        export_accounts, account_exports):
    """`list_for_user` is one account's exports newest-first, and never another account's."""
    await account_exports.upsert(an_account_export(updated_at=NOW))
    await account_exports.upsert(an_account_export(id=SECOND_EXPORT, updated_at=LATER))
    await account_exports.upsert(an_account_export(id=FOREIGN_EXPORT, user_id=OTHER_USER))
    listed = await account_exports.list_for_user(USER)
    assert [e.id for e in listed] == [SECOND_EXPORT, ACCOUNT_EXPORT]
    assert [e.id for e in await account_exports.list_for_user(OTHER_USER)] == [FOREIGN_EXPORT]


async def test_list_expired_pages_lapsed_ready_archives_across_owners(
        export_accounts, account_exports):
    """`list_expired` is the retention work-list: lapsed READY archives, oldest-first, all owners.

    Only a READY export past its `expires_at` qualifies — a still-downloadable one and a FAILED
    request are both excluded — and the read carries no `user_id`, so one operator sweep sees every
    account's lapsed archive (§30). The `limit` caps a page, so no single statement locks the table.
    """
    sweep_at = LATER + timedelta(days=30)
    await account_exports.upsert(an_account_export())  # lapsed (expires LATER + 7d), USER
    await account_exports.upsert(
        an_account_export(id=THIRD_EXPORT, user_id=OTHER_USER))  # lapsed, another owner
    await account_exports.upsert(  # still downloadable — excluded
        an_account_export(id=SECOND_EXPORT, expires_at=sweep_at + timedelta(days=7)))
    await account_exports.upsert(  # not READY — excluded
        an_account_export(id=FOREIGN_EXPORT, status=AccountExportStatus.FAILED, storage_key=None,
                          byte_size=None, expires_at=None, failure_reason="PRODUCTION_FAILED"))

    lapsed = await account_exports.list_expired(sweep_at)
    assert [e.id for e in lapsed] == [ACCOUNT_EXPORT, THIRD_EXPORT]  # oldest, then lowest id
    # The cap pages one row at a time, so the loop drains rather than locking the table.
    assert [e.id for e in await account_exports.list_expired(sweep_at, limit=1)] == [ACCOUNT_EXPORT]


async def test_count_expired_counts_the_same_lapsed_ready_archives(
        export_accounts, account_exports):
    """`count_expired` powers `--dry-run`: the work-list's size without touching a byte.

    It matches `list_expired`'s predicate exactly — lapsed READY only, every owner — and reads
    zero before anything has lapsed, so a dry run reports precisely what a real sweep would purge.
    """
    sweep_at = LATER + timedelta(days=30)
    await account_exports.upsert(an_account_export())
    await account_exports.upsert(an_account_export(id=THIRD_EXPORT, user_id=OTHER_USER))
    await account_exports.upsert(
        an_account_export(id=SECOND_EXPORT, expires_at=sweep_at + timedelta(days=7)))
    assert await account_exports.count_expired(NOW) == 0  # nothing has lapsed yet
    assert await account_exports.count_expired(sweep_at) == 2


@pytest_asyncio.fixture
async def session_account(db_session):
    """The one account every user-session row below cascades from."""
    db_session.add(a_user_row(display_name="owner"))
    await db_session.flush()


@pytest.fixture
def user_sessions(db_session):
    return SqlAlchemySessionRepository(db_session)


def _a_user_session(*, expires_at):
    """A live `USER` session with two distinct token digests, lapsing at `expires_at`."""
    return UserSession(
        id=new_user_session_id(), user_id=USER,
        token_digest=digest_of(new_token()), csrf_token_digest=digest_of(new_token()),
        issued_at=NOW, expires_at=expires_at, last_seen_at=NOW)


async def test_expired_sessions_are_counted_then_swept_under_the_cap(
        session_account, user_sessions):
    """`count_expired`/`delete_expired` are retention's session half — absolute-lifetime, all owners.

    A session past its stored `expires_at` is an attack surface no one will resume; the sweep removes
    it. `count_expired` matches `delete_expired`'s predicate exactly (`<`), so `--dry-run` reports what
    a real sweep would remove; the cap pages a large table; and a rerun removes nothing once the
    expired rows are gone, while a still-live session is never touched (§30-31).
    """
    await user_sessions.upsert(_a_user_session(expires_at=NOW + timedelta(days=1)))
    await user_sessions.upsert(_a_user_session(expires_at=NOW + timedelta(days=2)))
    live = await user_sessions.upsert(_a_user_session(expires_at=LATER + timedelta(days=30)))
    sweep_at = LATER + timedelta(days=7)  # past both short sessions, before the live one

    assert await user_sessions.count_expired(sweep_at) == 2  # the dry-run read
    assert await user_sessions.delete_expired(sweep_at, limit=1) == 1  # one capped page
    assert await user_sessions.delete_expired(sweep_at) == 1  # the loop drains the remainder
    assert await user_sessions.count_expired(sweep_at) == 0  # nothing left; a rerun is a no-op
    assert await user_sessions.get_by_digest(live.token_digest) is not None  # the live one stays


async def test_a_usage_event_is_reported_as_absent_to_another_user(
        commercial_accounts, usage_events):
    """The metering ledger is user data: another account neither lists nor sums it."""
    await usage_events.add(a_usage_event())
    assert [e.source_id for e in await usage_events.list_for_user(USER)] == \
        [str(APPLICATION)]
    assert await usage_events.list_for_user(OTHER_USER) == ()
    assert await usage_events.sum_for_period(
        OTHER_USER, EntitlementKey.APPLICATION_SUBMISSIONS, "2026-03") == 0


async def test_re_metering_the_same_consumption_collapses_onto_one_row(
        db_session, commercial_accounts, usage_events):
    """A retried meter of the same source converges on one row rather than double-charging.

    The id derives from the idempotency key, so the second `add` collides inside its
    SAVEPOINT and reads the winning row back — the append-only ledger's idempotency (§7, §9).
    """
    first = await usage_events.add(a_usage_event())
    again = await usage_events.add(a_usage_event(detail="retried, same source"))
    assert again.id == first.id
    assert await _count(db_session, UsageEventRow) == 1


async def test_sum_for_period_totals_only_the_matching_key_and_window(
        commercial_accounts, usage_events):
    """A per-period sum counts one entitlement in one window — the quota check's read.

    Events for another key, another period, or another account are all excluded, so the
    sum is exactly "how much of this entitlement has this account consumed in this window".
    """
    await usage_events.add(a_usage_event(source_id="app-a", quantity=1))
    await usage_events.add(a_usage_event(source_id="app-b", quantity=1))
    # Another key, same period — excluded from the submissions sum.
    await usage_events.add(a_usage_event(
        entitlement_key=EntitlementKey.LLM_TOKENS, source_type=UsageSourceType.LLM_RUN,
        source_id=str(RUN), quantity=4096))
    # Same key, a different period — excluded from March's sum.
    await usage_events.add(a_usage_event(source_id="app-c", billing_period="2026-04"))
    assert await usage_events.sum_for_period(
        USER, EntitlementKey.APPLICATION_SUBMISSIONS, "2026-03") == 2
    assert await usage_events.sum_for_period(
        USER, EntitlementKey.LLM_TOKENS, "2026-03") == 4096


async def test_the_usage_list_is_most_recently_occurred_first(
        commercial_accounts, usage_events):
    """The ledger reads newest-first — the order a usage history surface shows."""
    await usage_events.add(a_usage_event(source_id="older", occurred_at=NOW))
    await usage_events.add(a_usage_event(source_id="newer", occurred_at=LATER))
    listed = await usage_events.list_for_user(USER)
    assert [e.source_id for e in listed] == ["newer", "older"]


async def test_a_redelivered_event_collapses_onto_one_ledger_record(
        db_session, commercial_accounts, subscription_events):
    """A redelivery of the same webhook converges on one record rather than a second row.

    The record id derives from the provider's event id, so `add` of an already-recorded event
    collides inside its SAVEPOINT and reads the winning record back — the processed-webhook
    ledger's idempotency, the audit twin of the usage ledger (§13).
    """
    first = await subscription_events.add(a_subscription_event())
    again = await subscription_events.add(
        a_subscription_event(detail="redelivered, same event id"))
    assert again.id == first.id and again == first
    assert await _count(db_session, SubscriptionEventRow) == 1
    assert await subscription_events.get(SUBSCRIPTION_EVENT) == first


async def test_the_event_feed_scopes_to_its_owner_and_omits_unattributable_events(
        commercial_accounts, subscription_events):
    """`list_for_user` is the per-account audit feed; a null-owner event appears in no feed.

    An attributed event is one account's audit fact and never another's; an event the platform
    could not attribute carries a null `user_id` (recorded `IGNORED` so its redelivery stays a
    no-op) and so shows up in nobody's feed — the scoped read leaves it out exactly as the real
    `WHERE user_id = ?` does.
    """
    await subscription_events.add(a_subscription_event())
    await subscription_events.add(a_subscription_event(
        id=OTHER_SUBSCRIPTION_EVENT, external_event_id="evt_test_0002",
        outcome=SubscriptionEventOutcome.IGNORED, user_id=None, subscription_id=None,
        detail="the subscription could not be attributed to an account"))
    assert [e.id for e in await subscription_events.list_for_user(USER)] == \
        [SUBSCRIPTION_EVENT]
    assert await subscription_events.list_for_user(OTHER_USER) == ()


# --------------------------------------------------------------------------
# Phase 16 M6 — the two primitives account deletion drives: erase the user row (the database's
# own cascade removes everything it owns) and enumerate the stored artifacts no foreign key
# reaches. The row cascade itself and the `subscription_events` de-identification are proved in
# `test_v2_persistence_constraints.py`; here we prove only these two repository methods.
# --------------------------------------------------------------------------


async def test_deleting_an_absent_account_affects_no_rows(db_session):
    """A `DELETE` for an id that was never stored removes nothing and reports it: `False`.

    This is what makes the deletion service idempotent — a retry after the row is already gone is
    a no-op, not an error.
    """
    users = SqlAlchemyUserRepository(db_session)
    assert await users.delete(OTHER_USER) is False


async def test_deleting_a_present_account_removes_it_and_is_idempotent(
        db_session, evaluation_prerequisites):
    """Deleting a stored account returns `True` and the row is gone; a second delete returns `False`.

    The Core `DELETE` is scoped by id, so the first call removes the one row and the second finds
    nothing — the same statement retried is harmless.
    """
    users = SqlAlchemyUserRepository(db_session)
    assert await users.delete(USER) is True
    assert await users.get(USER) is None
    assert await users.delete(USER) is False


async def test_artifact_storage_keys_lists_a_users_rendered_keys_and_no_others(
        db_session, evaluation_prerequisites):
    """The enumeration returns the owner's rendered artifact keys and is empty for anyone else.

    Deletion removes the stored bytes explicitly because no foreign key reaches an object store;
    this projection is what tells it which keys to remove, scoped to the account being erased.
    """
    documents = SqlAlchemyCandidateDocumentRepository(db_session)
    await documents.upsert(a_rendered_document(storage_key="documents/u/v1.pdf"))
    assert await documents.artifact_storage_keys_for_user(USER) == ("documents/u/v1.pdf",)
    assert await documents.artifact_storage_keys_for_user(OTHER_USER) == ()
