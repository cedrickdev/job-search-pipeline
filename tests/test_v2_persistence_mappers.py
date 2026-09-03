# tests/test_v2_persistence_mappers.py
"""The translation between domain objects and rows, without a database.

`mappers.py` is the only module that imports both shapes, and everything it does
is a pure function of its arguments — so these tests need no PostgreSQL, run in
milliseconds, and can build the rows the database would refuse in order to prove
what the mapper does with them.

Three properties are worth more than the column-by-column checks. A round trip
must be lossless, or a posting quietly loses its salary on the way to storage.
Absence must survive: six NULL location columns are "no location", not an empty
`Location` the domain would reject. And the surrogate keys the tables need but
the domain does not model must be *derived*, because a random key turns a retried
import into a second child row for the same parent.
"""
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.app.domain.common import (
    Location,
    Reason,
    ReasonImpact,
    SalaryPeriod,
    SalaryRange,
    WorkloadRange,
)
from backend.app.domain.identifiers import (
    CompanyLocationId,
    EvidenceId,
    MatchEvaluationId,
)
from backend.app.domain.matching import DimensionScore, MatchDimension
from backend.app.infrastructure.database.mappers import (
    company_location_to_domain,
    company_to_domain,
    company_to_row,
    dimension_score_row_id,
    match_evaluation_to_domain,
    match_evaluation_to_row,
    opportunity_to_domain,
    opportunity_to_row,
    reasons_from_json,
    reasons_to_json,
    source_record_row_id,
)
from backend.app.infrastructure.database.models import CompanyLocationRow, OpportunityRow
from tests.v2_builders import (
    COMPANY,
    COMPANY_LOCATION,
    EVALUATION,
    OPPORTUNITY,
    OTHER_OPPORTUNITY,
    a_company,
    a_company_location,
    an_evaluation,
    an_opportunity,
)

EVIDENCE = EvidenceId(UUID("00000000-0000-4000-8000-000000000071"))
SECOND_EVALUATION = MatchEvaluationId(UUID("00000000-0000-4000-8000-000000000042"))
SECOND_SITE = CompanyLocationId(UUID("00000000-0000-4000-8000-000000000036"))


def test_a_posting_survives_the_trip_to_the_tables_and_back():
    """The whole point of the layer, on a posting with every field populated.

    Equality over the entire object rather than a list of columns: a field added
    to `Opportunity` and forgotten in the mapper fails here, which a hand-written
    list of assertions would not.
    """
    posting = an_opportunity()
    assert opportunity_to_domain(opportunity_to_row(posting)) == posting


def test_a_posting_with_nothing_optional_survives_it_too():
    """The sparse case, which is what a real job board usually gives.

    A board that publishes a title and a company name and nothing else must
    produce an `Opportunity` whose empty parts are `None` — not a `SalaryRange`
    with no amounts, and not a `Location` that locates nothing.
    """
    posting = an_opportunity(
        description=None, opportunity_type=None, contract_type=None,
        workplace_mode=None, workload=None, salary=None, location=None,
        posting_language=None, language_requirements=(), posted_at=None,
        application_url=None, dedup_fingerprint=None)
    read_back = opportunity_to_domain(opportunity_to_row(posting))
    assert read_back == posting
    assert (read_back.salary, read_back.workload, read_back.location) == \
        (None, None, None)


def test_six_null_location_columns_are_no_location_and_not_an_empty_one():
    """`Location` refuses to locate nothing, so the mapper must not build one.

    The failure this prevents is not subtle — it is a `ValidationError` raised
    while reading a perfectly valid row — but it would only appear once a posting
    without a location reached the database.
    """
    row = opportunity_to_row(an_opportunity(location=None))
    assert row.location_country is None and row.location_point is None
    assert opportunity_to_domain(row).location is None


def test_a_location_with_only_a_free_text_string_is_still_a_location():
    """One populated column out of six is enough, and `raw` counts.

    What a scraper has before geocoding runs: the string the posting printed. It
    has to survive storage, or Phase 7 has nothing left to geocode.
    """
    posting = an_opportunity(location=Location(raw="quelque part en Suisse"))
    read_back = opportunity_to_domain(opportunity_to_row(posting))
    assert read_back.location is not None
    assert read_back.location.raw == "quelque part en Suisse"
    assert read_back.location.point is None


def an_opportunity_row(**overrides) -> OpportunityRow:
    """A row built by hand, to read back values no valid write could produce."""
    row = opportunity_to_row(an_opportunity())
    for column, value in overrides.items():
        setattr(row, column, value)
    return row


def test_a_salary_missing_its_currency_is_read_as_no_salary_at_all():
    """A partial salary is not a salary, even though a bound is stored.

    `ck_opportunities_salary_complete_or_absent` makes this unreachable through a
    write, and the row is built by hand for that reason: a pre-constraint row from
    an early migration must not come back as an amount with no unit — which is a
    number a UI would happily display next to the wrong currency symbol.
    """
    row = an_opportunity_row(salary_currency=None, salary_period=None)
    assert row.salary_minimum is not None
    assert opportunity_to_domain(row).salary is None


