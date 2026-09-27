# tests/test_v2_persistence_constraints.py
"""What PostgreSQL refuses, asked of PostgreSQL.

Every domain rule that a column group can express is also a named CHECK, for one
reason: a `model_validator` protects the rows that go through Python, and the V1
importer, a future backfill script and a hand-written `UPDATE` in psql do not.
These tests write ORM rows directly — bypassing the domain on purpose, since the
domain would refuse most of them — and assert that the database refuses them by
the name a migration could later drop.

The cascades are here too. `ON DELETE` is a decision in both directions: deleting
a company must not take its postings with it, and deleting an account must take
everything the account owned.
"""
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from backend.app.domain.candidate import WorkAuthorizationStatus
from backend.app.domain.chat import (
    ChatActionExecutionOutcome,
    ChatActionKind,
    ChatMessageRole,
)
from backend.app.domain.common import LanguageLevel, SalaryPeriod, Weekday
from backend.app.domain.eligibility import (
    DeterminationSource,
    EligibilityRequirement,
    EligibilityStatus,
    RuleAuthority,
)
from backend.app.domain.entitlement import BillingInterval, EntitlementKey
from backend.app.domain.matching import MatchDimension
from backend.app.domain.opportunity import OpportunityType
from backend.app.domain.search import SearchAreaKind
from backend.app.domain.analytics import (
    DEFAULT_OBSERVATION_HORIZON_DAYS,
    DimensionKind,
    RateKind,
    TimingKind,
)
from backend.app.domain.application import ApplicationState
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.outcome import OutcomeKind
from backend.app.domain.recommendation import RecommendationKind
from backend.app.domain.role import RoleFamily, RoleFamilyProvenance
from backend.app.domain.strategy_change import (
    StrategyChangeExecutionOutcome,
    StrategyChangeKind,
    StrategyChangeTarget,
)
from backend.app.domain.subscription import SubscriptionStatus
from backend.app.domain.usage import UsageSourceType
from backend.app.infrastructure.database.models import (
    ApplicationOutcomeRow,
    ApplicationRow,
    CandidateAvailabilitySlotRow,
    CandidateLanguageRow,
    CandidateProfileRow,
    CandidateWorkAuthorizationRow,
    CareerRecommendationEvidenceRow,
    CareerRecommendationRow,
    ChatActionExecutionRow,
    ChatActionProposalRow,
    ChatMessageRow,
    CompanyLocationRow,
    CompanyRow,
    ConversationRow,
    EligibilityCheckRow,
    EligibilityResultRow,
    InterviewAnswerEvaluationRow,
    InterviewAnswerRow,
    InterviewQuestionRow,
    InterviewSessionRow,
    InterviewSessionSummaryRow,
    LLMRunRow,
    MatchDimensionScoreRow,
    MatchEvaluationRow,
    OpportunityRow,
    OpportunitySourceRecordRow,
    PlanEntitlementRow,
    PlanRow,
    RoleClassificationRow,
    SearchAreaRow,
    SearchProfileRow,
    StrategyChangeExecutionRow,
    StrategyChangeProposalRow,
    SubscriptionRow,
    UsageEventRow,
    UserRow,
    UserSessionRow,
)
from backend.app.infrastructure.database.mappers import (
    application_decision_to_row,
    application_policy_to_row,
    interview_answer_evaluation_to_row,
    interview_answer_to_row,
    interview_question_to_row,
    interview_session_summary_to_row,
    interview_session_to_row,
    llm_run_to_row,
)
from tests.v2_builders import (
    APPLICATION,
    COMPANY,
    COMPANY_LOCATION,
    DECISION,
    EVALUATION,
    FREE_PLAN,
    LATER,
    LAUSANNE,
    NOW,
    OPPORTUNITY,
    OTHER_OPPORTUNITY,
    OTHER_SUBSCRIPTION,
    OTHER_USER,
    POLICY,
    PRO_PLAN,
    PROFILE,
    RUN,
    SUBSCRIPTION,
    USER,
    a_decision,
    a_policy,
    an_answer_evaluation,
    an_interview_answer,
    an_interview_question,
    an_interview_session,
    an_interview_session_summary,
    an_llm_run,
)
from tests.v2_rows import a_candidate_profile_row, a_user_row, an_email_for

pytestmark = pytest.mark.asyncio

SECOND_LOCATION = UUID("00000000-0000-4000-8000-000000000036")
SECOND_SOURCE_RECORD = UUID("00000000-0000-4000-8000-000000000081")
SESSION = UUID("00000000-0000-4000-8000-000000000091")
SECOND_SESSION = UUID("00000000-0000-4000-8000-000000000092")
SEARCH = UUID("00000000-0000-4000-8000-0000000000a1")
AREA = UUID("00000000-0000-4000-8000-0000000000b1")
SECOND_AREA = UUID("00000000-0000-4000-8000-0000000000b2")
CHILD = UUID("00000000-0000-4000-8000-0000000000c1")
SECOND_CHILD = UUID("00000000-0000-4000-8000-0000000000c2")
ELIGIBILITY = UUID("00000000-0000-4000-8000-0000000000d1")
SECOND_ELIGIBILITY = UUID("00000000-0000-4000-8000-0000000000d2")
ELIGIBILITY_CHECK = UUID("00000000-0000-4000-8000-0000000000d3")
SECOND_ELIGIBILITY_CHECK = UUID("00000000-0000-4000-8000-0000000000d4")
CONVERSATION = UUID("00000000-0000-4000-8000-0000000000e1")
CHAT_MESSAGE = UUID("00000000-0000-4000-8000-0000000000e2")
SECOND_CHAT_MESSAGE = UUID("00000000-0000-4000-8000-0000000000e3")
CHAT_PROPOSAL = UUID("00000000-0000-4000-8000-0000000000e4")
SECOND_CHAT_PROPOSAL = UUID("00000000-0000-4000-8000-0000000000e5")
CHAT_EXECUTION = UUID("00000000-0000-4000-8000-0000000000e6")
SECOND_CHAT_EXECUTION = UUID("00000000-0000-4000-8000-0000000000e7")
OUTCOME = UUID("00000000-0000-4000-8000-0000000000f1")
SECOND_OUTCOME = UUID("00000000-0000-4000-8000-0000000000f2")
ROLE_CLASSIFICATION = UUID("00000000-0000-4000-8000-0000000000f3")
SECOND_ROLE_CLASSIFICATION = UUID("00000000-0000-4000-8000-0000000000f4")
RECOMMENDATION = UUID("00000000-0000-4000-8000-0000000000f5")
EVIDENCE = UUID("00000000-0000-4000-8000-0000000000f6")
SECOND_EVIDENCE = UUID("00000000-0000-4000-8000-0000000000f7")
STRATEGY_PROPOSAL = UUID("00000000-0000-4000-8000-0000000000f8")
STRATEGY_EXECUTION = UUID("00000000-0000-4000-8000-0000000000f9")
SECOND_STRATEGY_EXECUTION = UUID("00000000-0000-4000-8000-0000000000fa")
PLAN_ENTITLEMENT = UUID("00000000-0000-4000-8000-0000000000fb")
SECOND_PLAN_ENTITLEMENT = UUID("00000000-0000-4000-8000-0000000000fc")
USAGE_EVENT = UUID("00000000-0000-4000-8000-0000000000fd")
SECOND_USAGE_EVENT = UUID("00000000-0000-4000-8000-0000000000fe")
SECOND_PLAN = UUID("00000000-0000-4000-8000-0000000000ff")

# Two distinct SHA-256 digests, written out rather than computed: what the CHECK
# polices is the *shape* stored, so a literal that a reader can count is the point.
# Neither is the digest of anything — nothing here authenticates.
TOKEN_DIGEST = "a" * 64
CSRF_DIGEST = "b" * 64

# A single NEGATIVE reason, in the JSONB shape `reasons_to_json` writes. A gate
# that is not ELIGIBLE must carry one, so the tests about the *other* eligibility
# CHECKs supply it to keep `reasons_present_unless_eligible` from firing first and
# masking the constraint actually under test.
A_NEGATIVE_REASON = [{"code": "WORK_PERMIT_REQUIRED",
                      "detail": "The posting requires a work permit not declared.",
                      "impact": "NEGATIVE", "evidence_ids": []}]


def an_opportunity_row(**overrides) -> OpportunityRow:
    """The four columns a posting cannot be without, and nothing else.

    Minimal on purpose: each test adds exactly the columns whose combination is
    supposed to be rejected, so a failure names one rule rather than whichever of
    several the database happened to check first.
    """
    columns = {"id": OPPORTUNITY, "company_name": "Fixture SA",
               "title": "Ingenieur logiciel", "discovered_at": NOW}
    columns.update(overrides)
    return OpportunityRow(**columns)


def a_company_row(**overrides) -> CompanyRow:
    """A parent employer, with the two columns Phase 6 made non-optional.

    `normalized_name` is derived by the mapper from the domain object, so a raw row
    has to supply it by hand — and must supply what `normalize_company_name` would
    have produced, or the fixture would stand for a state the application cannot
    reach. The tests below are about *other* constraints; this one only has to exist.
    """
    columns = {"id": COMPANY, "name": "Fixture SA", "normalized_name": "fixture sa"}
    columns.update(overrides)
    return CompanyRow(**columns)


async def refuses(session, row, constraint: str) -> None:
    """Assert the flush fails, and that the row was rejected by `constraint`.

    The insert runs inside a savepoint so the session survives the violation and a
    parametrized case cannot poison the next one — the same mechanism the V1
    importer uses to attribute a failure to one row.
    """
    with pytest.raises(IntegrityError, match=constraint):
        async with session.begin_nested():
            session.add(row)
            await session.flush()


async def seed_owner_and_posting(session) -> None:
    """A user, a profile and a posting: the three foreign keys an evaluation needs.

    Flushed in dependency order by hand, because no `relationship()` joins these
    tables and SQLAlchemy therefore has no edge to sort the inserts by.
    """
    session.add(a_user_row())
    await session.flush()
    session.add(a_candidate_profile_row())
    session.add(an_opportunity_row())
    await session.flush()


async def seed_evaluation(session) -> None:
    """One valid evaluation, for the tests about its children and its cascades."""
    await seed_owner_and_posting(session)
    session.add(MatchEvaluationRow(id=EVALUATION, user_id=USER,
                                  candidate_profile_id=PROFILE,
                                  opportunity_id=OPPORTUNITY, overall=0.9,
                                  evaluated_at=NOW))
    await session.flush()


