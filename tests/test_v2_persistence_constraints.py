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
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from backend.app.domain.common import SalaryPeriod
from backend.app.domain.matching import MatchDimension
from backend.app.infrastructure.database.models import (
    CandidateProfileRow,
    CompanyLocationRow,
    CompanyRow,
    MatchDimensionScoreRow,
    MatchEvaluationRow,
    OpportunityRow,
    OpportunitySourceRecordRow,
    UserRow,
)
from tests.v2_builders import (
    COMPANY,
    COMPANY_LOCATION,
    EVALUATION,
    NOW,
    OPPORTUNITY,
    OTHER_OPPORTUNITY,
    PROFILE,
    USER,
)

pytestmark = pytest.mark.asyncio

SECOND_LOCATION = UUID("00000000-0000-4000-8000-000000000036")
SECOND_SOURCE_RECORD = UUID("00000000-0000-4000-8000-000000000081")

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
    session.add(UserRow(id=USER))
    await session.flush()
    session.add(CandidateProfileRow(id=PROFILE, user_id=USER))
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


async def test_a_site_that_locates_nothing_is_refused(db_session):
    """`Location._must_locate_something`, as a CHECK over six NULL columns.

    A `company_locations` row with nothing in it is a marker nobody can place on
    the Phase 8 map, and `CompanyLocation.location` is non-optional in the domain
    precisely to make it impossible.
    """
    db_session.add(CompanyRow(id=COMPANY, name="Fixture SA"))
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
    db_session.add(CompanyRow(id=COMPANY, name="Fixture SA"))
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
    db_session.add(CompanyRow(id=COMPANY, name="Fixture SA"))
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


async def test_deleting_a_company_keeps_its_postings(db_session):
    """`ON DELETE SET NULL`, and the reason it is not `CASCADE`.

    Phase 6 will merge duplicate company records. A posting is a fact that was
    observed; the employer it was attributed to is an inference, so losing the
    inference must not lose the fact.
    """
    db_session.add(CompanyRow(id=COMPANY, name="Fixture SA"))
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

    A candidate's evaluations and their dimension scores go with the user row, and
    the shared posting stays: it is not the user's to delete
    (docs/ENGINEERING_STANDARDS.md §Security).
    """
    await seed_evaluation(db_session)
    db_session.add(MatchDimensionScoreRow(
        id=SECOND_SOURCE_RECORD, match_evaluation_id=EVALUATION,
        dimension=MatchDimension.SKILLS_FIT, score=0.92))
    await db_session.flush()

    await db_session.execute(delete(UserRow).where(UserRow.id == USER))
    db_session.expunge_all()
    assert await _count(db_session, MatchEvaluationRow) == 0
    assert await _count(db_session, MatchDimensionScoreRow) == 0
    assert await _count(db_session, CandidateProfileRow) == 0
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
