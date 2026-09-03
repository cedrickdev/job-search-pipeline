"""ORM row ↔ domain model translation.

The only module that knows both shapes, which is the point: the domain does not
import SQLAlchemy and the tables do not import Pydantic, so something has to sit
between them, and it should be one obvious place rather than a `to_dict` on every
class.

Every `*_to_row` function takes an optional existing row. That single signature
is what makes the repositories upserts rather than inserts: a caller that has
loaded a row applies the new values onto it, and a caller that has not gets a new
row with the same field-by-field code. Two functions — one to insert, one to
update — is how a column comes to be written on create and forgotten on update.

Child collections (`Company.locations`, `MatchEvaluation.dimensions`) are
reassigned wholesale, and the relationships are `cascade="all, delete-orphan"`, so
a location dropped from the domain object is deleted from the table. Because the
relationships are `lazy="raise"`, the caller must have eager-loaded the collection
before passing an existing row — the repositories do, and the alternative
(implicit IO inside a mapper, under asyncio) is a bug rather than a convenience.

Surrogate primary keys for child rows are derived with uuid5, not `uuid4`. The
domain does not model an identity for an `OpportunitySourceRecord` or a
`DimensionScore`, but the table needs one, and a random key would make a retried
import insert a second child row for the same parent
(docs/ENGINEERING_STANDARDS.md §Database rules: idempotency keys).
"""
from typing import Any
from uuid import UUID, uuid5

from backend.app.domain.common import (
    LanguageRequirement,
    Location,
    Reason,
    SalaryRange,
    WorkloadRange,
)
from backend.app.domain.company import Company, CompanyLocation
from backend.app.domain.identifiers import (
    CandidateProfileId,
    CompanyId,
    CompanyLocationId,
    MatchEvaluationId,
    OpportunityId,
    UserId,
)
from backend.app.domain.matching import DimensionScore, MatchDimension, MatchEvaluation
from backend.app.domain.opportunity import Opportunity, OpportunitySourceRecord
from backend.app.infrastructure.database.models import (
    CompanyLocationRow,
    CompanyRow,
    LocationColumnsMixin,
    MatchDimensionScoreRow,
    MatchEvaluationRow,
    OpportunityRow,
    OpportunitySourceRecordRow,
)

"""Namespace for primary keys the domain does not carry.

Fixed for the lifetime of the schema: changing it would orphan every child row
written before the change, since nothing would derive their keys again.
"""
SURROGATE_KEY_NAMESPACE = UUID("20b521f6-f70d-4db4-895c-2fe588adf2ce")


def source_record_row_id(opportunity_id: OpportunityId) -> UUID:
    """The stable key of an opportunity's source record."""
    return uuid5(SURROGATE_KEY_NAMESPACE,
                 f"opportunity_source_record:{opportunity_id}")


def dimension_score_row_id(evaluation_id: MatchEvaluationId,
                           dimension: MatchDimension) -> UUID:
    """The stable key of one dimension of one evaluation.

    Keyed by the pair the unique constraint covers, so re-evaluating a pair
    updates the six existing rows instead of inserting six more.
    """
    return uuid5(SURROGATE_KEY_NAMESPACE,
                 f"match_dimension_score:{evaluation_id}:{dimension.value}")


def reasons_to_json(reasons: tuple[Reason, ...]) -> list[dict[str, Any]]:
    """`Reason` objects as JSON-safe dicts.

    `mode="json"` is what makes the UUIDs in `evidence_ids` strings rather than
    `UUID` objects, which psycopg's JSON serializer would refuse.
    """
    return [reason.model_dump(mode="json") for reason in reasons]


def reasons_from_json(payload: list[dict[str, Any]]) -> tuple[Reason, ...]:
    """Validate stored reasons back into domain objects.

    Validating rather than trusting: `Reason` forbids extra keys, so a payload
    written by an older version of this code fails loudly here instead of
    producing a half-populated object further up.
    """
    return tuple(Reason.model_validate(item) for item in payload)