def an_eligibility_result_row(**overrides) -> EligibilityResultRow:
    """One verdict for the fixture pair, with the denormalized status the mapper writes.

    ELIGIBLE by default — the one status whose checks need carry no reason — so a
    test that wants a closed verdict overrides both this and the check beneath it.
    """
    columns = {"id": ELIGIBILITY, "user_id": USER, "candidate_profile_id": PROFILE,
               "opportunity_id": OPPORTUNITY, "status": EligibilityStatus.ELIGIBLE,
               "determined_at": NOW}
    columns.update(overrides)
    return EligibilityResultRow(**columns)


def an_eligibility_check_row(**overrides) -> EligibilityCheckRow:
    """One gate under the fixture verdict: ELIGIBLE, deterministic, unexplained.

    The default is the one shape that carries no reason without violating anything,
    so each test adds exactly the columns whose combination a CHECK is meant to
    reject — and supplies a reason itself when the status it sets would otherwise
    trip `reasons_present_unless_eligible` before the constraint under test.
    """
    columns = {"id": ELIGIBILITY_CHECK, "result_id": ELIGIBILITY, "ordinal": 0,
               "requirement": EligibilityRequirement.WORK_AUTHORIZATION,
               "status": EligibilityStatus.ELIGIBLE,
               "determined_by": DeterminationSource.DETERMINISTIC_RULE,
               "authority": RuleAuthority.UNKNOWN}
    columns.update(overrides)
    return EligibilityCheckRow(**columns)


async def seed_eligibility_result(session) -> None:
    """One valid ELIGIBLE verdict, for the tests about its checks and its cascade."""
    await seed_owner_and_posting(session)
    session.add(an_eligibility_result_row())
    await session.flush()


def a_session_row(**overrides) -> UserSessionRow:
    """One live session: a forward window, two distinct digests, no revocation."""
    columns = {"id": SESSION, "user_id": USER, "token_digest": TOKEN_DIGEST,
               "csrf_token_digest": CSRF_DIGEST, "issued_at": NOW,
               "expires_at": NOW + timedelta(days=14), "last_seen_at": NOW}
    columns.update(overrides)
    return UserSessionRow(**columns)


def a_search_row(**overrides) -> SearchProfileRow:
    """One saved search with every filter left empty — "no restriction"."""
    columns = {"id": SEARCH, "user_id": USER, "name": "Backend in Romandie"}
    columns.update(overrides)
    return SearchProfileRow(**columns)


def an_area_row(**overrides) -> SearchAreaRow:
    """One country area, the shape with no geometry to get wrong."""
    columns = {"id": AREA, "search_profile_id": SEARCH, "ordinal": 0,
               "kind": SearchAreaKind.COUNTRY, "country": "CH"}
    columns.update(overrides)
    return SearchAreaRow(**columns)


async def seed_profile_children(session) -> None:
    """One row in each of the three tables that hang off a candidate profile."""
    session.add_all([
        CandidateLanguageRow(id=CHILD, profile_id=PROFILE, ordinal=0,
                             language="fr", level=LanguageLevel.NATIVE),
        CandidateWorkAuthorizationRow(
            id=CHILD, profile_id=PROFILE, ordinal=0, country="CH",
            status=WorkAuthorizationStatus.WORK_PERMIT_HELD),
        CandidateAvailabilitySlotRow(id=CHILD, profile_id=PROFILE, ordinal=0,
                                     weekday=Weekday.SATURDAY, start_hour=8,
                                     end_hour=12),
    ])
    await session.flush()


async def seed_search(session) -> None:
    """A saved search and one area, for the cascade and the shape tests."""
    session.add(a_search_row())
    await session.flush()
    session.add(an_area_row())
    await session.flush()


async def _count(session, model) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


@pytest.mark.parametrize(("columns", "constraint"), [
    # A currency and a period with no amount: nothing to display, so the domain
    # models it as `salary=None` and the table says the same.
    ({"salary_currency": "CHF", "salary_period": SalaryPeriod.MONTHLY},
     "ck_opportunities_salary_complete_or_absent"),
    # An amount with no currency: nothing to compare it against.
    ({"salary_minimum": Decimal("4500.00")},
     "ck_opportunities_salary_complete_or_absent"),
    ({"salary_currency": "CHF", "salary_period": SalaryPeriod.MONTHLY,
      "salary_minimum": Decimal("6000.00"), "salary_maximum": Decimal("4000.00")},
     "ck_opportunities_salary_bounds_ordered"),
    ({"salary_currency": "CHF", "salary_period": SalaryPeriod.MONTHLY,
      "salary_minimum": Decimal("-1.00")},
     "ck_opportunities_salary_non_negative"),
    ({"workload_min_percent": 100, "workload_max_percent": 80},
     "ck_opportunities_workload_percent_ordered"),
    # 0% is not a workload, and 101% is not a week.
    ({"workload_min_percent": 0, "workload_max_percent": 50},
     "ck_opportunities_workload_percent_range"),
    ({"workload_min_percent": 50, "workload_max_percent": 101},
     "ck_opportunities_workload_percent_range"),
    ({"workload_min_weekly_hours": 200.0, "workload_max_weekly_hours": 200.0},
     "ck_opportunities_workload_hours_range"),
    ({"workload_min_weekly_hours": 12.0, "workload_max_weekly_hours": 8.0},
     "ck_opportunities_workload_hours_ordered"),
    # The three ISO code columns, where the length is the validation and the case
    # is part of the standard: `ch`, `FR` and `chf` are all wrong.
    ({"location_country": "ch"}, "ck_opportunities_location_country_format"),
    ({"posting_language": "FR"}, "ck_opportunities_posting_language_format"),
    ({"salary_currency": "chf", "salary_period": SalaryPeriod.MONTHLY,
      "salary_minimum": Decimal("4500.00")},
     "ck_opportunities_salary_currency_format"),
])
async def test_a_posting_the_domain_would_refuse_is_refused_by_the_table(
        db_session, columns, constraint):
    """Every `model_validator` on `Opportunity` that a column group can express.

    The parametrization is the list of invariants that survive without Python: the
    V1 importer writes through the domain, but nothing stops a later migration or
    an operator with psql from writing a salary range with no amount.
    """
    await refuses(db_session, an_opportunity_row(**columns), constraint)


@pytest.mark.parametrize(("columns", "constraint"), [
    ({"overall": 1.5}, "ck_match_evaluations_overall_in_unit_interval"),
    ({"overall": -0.1}, "ck_match_evaluations_overall_in_unit_interval"),
    ({"evidence_confidence": 1.2},
     "ck_match_evaluations_evidence_confidence_in_unit_interval"),
])
async def test_a_score_outside_the_unit_interval_is_refused(
        db_session, columns, constraint):
    """`Score` is `Annotated[float, Field(ge=0.0, le=1.0)]`, and so is the column.

    V1 stored 0-100 integers; a 0-1 float that has quietly been given a percentage
    is the exact mistake this catches — 92 does not fail any type check.
    """
    await seed_owner_and_posting(db_session)
    await refuses(db_session, MatchEvaluationRow(
        id=EVALUATION, user_id=USER, candidate_profile_id=PROFILE,
        opportunity_id=OPPORTUNITY, evaluated_at=NOW, **{"overall": 0.9, **columns}),
        constraint)


@pytest.mark.parametrize(("columns", "constraint"), [
    ({"score": 1.5}, "ck_match_dimension_scores_score_in_unit_interval"),
    ({"weight": -0.5}, "ck_match_dimension_scores_weight_in_unit_interval"),
])
async def test_a_dimension_score_outside_the_unit_interval_is_refused(
        db_session, columns, constraint):
    await seed_evaluation(db_session)
    await refuses(db_session, MatchDimensionScoreRow(
        id=SECOND_SOURCE_RECORD, match_evaluation_id=EVALUATION,
        dimension=MatchDimension.SKILLS_FIT, **{"score": 0.9, **columns}), constraint)


@pytest.mark.parametrize(("columns", "constraint"), [
    # A gate that is not ELIGIBLE and carries no reason: the unexplained rejection
    # docs/V2_SPECIFICATION.md §13 forbids.
    ({"status": EligibilityStatus.INELIGIBLE, "reasons": []},
     "ck_eligibility_checks_reasons_present_unless_eligible"),
    # A model that decided more than "I don't know yet": an LLM_EXTRACTION check may
    # only ever be INCOMPLETE, so a review verdict carrying that source is refused.
    ({"determined_by": DeterminationSource.LLM_EXTRACTION,
      "status": EligibilityStatus.REVIEW_REQUIRED, "reasons": A_NEGATIVE_REASON},
     "ck_eligibility_checks_llm_extraction_is_incomplete"),
    # §59, the rule with legal teeth: a Country Pack's operator-maintained value
    # refusing on its own — INELIGIBLE from a COUNTRY_PACK_RULE whose authority is
    # anything short of VERIFIED.
    ({"determined_by": DeterminationSource.COUNTRY_PACK_RULE,
      "status": EligibilityStatus.INELIGIBLE,
      "authority": RuleAuthority.OPERATOR_CONFIG, "reasons": A_NEGATIVE_REASON},
     "ck_eligibility_checks_pack_rule_blocks_only_when_verified"),
])
async def test_an_eligibility_check_the_domain_would_refuse_is_refused_by_the_table(
        db_session, columns, constraint):
    """`EligibilityCheck._verdict_is_accountable`, as three named CHECKs.

    The domain refuses all three, but a backfill script or a hand-written UPDATE
    does not go through the domain — and the third is the legal-safety rule of
    docs/COUNTRY_PACKS.md §Eligibility and Phase 9 §59, which says operator-
    maintained pack data must not be able to refuse an application on its own. That
    one the database itself has to hold, or a wrong number in a YAML file becomes an
    automatic "you may not apply".
    """
    await seed_eligibility_result(db_session)
    await refuses(db_session, an_eligibility_check_row(**columns), constraint)


async def test_a_verified_pack_rule_may_refuse_where_an_operator_value_may_not(
        db_session):
    """The other side of §59: VERIFIED is the one authority allowed to block.

    The rule is not "a pack rule can never refuse" — it is "only a rule reviewed
    against the legal source can". An INELIGIBLE check from a VERIFIED
    COUNTRY_PACK_RULE is the one the constraint must *admit*, or the safety rule
    would have quietly become "a pack may never decide eligibility at all".
    """
    await seed_owner_and_posting(db_session)
    db_session.add(an_eligibility_result_row(status=EligibilityStatus.INELIGIBLE))
    await db_session.flush()
    db_session.add(an_eligibility_check_row(
        determined_by=DeterminationSource.COUNTRY_PACK_RULE,
        status=EligibilityStatus.INELIGIBLE, authority=RuleAuthority.VERIFIED,
        reasons=A_NEGATIVE_REASON))
    await db_session.flush()
    assert await _count(db_session, EligibilityCheckRow) == 1