def test_a_salary_with_one_bound_keeps_that_bound():
    """"From 4500" is most of what boards publish, and it is not nothing."""
    posting = an_opportunity(salary=SalaryRange(
        currency="CHF", period=SalaryPeriod.MONTHLY, minimum=Decimal("4500.10")))
    read_back = opportunity_to_domain(opportunity_to_row(posting))
    assert read_back.salary is not None
    assert (read_back.salary.minimum, read_back.salary.maximum) == \
        (Decimal("4500.10"), None)


def test_a_workload_stated_only_in_hours_is_not_lost():
    """Percent and hours are separate column pairs, and either one is enough.

    Swiss postings say "80–100%" and hourly contracts say "20 h/week"; a mapper
    that keyed the reconstruction on the percent columns would drop the second
    kind entirely.
    """
    posting = an_opportunity(workload=WorkloadRange(min_weekly_hours=20.0,
                                                    max_weekly_hours=25.0))
    read_back = opportunity_to_domain(opportunity_to_row(posting))
    assert read_back.workload is not None
    assert read_back.workload.min_percent is None
    assert (read_back.workload.min_weekly_hours,
            read_back.workload.max_weekly_hours) == (20.0, 25.0)


def test_a_second_pass_writes_onto_the_row_it_was_given():
    """The single signature that makes every repository an upsert.

    `row is target` is the assertion: the mapper must mutate the loaded row rather
    than return a detached copy, or SQLAlchemy would see an unchanged row and emit
    no UPDATE. The child is updated in place for the same reason.
    """
    row = opportunity_to_row(an_opportunity())
    source = row.source
    updated = opportunity_to_row(an_opportunity(title="Ingenieure logicielle",
                                                salary=None), row)
    assert updated is row
    assert updated.source is source
    assert updated.title == "Ingenieure logicielle"
    # Cleared, not left behind: an update that only ever sets values would keep
    # advertising a salary the posting no longer mentions.
    assert updated.salary_currency is None
    assert updated.salary_minimum is None


def test_the_source_record_belongs_to_the_row_being_written():
    """The relocation case: the id in hand is not the id in the table.

    A repository that found the row by `dedup_fingerprint` or by source key is
    holding a row whose id differs from the incoming object's. The child key is
    derived from the *row*, so the flush updates the existing source record rather
    than inserting a second one under a foreign key pointing nowhere.
    """
    row = opportunity_to_row(an_opportunity())
    relocated = opportunity_to_row(an_opportunity(id=OTHER_OPPORTUNITY), row)
    assert relocated.id == OPPORTUNITY
    assert relocated.source.id == source_record_row_id(OPPORTUNITY)
    assert relocated.source.opportunity_id == OPPORTUNITY


def test_a_source_record_key_is_derived_and_never_random():
    """Written out as a literal, because the value is part of the stored data.

    Two calls agreeing with each other would also be true of `uuid4` inside a
    cache. The pinned value is what makes a change to the namespace or to the
    seed string a deliberate migration instead of a silent orphaning of every
    child row already written.
    """
    assert source_record_row_id(OPPORTUNITY) == \
        UUID("5b7055d0-6899-513b-8ba8-44c00a406f91")
    assert source_record_row_id(OPPORTUNITY) == source_record_row_id(OPPORTUNITY)
    assert source_record_row_id(OPPORTUNITY) != source_record_row_id(OTHER_OPPORTUNITY)


def test_a_dimension_key_is_derived_from_the_pair_the_constraint_covers():
    """`(evaluation, dimension)` — the same pair as the unique constraint.

    Re-evaluating a candidate against a posting has to update the six rows already
    there. Keyed on anything else, a nightly re-scoring pass would add six rows a
    night and `uq_match_dimension_scores_match_evaluation_id_dimension` would
    start rejecting them.
    """
    assert dimension_score_row_id(EVALUATION, MatchDimension.SKILLS_FIT) == \
        UUID("f5a4f20a-908b-59b3-9f33-d79b6c28c729")
    assert dimension_score_row_id(EVALUATION, MatchDimension.SKILLS_FIT) != \
        dimension_score_row_id(EVALUATION, MatchDimension.LANGUAGE_FIT)
    assert dimension_score_row_id(EVALUATION, MatchDimension.SKILLS_FIT) != \
        dimension_score_row_id(SECOND_EVALUATION, MatchDimension.SKILLS_FIT)