def _apply_location(row: LocationColumnsMixin, location: Location | None) -> None:
    """Write a `Location` into the six `location_*` columns, or clear them."""
    row.location_country = None if location is None else location.country
    row.location_region = None if location is None else location.region
    row.location_city = None if location is None else location.city
    row.location_postal_code = None if location is None else location.postal_code
    row.location_point = None if location is None else location.point
    row.location_raw = None if location is None else location.raw


def _read_location(row: LocationColumnsMixin) -> Location | None:
    """The six `location_*` columns as a `Location`, or `None` if all are NULL.

    All-NULL has to become `None` rather than an empty `Location`: the domain
    refuses a `Location` that locates nothing, and "no location recorded" is what
    those six NULLs mean.
    """
    components = (row.location_country, row.location_region, row.location_city,
                  row.location_postal_code, row.location_point, row.location_raw)
    if not any(component is not None for component in components):
        return None
    return Location(country=row.location_country, region=row.location_region,
                    city=row.location_city, postal_code=row.location_postal_code,
                    point=row.location_point, raw=row.location_raw)


def company_location_to_row(
        location: CompanyLocation,
        row: CompanyLocationRow | None = None) -> CompanyLocationRow:
    """A `CompanyLocation` onto its row, creating it if none is given."""
    target = CompanyLocationRow(id=location.id) if row is None else row
    target.company_id = location.company_id
    target.is_headquarters = location.is_headquarters
    _apply_location(target, location.location)
    return target


def company_location_to_domain(row: CompanyLocationRow) -> CompanyLocation:
    location = _read_location(row)
    if location is None:
        # `ck_company_locations_location_not_empty` makes this unreachable through
        # the database; it is reachable through a hand-built row in a test, and a
        # clear error beats a Pydantic traceback from three frames deeper.
        raise ValueError(f"company_locations row {row.id} locates nothing")
    return CompanyLocation(id=CompanyLocationId(row.id),
                           company_id=CompanyId(row.company_id),
                           location=location,
                           is_headquarters=row.is_headquarters)


def company_to_row(company: Company, row: CompanyRow | None = None) -> CompanyRow:
    """A `Company` and its locations onto rows.

    When `row` is given its `locations` collection must already be loaded: the
    existing children are matched by id so a location that is still present is
    updated in place, and one that has disappeared from the domain object is
    deleted by the `delete-orphan` cascade.
    """
    target = CompanyRow(id=company.id) if row is None else row
    target.name = company.name
    target.website = company.website
    target.careers_url = company.careers_url
    target.accepts_spontaneous_applications = company.accepts_spontaneous_applications
    existing = {} if row is None else {child.id: child for child in row.locations}
    target.locations = [company_location_to_row(location, existing.get(location.id))
                        for location in company.locations]
    return target


def company_to_domain(row: CompanyRow) -> Company:
    return Company(
        id=CompanyId(row.id),
        name=row.name,
        website=row.website,
        careers_url=row.careers_url,
        locations=tuple(company_location_to_domain(child) for child in row.locations),
        accepts_spontaneous_applications=row.accepts_spontaneous_applications)


def _apply_salary(row: OpportunityRow, salary: SalaryRange | None) -> None:
    row.salary_currency = None if salary is None else salary.currency
    row.salary_period = None if salary is None else salary.period
    row.salary_minimum = None if salary is None else salary.minimum
    row.salary_maximum = None if salary is None else salary.maximum


def _read_salary(row: OpportunityRow) -> SalaryRange | None:
    """The salary columns as a `SalaryRange`, or `None` when none was advertised.

    Currency and period decide: `ck_opportunities_salary_complete_or_absent`
    guarantees that if either is set then a bound is too, so there is no partial
    state to reconstruct.
    """
    if row.salary_currency is None or row.salary_period is None:
        return None
    return SalaryRange(currency=row.salary_currency, period=row.salary_period,
                       minimum=row.salary_minimum, maximum=row.salary_maximum)