async def test_an_eligibility_result_addresses_its_checks_by_position(db_session):
    """`UNIQUE (result_id, ordinal)`: what makes re-evaluation reconcile in place.

    A requirement may repeat — two required languages are two LANGUAGE_MINIMUM
    gates — so the requirement is not the key. Position is, which is what lets a
    re-scoring run write the checks back as updates of the same rows rather than a
    delete-and-reinsert.
    """
    await seed_eligibility_result(db_session)
    db_session.add(an_eligibility_check_row())
    await db_session.flush()
    await refuses(db_session, an_eligibility_check_row(
        id=SECOND_ELIGIBILITY_CHECK, ordinal=0,
        requirement=EligibilityRequirement.LANGUAGE_MINIMUM),
        "uq_eligibility_checks_result_id_ordinal")


async def test_one_eligibility_verdict_per_candidate_and_opportunity(db_session):
    """`UNIQUE (candidate_profile_id, opportunity_id)`: one verdict per pair.

    Re-evaluating a pair updates its row; a second row for the same pair would be
    two answers to one question, with nothing to say which is current. It is the key
    that makes a re-scoring run idempotent, the same one `match_evaluations` carries.
    """
    await seed_eligibility_result(db_session)
    await refuses(db_session, an_eligibility_result_row(id=SECOND_ELIGIBILITY),
                  "uq_eligibility_results_candidate_profile_id_opportunity_id")


async def test_deleting_an_eligibility_result_deletes_its_checks(db_session):
    """`ON DELETE CASCADE` on `result_id`: a check with no verdict is unreachable.

    The relationship `match_evaluations` has with its dimension scores: the verdict
    owns the gates it was derived from, so re-evaluation can replace the set
    wholesale and a deleted verdict leaves none behind.
    """
    await seed_eligibility_result(db_session)
    db_session.add(an_eligibility_check_row())
    await db_session.flush()
    await db_session.execute(
        delete(EligibilityResultRow).where(EligibilityResultRow.id == ELIGIBILITY))
    db_session.expunge_all()
    assert await _count(db_session, EligibilityCheckRow) == 0


async def test_a_site_that_locates_nothing_is_refused(db_session):
    """`Location._must_locate_something`, as a CHECK over six NULL columns.

    A `company_locations` row with nothing in it is a marker nobody can place on
    the Phase 8 map, and `CompanyLocation.location` is non-optional in the domain
    precisely to make it impossible.
    """
    db_session.add(a_company_row())
    await db_session.flush()
    await refuses(db_session,
                  CompanyLocationRow(id=COMPANY_LOCATION, company_id=COMPANY),
                  "ck_company_locations_location_not_empty")


async def test_a_company_has_at_most_one_headquarters(db_session):
    """`Company._locations_belong_here`, as a partial unique index.

    Partial — `WHERE is_headquarters` — so the forty branches of a retail chain
    cost nothing, and only the one row that claims to be the head office is
    constrained.
    """
    db_session.add(a_company_row())
    await db_session.flush()
    db_session.add(CompanyLocationRow(id=COMPANY_LOCATION, company_id=COMPANY,
                                      location_city="Lausanne",
                                      is_headquarters=True))
    await db_session.flush()
    await refuses(db_session,
                  CompanyLocationRow(id=SECOND_LOCATION, company_id=COMPANY,
                                     location_city="Geneve", is_headquarters=True),
                  "uq_company_locations_company_id_headquarters")


async def test_a_company_may_have_any_number_of_ordinary_sites(db_session):
    """The other half of the partial index: without it, this would fail too."""
    db_session.add(a_company_row())
    await db_session.flush()
    db_session.add_all([
        CompanyLocationRow(id=COMPANY_LOCATION, company_id=COMPANY,
                           location_city="Lausanne"),
        CompanyLocationRow(id=SECOND_LOCATION, company_id=COMPANY,
                           location_city="Geneve"),
    ])
    await db_session.flush()
    assert await _count(db_session, CompanyLocationRow) == 2


async def test_one_source_cannot_publish_two_postings_under_one_id(db_session):
    """The import idempotency key, refusing the second copy.

    `UNIQUE (source_key, external_id)` is what makes re-running a discovery pass
    or the V1 import a conflict on an existing row instead of a duplicate posting.
    """
    db_session.add_all([an_opportunity_row(),
                        an_opportunity_row(id=OTHER_OPPORTUNITY)])
    await db_session.flush()
    db_session.add(OpportunitySourceRecordRow(
        id=COMPANY_LOCATION, opportunity_id=OPPORTUNITY, source_key="test_board",
        external_id="posting-1", fetched_at=NOW))
    await db_session.flush()
    await refuses(db_session, OpportunitySourceRecordRow(
        id=SECOND_SOURCE_RECORD, opportunity_id=OTHER_OPPORTUNITY,
        source_key="test_board", external_id="posting-1", fetched_at=NOW),
        "uq_opportunity_source_records_source_key_external_id")


async def test_two_sources_with_no_stable_id_do_not_collide(db_session):
    """PostgreSQL treats NULLs as distinct, which is the behaviour wanted here.

    A board that publishes no stable identifier cannot be used to claim that two
    postings are the same, so the pair must not conflict — and `dedup_fingerprint`
    is nullable and unique for the same reason.
    """
    db_session.add_all([an_opportunity_row(),
                        an_opportunity_row(id=OTHER_OPPORTUNITY)])
    await db_session.flush()
    db_session.add_all([
        OpportunitySourceRecordRow(id=COMPANY_LOCATION, opportunity_id=OPPORTUNITY,
                                   source_key="test_board", fetched_at=NOW),
        OpportunitySourceRecordRow(id=SECOND_SOURCE_RECORD,
                                   opportunity_id=OTHER_OPPORTUNITY,
                                   source_key="test_board", fetched_at=NOW),
    ])
    await db_session.flush()
    assert await _count(db_session, OpportunitySourceRecordRow) == 2


@pytest.mark.parametrize(("columns", "constraint"), [
    # The unique index compares TEXT case-sensitively, so the normalized form has
    # to be a constraint or `Ada@x.com` and `ada@x.com` are two accounts.
    ({"email": "Owner@example.test"}, "ck_users_email_normalized"),
    ({"email": " owner@example.test"}, "ck_users_email_normalized"),
    # Not an address: no `@` at all, or one with nothing on a side of it.
    ({"email": "owner.example.test"}, "ck_users_email_normalized"),
    ({"email": "@example.test"}, "ck_users_email_normalized"),
    ({"email": "owner@"}, "ck_users_email_normalized"),
    # A negative counter would make the lockout threshold unreachable.
    ({"failed_login_attempts": -1},
     "ck_users_failed_login_attempts_non_negative"),
])
async def test_an_account_that_could_never_log_in_is_refused(
        db_session, columns, constraint):
    """`normalize_email` and the lockout counter, as constraints on the table.

    The domain normalizes on the way in, and everything that authenticates goes
    through it — but a support script fixing an address in psql does not, and an
    address stored un-normalized is one that no login will ever match.
    """
    await refuses(db_session, a_user_row(**columns), constraint)


async def test_one_email_address_is_one_account(db_session):
    """`UNIQUE (email)`: the constraint registration relies on, not a lookup.

    `AuthenticationService` checks for an existing account first, but two
    simultaneous registrations both pass that check — the unique index is what makes
    the second one fail instead of creating a duplicate nobody can log into.
    """
    db_session.add(a_user_row())
    await db_session.flush()
    await refuses(db_session,
                  a_user_row(id=OTHER_USER, email=an_email_for(USER)),
                  "uq_users_email")


@pytest.mark.parametrize(("columns", "constraint"), [
    # The value the browser holds, written where its digest belongs: 43 characters
    # of `token_urlsafe`, refused for its shape before it can be stored.
    ({"token_digest": "3RCPqf7xJdWiXPHRRXR2Bg-not-a-digest"},
     "ck_user_sessions_token_digest_format"),
    ({"token_digest": TOKEN_DIGEST.upper()},
     "ck_user_sessions_token_digest_format"),
    ({"csrf_token_digest": "short"}, "ck_user_sessions_csrf_token_digest_format"),
    # One secret issued twice: whoever can read the CSRF cookie holds the session.
    ({"csrf_token_digest": TOKEN_DIGEST},
     "ck_user_sessions_digests_are_independent"),
    # A session that expires when it is issued, or before.
    ({"expires_at": NOW}, "ck_user_sessions_window_is_forward"),
    ({"expires_at": NOW - timedelta(seconds=1)},
     "ck_user_sessions_window_is_forward"),
    ({"last_seen_at": NOW - timedelta(seconds=1)},
     "ck_user_sessions_last_seen_after_issued"),
])
async def test_a_session_that_could_not_be_verified_is_refused(
        db_session, columns, constraint):
    """The digest columns hold digests, and the window points forward.

    A raw token written into `token_digest` would be a working credential sitting in
    the table the design says holds none (docs/AUTHENTICATION.md §Sessions), and it
    is the only mistake here that a reviewer cannot see by reading a row — 64 hex
    characters and 43 URL-safe ones look equally opaque.
    """
    db_session.add(a_user_row())
    await db_session.flush()
    await refuses(db_session, a_session_row(**columns), constraint)


async def test_two_sessions_cannot_share_one_token(db_session):
    """`UNIQUE (token_digest)`: one cookie value authenticates one session.

    Also the index the lookup on every authenticated request uses, which is why
    there is no second index on the column.
    """
    db_session.add(a_user_row())
    await db_session.flush()
    db_session.add(a_session_row())
    await db_session.flush()
    await refuses(db_session,
                  a_session_row(id=SECOND_SESSION, csrf_token_digest="c" * 64),
                  "uq_user_sessions_token_digest")


@pytest.mark.parametrize(("columns", "constraint"), [
    ({"availability_earliest_start": date(2026, 6, 1),
      "availability_latest_end": date(2026, 5, 1)},
     "ck_candidate_profiles_availability_window_ordered"),
    ({"availability_min_weekly_hours": 30.0, "availability_max_weekly_hours": 20.0},
     "ck_candidate_profiles_availability_hours_ordered"),
    # A week has 168 hours, and a maximum of zero is not availability.
    ({"availability_min_weekly_hours": 10.0, "availability_max_weekly_hours": 200.0},
     "ck_candidate_profiles_availability_hours_range"),
    ({"availability_notice_period_days": -1},
     "ck_candidate_profiles_availability_notice_non_negative"),
    ({"location_country": "ch"}, "ck_candidate_profiles_location_country_format"),
])
async def test_an_availability_the_domain_would_refuse_is_refused_by_the_table(
        db_session, columns, constraint):
    """`Availability`'s validators, over the five flattened columns.

    The window and the hours are what Phase 5 will compare a posting's workload
    against, so a reversed pair is not a display bug — it makes every comparison
    against that profile meaningless.
    """
    db_session.add(a_user_row())
    await db_session.flush()
    await refuses(db_session, a_candidate_profile_row(**columns), constraint)