def test_re_scoring_updates_the_dimensions_that_are_still_scored():
    """Children matched by dimension, not by list position and not by id.

    The three outcomes of a re-evaluation in one assertion: a dimension scored
    again keeps its row (so `created_at` and the key survive), a dimension no
    longer produced is dropped for the `delete-orphan` cascade to delete, and a
    new one is added.
    """
    row = match_evaluation_to_row(an_evaluation(dimensions=(
        DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.5),
        DimensionScore(dimension=MatchDimension.LANGUAGE_FIT, score=0.4))))
    kept = next(child for child in row.dimensions
                if child.dimension is MatchDimension.SKILLS_FIT)

    updated = match_evaluation_to_row(an_evaluation(dimensions=(
        DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.92),
        DimensionScore(dimension=MatchDimension.EXPERIENCE_FIT, score=0.8))), row)
    by_dimension = {child.dimension: child for child in updated.dimensions}
    assert set(by_dimension) == {MatchDimension.SKILLS_FIT,
                                 MatchDimension.EXPERIENCE_FIT}
    assert by_dimension[MatchDimension.SKILLS_FIT] is kept
    assert kept.score == 0.92


def test_an_evaluation_survives_the_trip_with_its_reasons_and_evidence():
    """Scores are the number; the reasons are why anyone should believe it.

    `evidence_ids` is the part that has to survive as UUIDs: it is what makes a
    reason auditable back to a `CandidateEvidence` record, and JSON has no UUID
    type, so this is exactly where the type is lost if the conversion is one-way.
    """
    evaluation = an_evaluation(
        evidence_confidence=0.75,
        evaluator_key="test_evaluator",
        reasons=(Reason(code="STRONG_MATCH", detail="five years of the stack",
                        impact=ReasonImpact.POSITIVE, evidence_ids=(EVIDENCE,)),))
    read_back = match_evaluation_to_domain(match_evaluation_to_row(evaluation))
    assert read_back == evaluation
    assert read_back.reasons[0].evidence_ids == (EVIDENCE,)
    assert isinstance(read_back.reasons[0].evidence_ids[0], UUID)


def test_reasons_are_stored_in_a_form_the_json_driver_accepts():
    """`mode="json"`, which is not a detail: psycopg refuses a `UUID` in JSONB.

    The failure without it is a driver-level `ProgrammingError` at flush time, far
    from the mapper, on the one code path that carries auditable evidence.
    """
    payload = reasons_to_json((Reason(code="STRONG_MATCH", detail="why",
                                      evidence_ids=(EVIDENCE,)),))
    assert payload == [{"code": "STRONG_MATCH", "detail": "why",
                        "impact": "NEUTRAL", "evidence_ids": [str(EVIDENCE)]}]


def test_a_stored_reason_with_an_unexpected_key_is_refused():
    """The payload is validated on the way out, not trusted.

    JSONB accepts any shape, so the column cannot be the guarantee. A key an older
    version of the code wrote must fail here — loudly, next to the row that has it
    — rather than be dropped and leave a reason that no longer says what it said.
    """
    with pytest.raises(ValidationError):
        reasons_from_json([{"code": "OLD", "detail": "why", "severity": "high"}])


def test_a_company_survives_the_trip_with_all_of_its_sites():
    """The parent and its child collection, including which site is the head office.

    `is_headquarters` is a column on the child rather than a pointer on the parent,
    so a mapper that reassigned the collection without carrying the flag would
    produce a company with no head office and no error.
    """
    company = a_company(
        a_company_location(),
        a_company_location(id=SECOND_SITE,
                           location=Location(country="CH", city="Geneve"),
                           is_headquarters=False))
    read_back = company_to_domain(company_to_row(company))
    assert read_back == company
    assert [site.is_headquarters for site in read_back.locations] == [True, False]


def test_a_site_dropped_from_the_domain_object_is_dropped_from_the_collection():
    """What makes `upsert` reconcile rather than accumulate.

    The row is not deleted here — that is the `delete-orphan` cascade's job at
    flush time, and `test_v2_persistence_repositories.py` proves it against the
    database. What is proved here is that the mapper removes it from the
    collection, which is what the cascade acts on.
    """
    row = company_to_row(a_company(
        a_company_location(),
        a_company_location(id=SECOND_SITE,
                           location=Location(country="CH", city="Geneve"),
                           is_headquarters=False)))
    kept = next(child for child in row.locations if child.id == COMPANY_LOCATION)

    updated = company_to_row(a_company(a_company_location()), row)
    assert [child.id for child in updated.locations] == [COMPANY_LOCATION]
    assert updated.locations[0] is kept


def test_a_site_row_that_locates_nothing_is_refused_by_name():
    """A clear message instead of a Pydantic traceback three frames deeper.

    `ck_company_locations_location_not_empty` makes the row unreachable through the
    database, so this is about the other way in: a hand-built row in a test, or a
    migration that added the columns before the constraint.
    """
    with pytest.raises(ValueError, match=str(COMPANY_LOCATION)):
        company_location_to_domain(CompanyLocationRow(id=COMPANY_LOCATION,
                                                      company_id=COMPANY))
