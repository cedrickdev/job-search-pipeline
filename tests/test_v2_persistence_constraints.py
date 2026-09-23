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
from backend.app.domain.matching import MatchDimension
from backend.app.domain.opportunity import OpportunityType
from backend.app.domain.search import SearchAreaKind
from backend.app.infrastructure.database.models import (
    CandidateAvailabilitySlotRow,
    CandidateLanguageRow,
    CandidateProfileRow,
    CandidateWorkAuthorizationRow,
    ChatActionExecutionRow,
    ChatActionProposalRow,
    ChatMessageRow,
    CompanyLocationRow,
    CompanyRow,
    ConversationRow,
    EligibilityCheckRow,
    EligibilityResultRow,
    MatchDimensionScoreRow,
    MatchEvaluationRow,
    OpportunityRow,
    OpportunitySourceRecordRow,
    SearchAreaRow,
    SearchProfileRow,
    UserRow,
    UserSessionRow,
)
from tests.v2_builders import (
    COMPANY,
    COMPANY_LOCATION,
    EVALUATION,
    LAUSANNE,
    NOW,
    OPPORTUNITY,
    OTHER_OPPORTUNITY,
    OTHER_USER,
    PROFILE,
    USER,
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