@pytest.mark.parametrize(("row", "constraint"), [
    (lambda: CandidateLanguageRow(id=SECOND_CHILD, profile_id=PROFILE, ordinal=1,
                                  language="fr", level=LanguageLevel.B2),
     "uq_candidate_languages_profile_id_language"),
    (lambda: CandidateWorkAuthorizationRow(
        id=SECOND_CHILD, profile_id=PROFILE, ordinal=1, country="CH",
        status=WorkAuthorizationStatus.NOT_AUTHORIZED),
     "uq_candidate_work_authorizations_profile_id_country"),
    (lambda: CandidateAvailabilitySlotRow(
        id=SECOND_CHILD, profile_id=PROFILE, ordinal=1, weekday=Weekday.SATURDAY,
        start_hour=8, end_hour=17),
     "uq_candidate_availability_slots_profile_id_weekday_start_hour"),
])
async def test_a_profile_states_each_fact_about_itself_once(
        db_session, row, constraint):
    """`CandidateProfile._one_entry_per_language_and_country`, as three constraints.

    Two rows for French — B2 and native — would make "does this candidate read
    French at B2?" answerable both ways, and nothing in the schema says which row
    wins. The same argument covers a country listed twice with different permits.
    """
    await seed_owner_and_posting(db_session)
    await seed_profile_children(db_session)
    await refuses(db_session, row(), constraint)


async def test_a_language_is_a_lower_case_two_letter_code(db_session):
    """The ISO 639-1 form, because `posting_language` is compared against it.

    Only the case is asserted: `language` is `VARCHAR(2)`, so `"fra"` is refused by
    the column width — as a `DataError` rather than an `IntegrityError` — before any
    CHECK sees it. The constraint exists for the case the width cannot catch, and
    `"FR" <> "fr"` is exactly the comparison a match would get wrong.
    """
    await seed_owner_and_posting(db_session)
    await refuses(db_session, CandidateLanguageRow(
        id=CHILD, profile_id=PROFILE, ordinal=0, level=LanguageLevel.B2,
        language="FR"), "ck_candidate_languages_language_format")


@pytest.mark.parametrize(("columns", "constraint"), [
    ({"weekday": Weekday.MONDAY, "start_hour": 17, "end_hour": 9},
     "ck_candidate_availability_slots_slot_hours_ordered"),
    ({"weekday": Weekday.MONDAY, "start_hour": 9, "end_hour": 25},
     "ck_candidate_availability_slots_slot_hours_range"),
    ({"weekday": Weekday.MONDAY, "start_hour": 9, "end_hour": 9},
     "ck_candidate_availability_slots_slot_hours_ordered"),
])
async def test_a_weekly_slot_covers_at_least_one_hour_of_a_real_day(
        db_session, columns, constraint):
    """`end_hour` is exclusive and up to 24, so 09:00-09:00 is not a slot."""
    await seed_owner_and_posting(db_session)
    await refuses(db_session, CandidateAvailabilitySlotRow(
        id=CHILD, profile_id=PROFILE, ordinal=0, **columns), constraint)


@pytest.mark.parametrize(("columns", "constraint"), [
    # A NULL element: `ARRAY[NULL]::text[]` is a non-null array of one null, and
    # `= ANY` against it evaluates to NULL, so every posting would silently pass.
    ({"queries": ["backend engineer", None]},
     "ck_search_profiles_queries_elements_present"),
    ({"title_keywords": [""]},
     "ck_search_profiles_title_keywords_elements_present"),
    ({"excluded_keywords": ["stage", ""]},
     "ck_search_profiles_excluded_keywords_elements_present"),
    ({"source_keys": [None]}, "ck_search_profiles_source_keys_elements_present"),
    # A member no enum has: the string would be stored and match nothing forever.
    ({"opportunity_types": [OpportunityType.FULL_TIME.value, "SUMMER_JOB"]},
     "ck_search_profiles_opportunity_types_members"),
    ({"contract_types": ["INTERIM"]}, "ck_search_profiles_contract_types_members"),
    ({"workplace_modes": ["REMOTE_FIRST"]},
     "ck_search_profiles_workplace_modes_members"),
    ({"posting_languages": ["FR"]},
     "ck_search_profiles_posting_languages_format"),
    ({"posting_languages": ["fr", "deu"]},
     "ck_search_profiles_posting_languages_format"),
    ({"workload_min_percent": 80, "workload_max_percent": 50},
     "ck_search_profiles_workload_percent_ordered"),
    ({"workload_min_percent": 0, "workload_max_percent": 100},
     "ck_search_profiles_workload_percent_range"),
    ({"workload_min_weekly_hours": 20.0, "workload_max_weekly_hours": 200.0},
     "ck_search_profiles_workload_hours_range"),
    ({"workload_min_weekly_hours": 30.0, "workload_max_weekly_hours": 20.0},
     "ck_search_profiles_workload_hours_ordered"),
])
async def test_a_saved_search_the_domain_would_refuse_is_refused_by_the_table(
        db_session, columns, constraint):
    """The array filters, policed element by element.

    An array column accepts whatever its element type accepts, which is why each
    filter carries a CHECK: without them `TEXT[]` would hold a NULL, an empty string
    and `"SUMMER_JOB"` equally happily, and each of those turns a saved search into
    one that quietly matches nothing.
    """
    db_session.add(a_user_row())
    await db_session.flush()
    await refuses(db_session, a_search_row(**columns), constraint)


async def test_an_empty_filter_is_stored_as_an_empty_array_not_null(db_session):
    """"No restriction" has one representation, and it is not NULL.

    A nullable filter would give the same intent two spellings, and the first query
    written with `= ANY` against the NULL one matches nothing without failing.
    """
    db_session.add(a_user_row())
    await db_session.flush()
    db_session.add(a_search_row())
    await db_session.flush()
    db_session.expunge_all()
    stored = (await db_session.execute(
        select(SearchProfileRow).where(SearchProfileRow.id == SEARCH))).scalar_one()
    assert stored.queries == []
    assert stored.opportunity_types == []
    assert stored.posting_languages == []


@pytest.mark.parametrize(("columns", "constraint"), [
    # Each kind names exactly which columns must be present and which absent, so a
    # discriminator cannot disagree with the row it labels.
    ({"kind": SearchAreaKind.RADIUS, "country": None, "center": None,
      "radius_km": 30.0}, "ck_search_areas_shape_matches_kind"),
    ({"kind": SearchAreaKind.RADIUS, "country": None, "center": LAUSANNE,
      "radius_km": None}, "ck_search_areas_shape_matches_kind"),
    ({"kind": SearchAreaKind.COUNTRY, "country": "CH", "radius_km": 30.0},
     "ck_search_areas_shape_matches_kind"),
    ({"kind": SearchAreaKind.COUNTRY, "country": None},
     "ck_search_areas_shape_matches_kind"),
    ({"kind": SearchAreaKind.REMOTE_ONLY, "country": None, "center": LAUSANNE},
     "ck_search_areas_shape_matches_kind"),
    # A radius area whose shape is right and whose radius is not.
    ({"kind": SearchAreaKind.RADIUS, "country": None, "center": LAUSANNE,
      "radius_km": 0.0}, "ck_search_areas_radius_km_range"),
    ({"kind": SearchAreaKind.RADIUS, "country": None, "center": LAUSANNE,
      "radius_km": 501.0}, "ck_search_areas_radius_km_range"),
    ({"country": "ch"}, "ck_search_areas_country_format"),
])
async def test_an_area_must_be_the_shape_its_kind_promises(
        db_session, columns, constraint):
    """The domain's discriminated union, kept coherent without Python.

    `SearchArea` is three models sharing a table, and flattening a union into
    nullable columns is where a discriminator becomes a label: this CHECK is what
    makes a RADIUS row with no centre — a search over nowhere — impossible to store.
    """
    db_session.add(a_user_row())
    await db_session.flush()
    db_session.add(a_search_row())
    await db_session.flush()
    await refuses(db_session, an_area_row(**columns), constraint)


async def test_a_search_addresses_its_areas_by_position(db_session):
    """`UNIQUE (search_profile_id, ordinal)`: what makes re-saving an update.

    Two radius areas can differ only by their radius, so position is the only thing
    that identifies a row — and it is what lets a saved search be written back as an
    update of the same rows instead of a delete-and-reinsert.
    """
    db_session.add(a_user_row())
    await db_session.flush()
    await seed_search(db_session)
    await refuses(db_session, an_area_row(id=SECOND_AREA, ordinal=0, country="FR"),
                  "uq_search_areas_search_profile_id_ordinal")


async def test_deleting_a_company_keeps_its_postings(db_session):
    """`ON DELETE SET NULL`, and the reason it is not `CASCADE`.

    Phase 6 will merge duplicate company records. A posting is a fact that was
    observed; the employer it was attributed to is an inference, so losing the
    inference must not lose the fact.
    """
    db_session.add(a_company_row())
    await db_session.flush()
    db_session.add(an_opportunity_row(company_id=COMPANY))
    await db_session.flush()

    await db_session.execute(delete(CompanyRow).where(CompanyRow.id == COMPANY))
    db_session.expunge_all()
    result = await db_session.execute(
        select(OpportunityRow.company_id, OpportunityRow.company_name)
        .where(OpportunityRow.id == OPPORTUNITY))
    company_id, company_name = result.one()
    assert company_id is None
    # The string the posting itself carried is untouched: it is what the source
    # said, and no company row ever owned it.
    assert company_name == "Fixture SA"