def _apply_workload(row: OpportunityRow, workload: WorkloadRange | None) -> None:
    row.workload_min_percent = None if workload is None else workload.min_percent
    row.workload_max_percent = None if workload is None else workload.max_percent
    row.workload_min_weekly_hours = (None if workload is None
                                     else workload.min_weekly_hours)
    row.workload_max_weekly_hours = (None if workload is None
                                     else workload.max_weekly_hours)


def _read_workload(row: OpportunityRow) -> WorkloadRange | None:
    bounds = (row.workload_min_percent, row.workload_max_percent,
              row.workload_min_weekly_hours, row.workload_max_weekly_hours)
    if not any(bound is not None for bound in bounds):
        return None
    return WorkloadRange(min_percent=row.workload_min_percent,
                         max_percent=row.workload_max_percent,
                         min_weekly_hours=row.workload_min_weekly_hours,
                         max_weekly_hours=row.workload_max_weekly_hours)


def source_record_to_row(
        record: OpportunitySourceRecord, opportunity_id: OpportunityId,
        row: OpportunitySourceRecordRow | None = None) -> OpportunitySourceRecordRow:
    """An `OpportunitySourceRecord` onto its row.

    The foreign key is set here as well as by the relationship assignment in
    `opportunity_to_row`, so the row is complete on its own — which is what lets
    the importer write a source record without holding the parent object.
    """
    target = (OpportunitySourceRecordRow(id=source_record_row_id(opportunity_id))
              if row is None else row)
    target.opportunity_id = opportunity_id
    target.source_key = record.source_key
    target.external_id = record.external_id
    target.source_url = record.source_url
    target.fetched_at = record.fetched_at
    target.raw = dict(record.raw)
    return target


def source_record_to_domain(row: OpportunitySourceRecordRow) -> OpportunitySourceRecord:
    return OpportunitySourceRecord(source_key=row.source_key,
                                   external_id=row.external_id,
                                   source_url=row.source_url,
                                   fetched_at=row.fetched_at,
                                   raw=dict(row.raw))


def opportunity_to_row(opportunity: Opportunity,
                       row: OpportunityRow | None = None) -> OpportunityRow:
    """An `Opportunity` and its source record onto rows.

    When `row` is given its `source` must already be loaded, and the existing
    child is updated in place rather than replaced: the source record's key is
    derived from the opportunity id, so a replacement object would carry the same
    primary key as the row being deleted and the flush would deadlock on itself.
    """
    target = OpportunityRow(id=opportunity.id) if row is None else row
    target.company_id = opportunity.company_id
    target.company_name = opportunity.company_name
    target.title = opportunity.title
    target.description = opportunity.description
    target.opportunity_type = opportunity.opportunity_type
    target.contract_type = opportunity.contract_type
    target.workplace_mode = opportunity.workplace_mode
    _apply_workload(target, opportunity.workload)
    _apply_salary(target, opportunity.salary)
    _apply_location(target, opportunity.location)
    target.posting_language = opportunity.posting_language
    target.language_requirements = [
        requirement.model_dump(mode="json")
        for requirement in opportunity.language_requirements]
    target.posted_at = opportunity.posted_at
    target.discovered_at = opportunity.discovered_at
    target.application_url = opportunity.application_url
    target.dedup_fingerprint = opportunity.dedup_fingerprint
    # Keyed on the *row*'s id, not the incoming object's. They differ when a
    # repository located the row by fingerprint or by source key rather than by
    # id, and the child has to belong to the row that is actually being written.
    target.source = source_record_to_row(opportunity.source, OpportunityId(target.id),
                                         None if row is None else row.source)
    return target