async def test_deleting_an_account_deletes_everything_it_owned(db_session):
    """"Delete my account" as one statement, which is why the cascades exist.

    The list is the point. Everything the account owns goes — its sessions, its
    profile and that profile's languages, permits and slots, its saved searches and
    their areas, its evaluations and their dimension scores, its eligibility verdicts
    and their checks — through two levels of cascade and without a script that has to
    know the order. The shared posting stays: it is not the user's to delete
    (docs/ENGINEERING_STANDARDS.md §Security).

    A table added to the schema and forgotten here keeps its rows after the account
    is gone, which is the leak `docs/ENGINEERING_STANDARDS.md §Security` calls out —
    so this test is also the reason `test_v2_persistence_schema.py` insists that
    every table declare which of the two cascade groups it belongs to.
    """
    await seed_evaluation(db_session)
    await seed_profile_children(db_session)
    await seed_search(db_session)
    db_session.add_all([
        a_session_row(),
        MatchDimensionScoreRow(id=SECOND_SOURCE_RECORD,
                               match_evaluation_id=EVALUATION,
                               dimension=MatchDimension.SKILLS_FIT, score=0.92),
        an_eligibility_result_row(),
    ])
    await db_session.flush()
    db_session.add(an_eligibility_check_row())
    await db_session.flush()

    await db_session.execute(delete(UserRow).where(UserRow.id == USER))
    db_session.expunge_all()
    for model in (UserSessionRow, CandidateProfileRow, CandidateLanguageRow,
                  CandidateWorkAuthorizationRow, CandidateAvailabilitySlotRow,
                  SearchProfileRow, SearchAreaRow, MatchEvaluationRow,
                  MatchDimensionScoreRow, EligibilityResultRow, EligibilityCheckRow):
        assert await _count(db_session, model) == 0, model.__tablename__
    assert await _count(db_session, OpportunityRow) == 1


async def test_deleting_a_posting_deletes_its_provenance(db_session):
    """A source record with no opportunity is unreachable, so it cascades."""
    db_session.add(an_opportunity_row())
    await db_session.flush()
    db_session.add(OpportunitySourceRecordRow(
        id=SECOND_SOURCE_RECORD, opportunity_id=OPPORTUNITY,
        source_key="test_board", external_id="posting-1", fetched_at=NOW))
    await db_session.flush()

    await db_session.execute(
        delete(OpportunityRow).where(OpportunityRow.id == OPPORTUNITY))
    db_session.expunge_all()
    assert await _count(db_session, OpportunitySourceRecordRow) == 0


async def test_a_posting_may_not_carry_two_source_records(db_session):
    """The one-to-one the domain models today, held by the database.

    `UNIQUE (opportunity_id)` is the constraint a later phase drops if a posting
    has to be traceable to several boards — a decision, not a schema accident.
    """
    db_session.add(an_opportunity_row())
    await db_session.flush()
    db_session.add(OpportunitySourceRecordRow(
        id=COMPANY_LOCATION, opportunity_id=OPPORTUNITY, source_key="test_board",
        external_id="posting-1", fetched_at=NOW))
    await db_session.flush()
    await refuses(db_session, OpportunitySourceRecordRow(
        id=SECOND_SOURCE_RECORD, opportunity_id=OPPORTUNITY,
        source_key="other_board", external_id="posting-9", fetched_at=NOW),
        "uq_opportunity_source_records_opportunity_id")


# --- Phase 13 career chat ---------------------------------------------------
# The chat's domain rules made physical: a candidate's own turn carries no LLM
# telemetry, the monotonic counters never go negative, a turn numbers each
# proposal once, and a proposal records at most one execution. Every row here is
# built raw to reach a state the domain would refuse.


def a_conversation_row(**overrides) -> ConversationRow:
    """One thread owned by `USER`, for the tests about its turns and its cascade."""
    columns = {"id": CONVERSATION, "user_id": USER, "title": "Ma recherche d'emploi"}
    columns.update(overrides)
    return ConversationRow(**columns)


def a_chat_message_row(**overrides) -> ChatMessageRow:
    """A user turn at sequence 0, the shape the `user_has_no_run` CHECK permits."""
    columns = {"id": CHAT_MESSAGE, "conversation_id": CONVERSATION, "user_id": USER,
               "role": ChatMessageRole.USER, "content": "Peux-tu m'aider ?",
               "sequence": 0}
    columns.update(overrides)
    return ChatMessageRow(**columns)


def a_chat_action_proposal_row(**overrides) -> ChatActionProposalRow:
    """One NAVIGATE proposal hanging off the seeded turn — `status` takes its default."""
    columns = {"id": CHAT_PROPOSAL, "conversation_id": CONVERSATION,
               "message_id": CHAT_MESSAGE, "user_id": USER, "ordinal": 0,
               "kind": ChatActionKind.NAVIGATE,
               "action": {"kind": "NAVIGATE", "target": "APPLICATIONS"},
               "summary": "Aller aux candidatures"}
    columns.update(overrides)
    return ChatActionProposalRow(**columns)


async def seed_conversation(session) -> None:
    """An account and one conversation: the two foreign keys a turn needs."""
    session.add(a_user_row())
    await session.flush()
    session.add(a_conversation_row())
    await session.flush()


async def seed_chat_message(session) -> None:
    """A valid user turn, for the tests about the proposals and executions beneath it."""
    await seed_conversation(session)
    session.add(a_chat_message_row())
    await session.flush()


async def test_a_user_turn_carries_no_run_but_an_assistant_turn_may(db_session):
    """`ChatMessage`'s rule made physical: only an assistant turn holds LLM telemetry.

    A user turn with a provider key is refused by the CHECK — not by the `llm_runs`
    foreign key, which is why the telemetry column exercised here is `provider_key`
    and not `llm_run_id`. The same value on an assistant turn is permitted, because a
    generated turn is exactly what must be traceable to the call that produced it.
    """
    await seed_conversation(db_session)
    await refuses(db_session, a_chat_message_row(provider_key="openai_compatible"),
                  "ck_chat_messages_user_has_no_run")
    db_session.add(a_chat_message_row(id=SECOND_CHAT_MESSAGE, sequence=1,
                                      role=ChatMessageRole.ASSISTANT,
                                      content="Voici ce que je propose.",
                                      provider_key="openai_compatible"))
    await db_session.flush()
    assert await _count(db_session, ChatMessageRow) == 1


async def test_a_negative_turn_sequence_is_refused(db_session):
    """The monotonic counter the message id derives from never goes below zero."""
    await seed_conversation(db_session)
    await refuses(db_session, a_chat_message_row(sequence=-1),
                  "ck_chat_messages_sequence_non_negative")


async def test_a_conversation_numbers_each_turn_once(db_session):
    """`UNIQUE (conversation_id, sequence)`: what makes re-finalizing a turn an update.

    The message id is derived from `(conversation_id, sequence)`, so re-finalizing the
    same turn writes the same row — and a second row claiming a taken sequence is the
    duplicated exchange this constraint exists to prevent.
    """
    await seed_chat_message(db_session)
    await refuses(db_session, a_chat_message_row(id=SECOND_CHAT_MESSAGE, sequence=0),
                  "uq_chat_messages_conversation_id_sequence")


async def test_a_negative_proposal_ordinal_is_refused(db_session):
    """The position the proposal id derives from is a counter, never negative."""
    await seed_chat_message(db_session)
    await refuses(db_session, a_chat_action_proposal_row(ordinal=-1),
                  "ck_chat_action_proposals_ordinal_non_negative")


async def test_a_turn_numbers_each_proposal_once(db_session):
    """`UNIQUE (message_id, ordinal)`: re-finalizing a turn rewrites its proposals.

    A turn can propose several actions, ordered; the id derives from `(message_id,
    ordinal)`, so re-parsing the same assistant turn lands on the same proposal rows
    rather than duplicating them.
    """
    await seed_chat_message(db_session)
    db_session.add(a_chat_action_proposal_row())
    await db_session.flush()
    await refuses(db_session,
                  a_chat_action_proposal_row(id=SECOND_CHAT_PROPOSAL, ordinal=0),
                  "uq_chat_action_proposals_message_id_ordinal")


async def test_a_proposal_records_at_most_one_execution(db_session):
    """`UNIQUE (proposal_id)`: a double-confirm collides rather than running twice.

    The execution id is derived from the proposal alone, and the unique key is the
    second guard beneath it — so a second attempt to execute a confirmed proposal is
    refused by the database, not just by the derived id colliding.
    """
    await seed_chat_message(db_session)
    db_session.add(a_chat_action_proposal_row())
    await db_session.flush()
    db_session.add(ChatActionExecutionRow(
        id=CHAT_EXECUTION, proposal_id=CHAT_PROPOSAL, user_id=USER,
        outcome=ChatActionExecutionOutcome.SUCCEEDED))
    await db_session.flush()
    await refuses(db_session, ChatActionExecutionRow(
        id=SECOND_CHAT_EXECUTION, proposal_id=CHAT_PROPOSAL, user_id=USER,
        outcome=ChatActionExecutionOutcome.FAILED),
        "uq_chat_action_executions_proposal_id")


async def test_deleting_an_account_deletes_its_conversations(db_session):
    """"Delete my account" reaches the chat too, through one cascade from `users`.

    A conversation, its turns, the proposals parsed from them and the executions that
    ran are all the account's — so all four carry `user_id` and cascade from `users`,
    and deleting the account takes the whole thread without a script that has to know
    the order. This is the chat's row in `test_deleting_an_account_deletes_everything`.
    """
    await seed_chat_message(db_session)
    db_session.add(a_chat_action_proposal_row())
    await db_session.flush()
    db_session.add(ChatActionExecutionRow(
        id=CHAT_EXECUTION, proposal_id=CHAT_PROPOSAL, user_id=USER,
        outcome=ChatActionExecutionOutcome.SUCCEEDED))
    await db_session.flush()

    await db_session.execute(delete(UserRow).where(UserRow.id == USER))
    db_session.expunge_all()
    for model in (ConversationRow, ChatMessageRow, ChatActionProposalRow,
                  ChatActionExecutionRow):
        assert await _count(db_session, model) == 0, model.__tablename__


async def seed_interview_provenance(session) -> None:
    """A whole graded exchange, every generated artefact linked to one `LLMRun`.

    The account, its profile, the posting and the run come first — the four foreign
    keys an interview graph hangs off — then the session, its one question, the answer
    to it and that answer's grade, and finally the closing summary. Built through the
    mappers from valid domain values, because the point here is a cascade on well-formed
    rows, not a rejected shape. All four provenance links point at the same run.
    """
    session.add(a_user_row())
    await session.flush()
    session.add_all([a_candidate_profile_row(), an_opportunity_row(),
                     llm_run_to_row(an_llm_run(connection_id=None))])
    await session.flush()
    session.add(interview_session_to_row(an_interview_session(plan_llm_run_id=RUN)))
    await session.flush()
    session.add(interview_question_to_row(an_interview_question(llm_run_id=RUN)))
    await session.flush()
    session.add(interview_answer_to_row(an_interview_answer()))
    await session.flush()
    session.add_all([
        interview_answer_evaluation_to_row(an_answer_evaluation(llm_run_id=RUN)),
        interview_session_summary_to_row(an_interview_session_summary(llm_run_id=RUN)),
    ])
    await session.flush()