def opportunity_to_domain(row: OpportunityRow) -> Opportunity:
    """A row and its loaded `source` as an `Opportunity`.

    Re-validated through Pydantic on the way out, so a value that reached the
    table through psql or a migration and violates a domain rule fails here
    instead of propagating as a malformed domain object.
    """
    return Opportunity(
        id=OpportunityId(row.id),
        source=source_record_to_domain(row.source),
        company_name=row.company_name,
        company_id=None if row.company_id is None else CompanyId(row.company_id),
        title=row.title,
        description=row.description,
        opportunity_type=row.opportunity_type,
        contract_type=row.contract_type,
        workplace_mode=row.workplace_mode,
        workload=_read_workload(row),
        salary=_read_salary(row),
        location=_read_location(row),
        posting_language=row.posting_language,
        language_requirements=tuple(
            LanguageRequirement.model_validate(item)
            for item in row.language_requirements),
        posted_at=row.posted_at,
        discovered_at=row.discovered_at,
        application_url=row.application_url,
        dedup_fingerprint=row.dedup_fingerprint)


def dimension_score_to_row(
        score: DimensionScore, evaluation_id: MatchEvaluationId,
        row: MatchDimensionScoreRow | None = None) -> MatchDimensionScoreRow:
    target = (MatchDimensionScoreRow(
        id=dimension_score_row_id(evaluation_id, score.dimension))
        if row is None else row)
    target.match_evaluation_id = evaluation_id
    target.dimension = score.dimension
    target.score = score.score
    target.weight = score.weight
    target.reasons = reasons_to_json(score.reasons)
    return target


def dimension_score_to_domain(row: MatchDimensionScoreRow) -> DimensionScore:
    return DimensionScore(dimension=row.dimension, score=row.score,
                          weight=row.weight,
                          reasons=reasons_from_json(row.reasons))


def match_evaluation_to_row(evaluation: MatchEvaluation,
                            row: MatchEvaluationRow | None = None) -> MatchEvaluationRow:
    """A `MatchEvaluation` and its dimension scores onto rows.

    Existing children are matched by dimension rather than by primary key,
    because the dimension is what the unique constraint and the derived key are
    both built from. Re-evaluating a pair therefore updates the rows already
    there, and a dimension that is no longer computed is deleted.
    """
    target = MatchEvaluationRow(id=evaluation.id) if row is None else row
    target.user_id = evaluation.user_id
    target.candidate_profile_id = evaluation.candidate_profile_id
    target.opportunity_id = evaluation.opportunity_id
    target.overall = evaluation.overall
    target.evidence_confidence = evaluation.evidence_confidence
    target.reasons = reasons_to_json(evaluation.reasons)
    target.evaluator_key = evaluation.evaluator_key
    target.evaluated_at = evaluation.evaluated_at
    existing = ({} if row is None
                else {child.dimension: child for child in row.dimensions})
    # As in `opportunity_to_row`: the children hang from the row being written,
    # which is the row found by (candidate_profile_id, opportunity_id) — the pair
    # the unique constraint covers — and not necessarily from `evaluation.id`.
    evaluation_id = MatchEvaluationId(target.id)
    target.dimensions = [
        dimension_score_to_row(entry, evaluation_id, existing.get(entry.dimension))
        for entry in evaluation.dimensions]
    return target


def match_evaluation_to_domain(row: MatchEvaluationRow) -> MatchEvaluation:
    return MatchEvaluation(
        id=MatchEvaluationId(row.id),
        user_id=UserId(row.user_id),
        candidate_profile_id=CandidateProfileId(row.candidate_profile_id),
        opportunity_id=OpportunityId(row.opportunity_id),
        overall=row.overall,
        dimensions=tuple(dimension_score_to_domain(child)
                         for child in row.dimensions),
        evidence_confidence=row.evidence_confidence,
        reasons=reasons_from_json(row.reasons),
        evaluator_key=row.evaluator_key,
        evaluated_at=row.evaluated_at)