async def test_deleting_a_run_unlinks_the_interview_artefacts_it_produced(db_session):
    """`ON DELETE SET NULL` on every interview provenance link, and why it is not CASCADE.

    A run is the telemetry of the call that generated a question, graded an answer or
    wrote a summary — provenance the corrective added so an artefact is traceable to
    exactly the call behind it. But the artefact is the candidate's practice history; the
    run is bookkeeping about how it was produced. Pruning old telemetry must not delete a
    session's questions and grades, so the link is severed, not the fact — the same trade
    `llm_runs.connection_id` and `opportunities.company_id` make.

    So deleting the run leaves all four artefacts standing with a `NULL` link, the honest
    "produced by a run no longer on file" — never a dangling id, and never a lost grade.
    """
    await seed_interview_provenance(db_session)

    await db_session.execute(delete(LLMRunRow).where(LLMRunRow.id == RUN))
    db_session.expunge_all()

    # Every artefact survives — the practice history is untouched by the pruning.
    for model in (InterviewSessionRow, InterviewQuestionRow, InterviewAnswerRow,
                  InterviewAnswerEvaluationRow, InterviewSessionSummaryRow):
        assert await _count(db_session, model) == 1, model.__tablename__

    # And each link the run backed now reads NULL, not a dangling reference.
    session_run = await db_session.execute(
        select(InterviewSessionRow.plan_llm_run_id))
    question_run = await db_session.execute(select(InterviewQuestionRow.llm_run_id))
    evaluation_run = await db_session.execute(
        select(InterviewAnswerEvaluationRow.llm_run_id))
    summary_run = await db_session.execute(select(InterviewSessionSummaryRow.llm_run_id))
    assert session_run.scalar_one() is None
    assert question_run.scalar_one() is None
    assert evaluation_run.scalar_one() is None
    assert summary_run.scalar_one() is None


async def test_deleting_an_account_deletes_its_interview_history(db_session):
    """"Delete my account" reaches the interview simulator, through one cascade from `users`.

    A session, its questions, the answers, their grades and the summary are all the
    account's, so each carries `user_id` and cascades from `users` — deleting the account
    takes the whole practice history without a script that has to know the order. This is
    the simulator's row in `test_deleting_an_account_deletes_everything`. The run, also
    owned, goes with it; the shared posting stays.
    """
    await seed_interview_provenance(db_session)

    await db_session.execute(delete(UserRow).where(UserRow.id == USER))
    db_session.expunge_all()

    for model in (InterviewSessionRow, InterviewQuestionRow, InterviewAnswerRow,
                  InterviewAnswerEvaluationRow, InterviewSessionSummaryRow, LLMRunRow):
        assert await _count(db_session, model) == 0, model.__tablename__
    # The posting is not the account's to delete.
    assert await _count(db_session, OpportunityRow) == 1


# --- Phase 15 career intelligence -------------------------------------------
# The observe-measure-recommend loop's domain rules made physical: a milestone is
# recorded once per application, a correction never supersedes itself, a manual
# role verdict names a family, a cited metric clears the sample floor and carries
# exactly one shape, a proposal's queryable target agrees with its change family,
# and a confirmed proposal runs at most once. Every row here is built raw to reach
# a state the domain would refuse. Nothing here touches the Phase 12 execution
# lifecycle: an outcome has no `ApplicationState`, by construction (§2, §84).


def an_outcome_row(**overrides) -> ApplicationOutcomeRow:
    """One INTERVIEW milestone on the seeded application — `source`/`status` default.

    `outcome_key` is supplied by hand because the raw row bypasses the derivation
    that would otherwise compute it from the kind and `occurred_at`; the tests below
    are about the constraints, not that derivation.
    """
    columns = {"id": OUTCOME, "user_id": USER, "application_id": APPLICATION,
               "kind": OutcomeKind.INTERVIEW,
               "outcome_key": "INTERVIEW:2026-03-01T09:30:00+00:00",
               "occurred_at": NOW, "recorded_at": LATER}
    columns.update(overrides)
    return ApplicationOutcomeRow(**columns)


def a_role_classification_row(**overrides) -> RoleClassificationRow:
    """A deterministic verdict on the seeded posting — the shape both CHECKs permit.

    `created_at`/`updated_at` are set to `NOW` rather than left to the server so a
    test can move `updated_at` behind `created_at` and trip one named CHECK, the way
    the domain's own `updated_at >= created_at` guard would refuse.
    """
    columns = {"id": ROLE_CLASSIFICATION, "user_id": USER,
               "opportunity_id": OPPORTUNITY,
               "role_family": RoleFamily.SOFTWARE_ENGINEERING,
               "provenance": RoleFamilyProvenance.DETERMINISTIC_TITLE,
               "created_at": NOW, "updated_at": NOW}
    columns.update(overrides)
    return RoleClassificationRow(**columns)


def a_recommendation_row(**overrides) -> CareerRecommendationRow:
    """One suggestion owned by `USER`, the parent the evidence tests hang off."""
    columns = {"id": RECOMMENDATION, "user_id": USER,
               "kind": RecommendationKind.PRIORITIZE_ROLE_FAMILY,
               "analytics_version": "v1",
               "summary": "Priorise l'ingenierie logicielle.",
               "fingerprint": "fp-test",
               "analytics_computed_at": NOW,
               "observation_horizon_days": DEFAULT_OBSERVATION_HORIZON_DAYS}
    columns.update(overrides)
    return CareerRecommendationRow(**columns)


def an_evidence_row(**overrides) -> CareerRecommendationEvidenceRow:
    """A valid RESPONSE-rate citation: one metric shape, counts within the sample.

    Each test overrides exactly the columns whose combination one CHECK is meant to
    reject, so a failure names one rule rather than whichever of the metric-shape
    family the database happened to evaluate first.
    """
    columns = {"id": EVIDENCE, "recommendation_id": RECOMMENDATION, "ordinal": 0,
               "dimension": DimensionKind.ROLE_FAMILY,
               "dimension_key": "SOFTWARE_ENGINEERING",
               "rate_kind": RateKind.RESPONSE, "timing_kind": None,
               "numerator": 6, "denominator": 20, "median_days": None,
               "sample_size": 20, "detail": "6 reponses sur 20 candidatures."}
    columns.update(overrides)
    return CareerRecommendationEvidenceRow(**columns)


def a_proposal_row(**overrides) -> StrategyChangeProposalRow:
    """A PROPOSED SET_SEARCH_RADIUS edit whose target agrees with its kind.

    `change` takes the empty-object default — no CHECK polices its JSON shape, which
    is a Python validator — and the timestamps are set so a test can trip exactly the
    `updated_at`/`expires_at` ordering CHECK it is exercising.
    """
    columns = {"id": STRATEGY_PROPOSAL, "user_id": USER,
               "target": StrategyChangeTarget.SEARCH_PROFILE, "target_id": SEARCH,
               "kind": StrategyChangeKind.SET_SEARCH_RADIUS,
               "target_version": NOW, "summary": "Elargis le rayon de recherche.",
               "created_at": NOW, "updated_at": NOW,
               "expires_at": NOW + timedelta(days=7)}
    columns.update(overrides)
    return StrategyChangeProposalRow(**columns)


def an_execution_row(**overrides) -> StrategyChangeExecutionRow:
    """One SUCCEEDED execution of the seeded proposal — the executor's audit row."""
    columns = {"id": STRATEGY_EXECUTION, "proposal_id": STRATEGY_PROPOSAL,
               "user_id": USER, "outcome": StrategyChangeExecutionOutcome.SUCCEEDED}
    columns.update(overrides)
    return StrategyChangeExecutionRow(**columns)


async def seed_account(session) -> None:
    """One account: the single foreign key a recommendation or a proposal needs."""
    session.add(a_user_row())
    await session.flush()


async def seed_application(session) -> None:
    """The whole FK chain an outcome hangs off: account, profile, posting, policy,
    decision and one application.

    The policy and decision are built through their mappers from valid domain values;
    the application itself is a raw row at `APPLICATION`, because its id derives from
    an idempotency key and would not equal the fixture constant the outcome names.
    """
    await seed_owner_and_posting(session)
    session.add(application_policy_to_row(a_policy()))
    await session.flush()
    session.add(application_decision_to_row(a_decision()))
    await session.flush()
    session.add(ApplicationRow(
        id=APPLICATION, user_id=USER, candidate_profile_id=PROFILE,
        decision_id=DECISION, channel=ApplicationChannel.BROWSER,
        state=ApplicationState.PLANNED, idempotency_key="outcome-fixture-key",
        opportunity_id=OPPORTUNITY, policy_id=POLICY))
    await session.flush()


async def seed_recommendation(session) -> None:
    """An account and one recommendation: the parent its evidence rows need."""
    await seed_account(session)
    session.add(a_recommendation_row())
    await session.flush()


async def seed_proposal(session) -> None:
    """An account and one PROPOSED proposal, for the test about its one execution."""
    await seed_account(session)
    session.add(a_proposal_row())
    await session.flush()


async def test_a_correction_may_not_supersede_itself(db_session):
    """`no_self_supersede`: a correction points at the row it replaces, never itself.

    The domain builds a correction with `supersedes_id` naming a *prior* outcome; a
    row whose `supersedes_id` equals its own id is the cycle-of-one the CHECK refuses,
    the state a hand-written UPDATE could otherwise reach.
    """
    await seed_application(db_session)
    await refuses(db_session, an_outcome_row(supersedes_id=OUTCOME),
                  "ck_application_outcomes_no_self_supersede")


async def test_the_same_milestone_is_recorded_once(db_session):
    """`UNIQUE (application_id, outcome_key)`: a double-report collapses onto one row.

    The outcome id derives from `(application_id, outcome_key)`, so a retried request
    for the same milestone writes the same row; a second row claiming a taken key is
    the duplicated fact this constraint exists to prevent.
    """
    await seed_application(db_session)
    db_session.add(an_outcome_row())
    await db_session.flush()
    await refuses(db_session, an_outcome_row(id=SECOND_OUTCOME),
                  "uq_application_outcomes_application_id_outcome_key")


async def test_a_manual_role_verdict_must_name_a_family(db_session):
    """`manual_names_a_family`: a human correction cannot leave the role unclassified.

    A deterministic rule may decline to name a family — an honest "unclassified" — but
    a `MANUAL` verdict is a person's explicit choice, so the CHECK refuses one with a
    NULL `role_family`. The deterministic default row proves the other side stands.
    """
    await seed_owner_and_posting(db_session)
    await refuses(db_session,
                  a_role_classification_row(provenance=RoleFamilyProvenance.MANUAL,
                                            role_family=None),
                  "ck_role_classifications_manual_names_a_family")


async def test_a_classification_is_made_once_per_opportunity(db_session):
    """`UNIQUE (user_id, opportunity_id)`: re-classifying updates the one row.

    The id derives from `(user_id, opportunity_id)`, so a manual correction lands on
    the same row the deterministic rule wrote; a second row for the same pair is the
    divergent verdict this constraint keeps a user from holding two of.
    """
    await seed_owner_and_posting(db_session)
    db_session.add(a_role_classification_row())
    await db_session.flush()
    await refuses(db_session,
                  a_role_classification_row(id=SECOND_ROLE_CLASSIFICATION),
                  "uq_role_classifications_user_id_opportunity_id")


async def test_a_classification_updated_before_it_was_created_is_refused(db_session):
    """`updated_at >= created_at`: a correction advances the clock, never rewinds it."""
    await seed_owner_and_posting(db_session)
    await refuses(db_session,
                  a_role_classification_row(updated_at=NOW - timedelta(days=1)),
                  "ck_role_classifications_updated_at_after_created_at")


@pytest.mark.parametrize(("columns", "constraint"), [
    # A sample below the floor: the "no claim from weak data" rule made physical.
    ({"sample_size": 4},
     "ck_career_recommendation_evidence_sample_size_meets_minimum"),
    # The ordinal the evidence id derives from is a position, never negative.
    ({"ordinal": -1},
     "ck_career_recommendation_evidence_ordinal_non_negative"),
    # Neither a rate nor a timing: a citation that cites no metric shape.
    ({"rate_kind": None, "timing_kind": None, "numerator": None,
      "denominator": None},
     "ck_career_recommendation_evidence_cites_one_metric_shape"),
    # A rate whose numerator exceeds its denominator — a proportion above one.
    ({"numerator": 25, "denominator": 20},
     "ck_career_recommendation_evidence_rate_shape_coherent"),
    # A timing with no median: the one number a timing metric exists to carry.
    ({"rate_kind": None, "timing_kind": TimingKind.TIME_TO_FIRST_RESPONSE,
      "numerator": None, "denominator": None, "median_days": None},
     "ck_career_recommendation_evidence_timing_shape_coherent"),
    # An overall metric that still names a dimension key it has no dimension for.
    ({"dimension": None, "dimension_key": "SOFTWARE_ENGINEERING"},
     "ck_career_recommendation_evidence_overall_metric_has_no_key"),
], ids=["sample_floor", "ordinal", "one_shape", "rate_shape", "timing_shape",
        "overall_key"])
async def test_evidence_the_domain_would_refuse_is_refused_by_the_table(
        db_session, columns, constraint):
    """`RecommendationEvidence`'s validators that a column group can express.

    The parametrization is the list of shape rules that survive without Python: the
    engine writes evidence through the domain, but nothing stops a backfill or an
    operator with psql from writing a rate above one or a claim from four data points.
    Each case violates exactly one CHECK so the database names the rule under test.
    """
    await seed_recommendation(db_session)
    await refuses(db_session, an_evidence_row(**columns), constraint)


async def test_a_recommendation_numbers_each_evidence_once(db_session):
    """`UNIQUE (recommendation_id, ordinal)`: re-persisting reuses the evidence rows.

    A recommendation cites several metrics, ordered; each id derives from
    `(recommendation_id, ordinal)`, so re-persisting the aggregate lands on the same
    rows rather than duplicating them, and a second row at a taken ordinal is refused.
    """
    await seed_recommendation(db_session)
    db_session.add(an_evidence_row())
    await db_session.flush()
    await refuses(db_session, an_evidence_row(id=SECOND_EVIDENCE, ordinal=0),
                  "uq_career_recommendation_evidence_recommendation_id_ordinal")


async def test_a_proposals_target_must_match_its_change_family(db_session):
    """`target_matches_kind`: the queryable target agrees with the change it wraps.

    `target`/`kind` are denormalized columns an index answers "my open policy
    proposals" from, so they must not drift from the `change` payload they summarize:
    a SEARCH_PROFILE proposal carrying a policy-only `SET_MINIMUM_SCORE` kind is the
    inconsistency the CHECK refuses. The target_id-vs-payload half stays in Python.
    """
    await seed_account(db_session)
    await refuses(db_session,
                  a_proposal_row(target=StrategyChangeTarget.SEARCH_PROFILE,
                                 kind=StrategyChangeKind.SET_MINIMUM_SCORE),
                  "ck_strategy_change_proposals_target_matches_kind")


async def test_a_proposal_updated_before_it_was_created_is_refused(db_session):
    """`updated_at >= created_at`: a status transition advances the clock, never back."""
    await seed_account(db_session)
    await refuses(db_session, a_proposal_row(updated_at=NOW - timedelta(days=1)),
                  "ck_strategy_change_proposals_updated_at_after_created_at")


async def test_a_proposal_expiring_when_it_was_created_is_refused(db_session):
    """`expires_at > created_at`: a proposal a user can never act on is not written.

    The window is strict: an `expires_at` equal to `created_at` leaves no instant in
    which the proposal is open, so the CHECK demands the deadline strictly follow the
    drafting — the physical half of "a proposal is confirmable for a bounded while".
    """
    await seed_account(db_session)
    await refuses(db_session, a_proposal_row(expires_at=NOW),
                  "ck_strategy_change_proposals_expires_at_after_created_at")


async def test_a_confirmed_proposal_records_at_most_one_execution(db_session):
    """`UNIQUE (proposal_id)`: a double-confirm collides rather than applying twice.

    The execution id derives from the proposal alone, and the unique key is the guard
    beneath it — so a second attempt to apply a confirmed proposal is refused by the
    database, the idempotency the executor rests on, not just by the derived id.
    """
    await seed_proposal(db_session)
    db_session.add(an_execution_row())
    await db_session.flush()
    await refuses(db_session,
                  an_execution_row(id=SECOND_STRATEGY_EXECUTION,
                                   outcome=StrategyChangeExecutionOutcome.FAILED),
                  "uq_strategy_change_executions_proposal_id")


async def test_deleting_an_account_deletes_its_career_intelligence(db_session):
    """"Delete my account" reaches the intelligence loop, through one cascade from `users`.

    An outcome, a role verdict, a recommendation with its evidence, and a proposal with
    its execution are all the account's — so each carries `user_id` and cascades from
    `users`, and deleting the account takes the whole loop without a script that has to
    know the order. This is the loop's row in `test_deleting_an_account_deletes_everything`.
    The shared posting is not the account's to delete and stays.
    """
    await seed_application(db_session)
    db_session.add_all([an_outcome_row(), a_role_classification_row(),
                        a_recommendation_row()])
    await db_session.flush()
    db_session.add_all([an_evidence_row(), a_proposal_row()])
    await db_session.flush()
    db_session.add(an_execution_row())
    await db_session.flush()

    await db_session.execute(delete(UserRow).where(UserRow.id == USER))
    db_session.expunge_all()
    for model in (ApplicationOutcomeRow, RoleClassificationRow,
                  CareerRecommendationRow, CareerRecommendationEvidenceRow,
                  StrategyChangeProposalRow, StrategyChangeExecutionRow):
        assert await _count(db_session, model) == 0, model.__tablename__
    # The posting is not the account's to delete.
    assert await _count(db_session, OpportunityRow) == 1


# --------------------------------------------------------------------------
# Phase 16 — the commercial tables. `plans`/`plan_entitlements` are shared; a
# `subscription` and a `usage_event` are the account's, cascading from `users`.
# --------------------------------------------------------------------------


def a_plan_row(**overrides) -> PlanRow:
    """A valid paid monthly plan, its price/currency/interval all present.

    Each test overrides exactly the columns whose combination one CHECK is meant to
    reject, so a failure names one rule rather than whichever of the price family the
    database happened to evaluate first.
    """
    columns = {"id": PRO_PLAN, "slug": "pro", "name": "Plan Pro",
               "price_amount_cents": 1900, "currency": "CHF",
               "billing_interval": BillingInterval.MONTHLY}
    columns.update(overrides)
    return PlanRow(**columns)


def a_plan_entitlement_row(**overrides) -> PlanEntitlementRow:
    """One finite ceiling on the seeded plan — the child the plan's cascade reaches."""
    columns = {"id": PLAN_ENTITLEMENT, "plan_id": PRO_PLAN,
               "key": EntitlementKey.APPLICATION_SUBMISSIONS, "entitlement_limit": 5}
    columns.update(overrides)
    return PlanEntitlementRow(**columns)


def a_subscription_row(**overrides) -> SubscriptionRow:
    """One ACTIVE `stripe` subscription owned by `USER`, its paid window bounded.

    The window is both-set and forward-running and the cancel flag is off, so a test
    trips exactly the window/flag/sequence CHECK it overrides for.
    """
    columns = {"id": SUBSCRIPTION, "user_id": USER, "plan_id": PRO_PLAN,
               "status": SubscriptionStatus.ACTIVE, "provider": "stripe",
               "external_customer_id": "cus_fixture", "external_subscription_id": "sub_1",
               "current_period_start": NOW, "current_period_end": LATER,
               "cancel_at_period_end": False, "provider_event_at": NOW,
               "provider_event_sequence": 1}
    columns.update(overrides)
    return SubscriptionRow(**columns)


def a_usage_event_row(**overrides) -> UsageEventRow:
    """One measured submission owned by `USER`, positive quantity, a real period."""
    columns = {"id": USAGE_EVENT, "user_id": USER,
               "entitlement_key": EntitlementKey.APPLICATION_SUBMISSIONS,
               "quantity": 1, "source_type": UsageSourceType.APPLICATION_SUBMISSION,
               "source_id": str(APPLICATION), "occurred_at": NOW,
               "billing_period": "2026-03", "idempotency_key": "usage-fixture-key"}
    columns.update(overrides)
    return UsageEventRow(**columns)


async def seed_plan(session) -> None:
    """The one plan an entitlement or a subscription foreign key points at."""
    session.add(a_plan_row())
    await session.flush()


async def seed_account_and_plan(session) -> None:
    """A user and a plan: the two foreign keys a subscription needs."""
    session.add(a_user_row())
    await seed_plan(session)


async def test_a_priced_plan_without_a_currency_is_refused(db_session):
    """`price_all_or_nothing`: an amount with no currency could not be charged.

    A price and a currency are all-or-nothing (a free plan sets neither); an amount
    without the currency to denominate it is the half-stated price the CHECK refuses.
    The interval is present so `paid_carries_an_interval` does not fire first.
    """
    await refuses(db_session, a_plan_row(currency=None), "ck_plans_price_all_or_nothing")


async def test_a_paid_plan_without_a_billing_interval_is_refused(db_session):
    """`paid_carries_an_interval`: a positive price is a subscription and needs a cadence.

    A *paid* plan with no `billing_interval` is a recurring charge with no renewal
    period — the incoherent state the CHECK refuses. Price and currency are both set so
    `price_all_or_nothing` stands.
    """
    await refuses(db_session, a_plan_row(billing_interval=None),
                  "ck_plans_paid_carries_an_interval")


async def test_a_currency_that_is_not_an_iso_code_is_refused(db_session):
    """`currency_format`: the column holds the ISO-4217 shape the domain validates."""
    await refuses(db_session, a_plan_row(currency="chf"), "ck_plans_currency_format")


async def test_a_negative_price_is_refused(db_session):
    """`price_amount_cents_non_negative`: a plan cannot cost less than nothing."""
    await refuses(db_session, a_plan_row(price_amount_cents=-1),
                  "ck_plans_price_amount_cents_non_negative")


async def test_a_free_plan_sets_neither_price_nor_currency_nor_interval(db_session):
    """The other side of the price CHECKs: a free plan with all three absent is admitted.

    `price_amount_cents`, `currency` and `billing_interval` all NULL satisfies both the
    all-or-nothing and the paid-carries clauses, so the free tier is a plan the table
    accepts — the row the pricing surface offers at the top.
    """
    db_session.add(a_plan_row(id=FREE_PLAN, slug="free", name="Plan Free",
                              price_amount_cents=None, currency=None,
                              billing_interval=None))
    await db_session.flush()
    assert await _count(db_session, PlanRow) == 1


async def test_one_slug_is_one_plan(db_session):
    """`UNIQUE (slug)`: re-seeding a plan updates its row, never mints a second.

    The plan id derives from the slug, so a repeated seed writes the same row; a second
    row claiming a taken slug under a different id is the duplicate this constraint stops.
    """
    db_session.add(a_plan_row())
    await db_session.flush()
    await refuses(db_session, a_plan_row(id=SECOND_PLAN), "uq_plans_slug")


async def test_a_plan_updated_before_it_was_created_is_refused(db_session):
    """`updated_at_after_created_at`: a plan's revision cannot predate its creation."""
    await refuses(db_session, a_plan_row(created_at=LATER, updated_at=NOW),
                  "ck_plans_updated_at_after_created_at")


async def test_a_negative_entitlement_limit_is_refused(db_session):
    """`entitlement_limit_non_negative`: a ceiling is a count, never below zero.

    `NULL` is the domain's *unlimited* and stands; a negative limit is the value the
    CHECK refuses.
    """
    await seed_plan(db_session)
    await refuses(db_session, a_plan_entitlement_row(entitlement_limit=-1),
                  "ck_plan_entitlements_entitlement_limit_non_negative")


async def test_a_plan_grants_each_entitlement_key_once(db_session):
    """`UNIQUE (plan_id, key)`: one plan states each capability's ceiling exactly once."""
    await seed_plan(db_session)
    db_session.add(a_plan_entitlement_row())
    await db_session.flush()
    await refuses(db_session, a_plan_entitlement_row(id=SECOND_PLAN_ENTITLEMENT),
                  "uq_plan_entitlements_plan_id_key")


async def test_an_entitlement_for_no_plan_is_refused(db_session):
    """`fk_plan_entitlements_plan_id_plans`: an entitlement is reached only through its plan."""
    await refuses(db_session, a_plan_entitlement_row(),
                  "fk_plan_entitlements_plan_id_plans")


async def test_deleting_a_plan_deletes_its_entitlements(db_session):
    """The plan→entitlement cascade: retiring a plan's row takes the ceilings it named.

    A plan is normally retired via `is_active`, not deleted — but the FK is `CASCADE`, so
    a genuine delete of the catalogue row does not orphan its entitlement children.
    """
    await seed_plan(db_session)
    db_session.add(a_plan_entitlement_row())
    await db_session.flush()
    await db_session.execute(delete(PlanRow).where(PlanRow.id == PRO_PLAN))
    db_session.expunge_all()
    assert await _count(db_session, PlanEntitlementRow) == 0


async def test_a_subscription_window_with_only_one_end_is_refused(db_session):
    """`window_both_or_neither`: a billing window names both ends or neither.

    A start with no end (or an end with no start) is a half-open window the table
    refuses; the unbounded case is the internal free tier, admitted elsewhere.
    """
    await seed_account_and_plan(db_session)
    await refuses(db_session, a_subscription_row(current_period_end=None),
                  "ck_subscriptions_window_both_or_neither")


async def test_a_subscription_window_that_ends_before_it_starts_is_refused(db_session):
    """`window_runs_forward`: a paid period cannot end before it began."""
    await seed_account_and_plan(db_session)
    await refuses(db_session,
                  a_subscription_row(current_period_start=LATER, current_period_end=NOW),
                  "ck_subscriptions_window_runs_forward")


async def test_a_cancel_at_period_end_status_must_carry_the_flag(db_session):
    """`cancel_at_period_end_carries_the_flag`: the status and the flag cannot disagree.

    A `CANCEL_AT_PERIOD_END` subscription that leaves `cancel_at_period_end` false is a
    contradiction between the lifecycle and the flag the resolver reads — refused here.
    """
    await seed_account_and_plan(db_session)
    await refuses(db_session,
                  a_subscription_row(status=SubscriptionStatus.CANCEL_AT_PERIOD_END,
                                     cancel_at_period_end=False),
                  "ck_subscriptions_cancel_at_period_end_carries_the_flag")


async def test_a_negative_provider_event_sequence_is_refused(db_session):
    """`provider_event_sequence_non_negative`: the out-of-order guard's counter is a count."""
    await seed_account_and_plan(db_session)
    await refuses(db_session, a_subscription_row(provider_event_sequence=-1),
                  "ck_subscriptions_provider_event_sequence_non_negative")


async def test_a_subscription_updated_before_it_was_created_is_refused(db_session):
    """`updated_at_after_created_at`: a subscription's revision cannot predate its creation."""
    await seed_account_and_plan(db_session)
    await refuses(db_session, a_subscription_row(created_at=LATER, updated_at=NOW),
                  "ck_subscriptions_updated_at_after_created_at")


async def test_one_provider_subscription_handle_is_one_row(db_session):
    """`UNIQUE (provider, external_subscription_id)`: a redelivered webhook lands on one row.

    A provider's subscription handle identifies one subscription; a second row under the
    same `(provider, external_subscription_id)` is the duplicate a replayed event would
    otherwise create.
    """
    await seed_account_and_plan(db_session)
    db_session.add(a_subscription_row())
    await db_session.flush()
    await refuses(db_session, a_subscription_row(id=OTHER_SUBSCRIPTION),
                  "uq_subscriptions_provider_external_subscription_id")


async def test_internal_free_subscriptions_carry_a_null_handle_and_do_not_collide(
        db_session):
    """The internal free tier has no provider handle, and NULLs never collide.

    Two accounts on the internal free tier both carry `external_subscription_id = NULL`,
    which a UNIQUE constraint treats as distinct — so the free tier is not capped at one
    account by the very constraint that dedupes provider webhooks.
    """
    db_session.add_all([a_user_row(), a_user_row(id=OTHER_USER)])
    await seed_plan(db_session)
    db_session.add(a_subscription_row(
        id=SUBSCRIPTION, user_id=USER, provider="internal",
        external_customer_id=None, external_subscription_id=None,
        current_period_start=None, current_period_end=None,
        provider_event_at=None, provider_event_sequence=None))
    db_session.add(a_subscription_row(
        id=OTHER_SUBSCRIPTION, user_id=OTHER_USER, provider="internal",
        external_customer_id=None, external_subscription_id=None,
        current_period_start=None, current_period_end=None,
        provider_event_at=None, provider_event_sequence=None))
    await db_session.flush()
    assert await _count(db_session, SubscriptionRow) == 2


async def test_a_subscription_for_no_plan_is_refused(db_session):
    """`fk_subscriptions_plan_id_plans`: a subscription always points at a real plan."""
    db_session.add(a_user_row())
    await db_session.flush()
    await refuses(db_session, a_subscription_row(), "fk_subscriptions_plan_id_plans")


async def test_a_non_positive_usage_quantity_is_refused(db_session):
    """`quantity_positive`: a metered event exists only for real, positive consumption.

    An unmeasurable call writes *no* event rather than a fabricated `0`, so the table
    refuses a zero (or negative) quantity — a stored row is always measured consumption.
    """
    await seed_account(db_session)
    await refuses(db_session, a_usage_event_row(quantity=0),
                  "ck_usage_events_quantity_positive")


async def test_one_idempotency_key_is_one_usage_event(db_session):
    """`UNIQUE (idempotency_key)`: re-metering the same source collapses onto one row.

    The event id derives from the idempotency key, so a retried meter writes the same
    row; a second row under a taken key — written by hand — is what this constraint stops,
    the second half of the guard the derived id already gives.
    """
    await seed_account(db_session)
    db_session.add(a_usage_event_row())
    await db_session.flush()
    await refuses(db_session, a_usage_event_row(id=SECOND_USAGE_EVENT),
                  "uq_usage_events_idempotency_key")


async def test_a_usage_event_for_no_account_is_refused(db_session):
    """`fk_usage_events_user_id_users`: every metered fact is owned by a real account."""
    await refuses(db_session, a_usage_event_row(), "fk_usage_events_user_id_users")


async def test_deleting_an_account_deletes_its_commercial_data(db_session):
    """"Delete my account" reaches the subscription and the usage ledger, via one cascade.

    A subscription and a usage event are the account's, so each carries `user_id` and
    cascades from `users`; deleting the account takes both. The shared plan and its
    entitlements are the platform's, not the account's, and stay.
    """
    await seed_account_and_plan(db_session)
    db_session.add(a_plan_entitlement_row())
    db_session.add(a_subscription_row())
    db_session.add(a_usage_event_row())
    await db_session.flush()

    await db_session.execute(delete(UserRow).where(UserRow.id == USER))
    db_session.expunge_all()
    assert await _count(db_session, SubscriptionRow) == 0
    assert await _count(db_session, UsageEventRow) == 0
    # The catalogue is the platform's; it is not the account's to delete.
    assert await _count(db_session, PlanRow) == 1
    assert await _count(db_session, PlanEntitlementRow) == 1
