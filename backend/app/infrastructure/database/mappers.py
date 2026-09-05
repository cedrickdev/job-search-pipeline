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
from typing import Any, Protocol
from uuid import UUID, uuid5

from pydantic import SecretStr

from backend.app.domain.candidate import (
    Availability,
    CandidateProfile,
    WeeklyAvailabilitySlot,
    WorkAuthorization,
    WorkAuthorizationStatus,
)
from backend.app.domain.common import (
    LanguageLevel,
    LanguageProficiency,
    LanguageRequirement,
    Location,
    Reason,
    SalaryRange,
    Weekday,
    WorkloadRange,
)
from backend.app.domain.company import Company, CompanyLocation
from backend.app.domain.identifiers import (
    SURROGATE_KEY_NAMESPACE,
    CandidateProfileId,
    CompanyId,
    CompanyLocationId,
    MatchEvaluationId,
    OpportunityId,
    SearchProfileId,
    UserId,
    UserSessionId,
)
from backend.app.domain.matching import DimensionScore, MatchDimension, MatchEvaluation
from backend.app.domain.opportunity import (
    ContractType,
    Opportunity,
    OpportunitySourceRecord,
    OpportunityType,
    WorkplaceMode,
)
from backend.app.domain.search import (
    CountrySearchArea,
    RadiusSearchArea,
    RemoteOnlySearchArea,
    SearchArea,
    SearchAreaKind,
    SearchProfile,
)
from backend.app.domain.user import User, UserSession, UserStatus
from backend.app.infrastructure.database.models import (
    CandidateAvailabilitySlotRow,
    CandidateLanguageRow,
    CandidateProfileRow,
    CandidateWorkAuthorizationRow,
    CompanyLocationRow,
    CompanyRow,
    LocationColumnsMixin,
    MatchDimensionScoreRow,
    MatchEvaluationRow,
    OpportunityRow,
    OpportunitySourceRecordRow,
    SearchAreaRow,
    SearchProfileRow,
    UserRow,
    UserSessionRow,
)

"""Namespace for primary keys the domain does not carry.

Imported from `backend.app.domain.identifiers` rather than defined here, so the
derived keys in this module and `default_candidate_profile_id` share one namespace
that cannot drift. Fixed for the lifetime of the schema: changing it would orphan
every child row written before the change, since nothing would derive their keys
again.
"""


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


def candidate_language_row_id(profile_id: CandidateProfileId, language: str) -> UUID:
    """The stable key of one language of one profile.

    Keyed by the pair `uq_candidate_languages_profile_id_language` covers, for the
    reason `dimension_score_row_id` exists: saving a profile twice must update the
    rows already there. `LanguageProficiency` carries no id of its own, so without
    this a second save would insert a duplicate and violate the constraint.
    """
    return uuid5(SURROGATE_KEY_NAMESPACE,
                 f"candidate_language:{profile_id}:{language}")


def work_authorization_row_id(profile_id: CandidateProfileId, country: str) -> UUID:
    """The stable key of one country's authorization on one profile."""
    return uuid5(SURROGATE_KEY_NAMESPACE,
                 f"candidate_work_authorization:{profile_id}:{country}")


def availability_slot_row_id(profile_id: CandidateProfileId, weekday: Weekday,
                             start_hour: int) -> UUID:
    """The stable key of one weekly slot on one profile."""
    return uuid5(SURROGATE_KEY_NAMESPACE,
                 f"candidate_availability_slot:{profile_id}:{weekday.value}:{start_hour}")


def search_area_row_id(search_profile_id: SearchProfileId, ordinal: int) -> UUID:
    """The stable key of one area of one saved search.

    Keyed by position because position is all there is: two radius areas can differ
    only by their radius, so a search with three areas has three rows identified by
    being first, second and third. Editing the second area therefore updates the
    second row.
    """
    return uuid5(SURROGATE_KEY_NAMESPACE,
                 f"search_area:{search_profile_id}:{ordinal}")



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


class _WorkloadColumns(Protocol):
    """The four workload columns, wherever they appear.

    `opportunities` carries what a posting offers and `search_profiles` carries
    what a candidate wants; both are a `WorkloadRange`, so both get the same four
    columns and the same pair of functions. A structural type rather than a shared
    mixin because `OpportunityRow` is a Phase 2 table and changing its bases to
    gain nothing but a shorter annotation would rewrite a migration's column order.

    The members are annotated with the *attribute* type, not `Mapped[...]`: on an
    instance a `Mapped[int | None]` descriptor reads and writes `int | None`, and
    a protocol declaring the wrapper matches no row class at all.
    """

    workload_min_percent: int | None
    workload_max_percent: int | None
    workload_min_weekly_hours: float | None
    workload_max_weekly_hours: float | None


def _apply_workload(row: _WorkloadColumns, workload: WorkloadRange | None) -> None:
    row.workload_min_percent = None if workload is None else workload.min_percent
    row.workload_max_percent = None if workload is None else workload.max_percent
    row.workload_min_weekly_hours = (None if workload is None
                                     else workload.min_weekly_hours)
    row.workload_max_weekly_hours = (None if workload is None
                                     else workload.max_weekly_hours)


def _read_workload(row: _WorkloadColumns) -> WorkloadRange | None:
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


# Phase 4: identity, the candidate profile onboarding fills in, and saved searches.
#
# Three of the functions below unwrap a `SecretStr`, and they are the only ones in
# the persistence layer that do: `users.password_hash`, `user_sessions.token_digest`
# and `user_sessions.csrf_token_digest`. Keeping the unwrap here rather than in a
# service means the plain value exists only inside a statement's parameter list —
# never in a domain object a log line might format
# (docs/ENGINEERING_STANDARDS.md §Security).


def user_to_row(user: User, row: UserRow | None = None) -> UserRow:
    """A `User` onto its row.

    `created_at` and `updated_at` are written explicitly rather than left to the
    server defaults, because on this table they are domain fields: the caller owns
    the clock, so a test can write an account that registered last week without
    persuading PostgreSQL that `now()` is in the past.
    """
    target = UserRow(id=user.id) if row is None else row
    target.email = user.email
    target.password_hash = user.password_hash.get_secret_value()
    target.status = user.status
    target.display_name = user.display_name
    target.email_verified_at = user.email_verified_at
    target.last_login_at = user.last_login_at
    target.failed_login_attempts = user.failed_login_attempts
    target.locked_until = user.locked_until
    target.onboarding_completed_at = user.onboarding_completed_at
    target.created_at = user.created_at
    target.updated_at = user.updated_at
    return target


def user_to_domain(row: UserRow) -> User:
    """A row as a `User`, with the hash wrapped again before it can be printed."""
    return User(
        id=UserId(row.id),
        email=row.email,
        password_hash=SecretStr(row.password_hash),
        status=UserStatus(row.status),
        display_name=row.display_name,
        email_verified_at=row.email_verified_at,
        last_login_at=row.last_login_at,
        failed_login_attempts=row.failed_login_attempts,
        locked_until=row.locked_until,
        onboarding_completed_at=row.onboarding_completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at)


def user_session_to_row(session: UserSession,
                        row: UserSessionRow | None = None) -> UserSessionRow:
    """A `UserSession` onto its row, as two digests and a window.

    `created_at` and `updated_at` are *not* set here, unlike on `users`: the domain
    object has `issued_at` and `last_seen_at`, which say the same thing more
    precisely, and the mixin's server defaults are then honest bookkeeping rather
    than a second copy of the same instant.
    """
    target = UserSessionRow(id=session.id) if row is None else row
    target.user_id = session.user_id
    target.token_digest = session.token_digest.get_secret_value()
    target.csrf_token_digest = session.csrf_token_digest.get_secret_value()
    target.issued_at = session.issued_at
    target.expires_at = session.expires_at
    target.last_seen_at = session.last_seen_at
    target.revoked_at = session.revoked_at
    return target


def user_session_to_domain(row: UserSessionRow) -> UserSession:
    return UserSession(
        id=UserSessionId(row.id),
        user_id=UserId(row.user_id),
        token_digest=SecretStr(row.token_digest),
        csrf_token_digest=SecretStr(row.csrf_token_digest),
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        last_seen_at=row.last_seen_at,
        revoked_at=row.revoked_at)


def _language_to_row(proficiency: LanguageProficiency,
                     profile_id: CandidateProfileId, ordinal: int,
                     row: CandidateLanguageRow | None = None) -> CandidateLanguageRow:
    target = (CandidateLanguageRow(
        id=candidate_language_row_id(profile_id, proficiency.language))
        if row is None else row)
    target.profile_id = profile_id
    target.ordinal = ordinal
    target.language = proficiency.language
    target.level = proficiency.level
    return target


def _work_authorization_to_row(
        authorization: WorkAuthorization, profile_id: CandidateProfileId, ordinal: int,
        row: CandidateWorkAuthorizationRow | None = None
) -> CandidateWorkAuthorizationRow:
    target = (CandidateWorkAuthorizationRow(
        id=work_authorization_row_id(profile_id, authorization.country))
        if row is None else row)
    target.profile_id = profile_id
    target.ordinal = ordinal
    target.country = authorization.country
    target.status = authorization.status
    target.permit_label = authorization.permit_label
    target.valid_until = authorization.valid_until
    target.permit_hours_cap = authorization.permit_hours_cap
    return target


def _availability_slot_to_row(
        slot: WeeklyAvailabilitySlot, profile_id: CandidateProfileId, ordinal: int,
        row: CandidateAvailabilitySlotRow | None = None) -> CandidateAvailabilitySlotRow:
    target = (CandidateAvailabilitySlotRow(
        id=availability_slot_row_id(profile_id, slot.weekday, slot.start_hour))
        if row is None else row)
    target.profile_id = profile_id
    target.ordinal = ordinal
    target.weekday = slot.weekday
    target.start_hour = slot.start_hour
    target.end_hour = slot.end_hour
    return target


def _apply_availability(row: CandidateProfileRow,
                        availability: Availability | None) -> None:
    """Write the five scalar availability columns, or clear them.

    The weekly slots are a child collection and are handled by
    `candidate_profile_to_row`, which is the only function holding the ordinals.
    """
    row.availability_earliest_start = (None if availability is None
                                       else availability.earliest_start)
    row.availability_latest_end = (None if availability is None
                                   else availability.latest_end)
    row.availability_min_weekly_hours = (None if availability is None
                                         else availability.min_weekly_hours)
    row.availability_max_weekly_hours = (None if availability is None
                                         else availability.max_weekly_hours)
    row.availability_notice_period_days = (None if availability is None
                                           else availability.notice_period_days)


def _read_availability(row: CandidateProfileRow) -> Availability | None:
    """The availability columns and slot rows as an `Availability`, or `None`.

    All five NULL *and* no slots reads back as "not stated", the same convention
    `_read_location` follows. It makes `Availability()` — a value object with
    nothing set — indistinguishable from absence, which is what it means anyway.
    """
    bounds = (row.availability_earliest_start, row.availability_latest_end,
              row.availability_min_weekly_hours, row.availability_max_weekly_hours,
              row.availability_notice_period_days)
    if not any(bound is not None for bound in bounds) and not row.availability_slots:
        return None
    return Availability(
        earliest_start=row.availability_earliest_start,
        latest_end=row.availability_latest_end,
        weekly_slots=tuple(
            WeeklyAvailabilitySlot(weekday=Weekday(child.weekday),
                                   start_hour=child.start_hour,
                                   end_hour=child.end_hour)
            for child in row.availability_slots),
        min_weekly_hours=row.availability_min_weekly_hours,
        max_weekly_hours=row.availability_max_weekly_hours,
        notice_period_days=row.availability_notice_period_days)


def candidate_profile_to_row(profile: CandidateProfile,
                             row: CandidateProfileRow | None = None
                             ) -> CandidateProfileRow:
    """A `CandidateProfile` and its three child collections onto rows.

    Raises `ValueError` when the profile carries evidence or claims. Phase 10 owns
    the evidence store, and there is no column for either here; writing the profile
    and dropping them would break `_claims_rest_on_held_evidence` on the way back
    out — a claim would return citing evidence the profile no longer holds. The
    check lives in the mapper rather than in the repository because the mapper is
    the layer every path goes through.

    `updated_at` is written from the domain object and `created_at` is left to the
    server default, for the reason `user_session_to_row` gives: the domain models
    the field it actually has.
    """
    if profile.evidence or profile.claims:
        raise ValueError(
            "candidate evidence and claims have no V2 persistence yet (Phase 10); "
            f"profile {profile.id} carries {len(profile.evidence)} evidence records "
            f"and {len(profile.claims)} claims")
    target = CandidateProfileRow(id=profile.id) if row is None else row
    target.user_id = profile.user_id
    target.display_name = profile.display_name
    target.headline = profile.headline
    _apply_location(target, profile.base_location)
    availability = profile.availability
    _apply_availability(target, availability)
    target.updated_at = profile.updated_at
    # As in `opportunity_to_row`, the children hang from the row being written and
    # are matched by their natural key, so re-saving a profile updates the rows
    # already there and a language the candidate removed is deleted.
    profile_id = CandidateProfileId(target.id)
    languages = {child.language: child for child in (row.languages if row else [])}
    target.languages = [
        _language_to_row(proficiency, profile_id, ordinal,
                         languages.get(proficiency.language))
        for ordinal, proficiency in enumerate(profile.languages)]
    authorizations = {child.country: child
                      for child in (row.work_authorizations if row else [])}
    target.work_authorizations = [
        _work_authorization_to_row(authorization, profile_id, ordinal,
                                   authorizations.get(authorization.country))
        for ordinal, authorization in enumerate(profile.work_authorizations)]
    slots = {(child.weekday, child.start_hour): child
             for child in (row.availability_slots if row else [])}
    target.availability_slots = [
        _availability_slot_to_row(slot, profile_id, ordinal,
                                  slots.get((slot.weekday, slot.start_hour)))
        for ordinal, slot in enumerate(
            () if availability is None else availability.weekly_slots)]
    return target


def candidate_profile_to_domain(row: CandidateProfileRow) -> CandidateProfile:
    """A row and its children as a `CandidateProfile`.

    `evidence` and `claims` are left at their defaults — empty — which is the
    truthful reading of a schema that has nowhere to store them.
    """
    return CandidateProfile(
        id=CandidateProfileId(row.id),
        user_id=UserId(row.user_id),
        display_name=row.display_name,
        headline=row.headline,
        base_location=_read_location(row),
        languages=tuple(
            LanguageProficiency(language=child.language,
                                level=LanguageLevel(child.level))
            for child in row.languages),
        work_authorizations=tuple(
            WorkAuthorization(country=child.country,
                              status=WorkAuthorizationStatus(child.status),
                              permit_label=child.permit_label,
                              valid_until=child.valid_until,
                              permit_hours_cap=child.permit_hours_cap)
            for child in row.work_authorizations),
        availability=_read_availability(row),
        updated_at=row.updated_at)


def _area_to_row(area: SearchArea, search_profile_id: SearchProfileId, ordinal: int,
                 row: SearchAreaRow | None = None) -> SearchAreaRow:
    """One `SearchArea` onto the flattened row, narrowed by `isinstance`.

    `isinstance` rather than `getattr(area, "country", None)`: the union's whole
    value is that each shape has exactly the fields it needs, and `getattr` with a
    default would hand a misspelled field name straight to a NULL column. Every
    branch sets *all four* shape columns, including the ones it clears, so
    `ck_search_areas_shape_matches_kind` cannot be tripped by a leftover value from
    the row's previous kind.
    """
    target = (SearchAreaRow(id=search_area_row_id(search_profile_id, ordinal))
              if row is None else row)
    target.search_profile_id = search_profile_id
    target.ordinal = ordinal
    target.kind = area.kind
    target.label = area.label
    if isinstance(area, CountrySearchArea):
        target.country = area.country
        target.center = None
        target.radius_km = None
    elif isinstance(area, RadiusSearchArea):
        target.country = None
        target.center = area.center
        target.radius_km = area.radius_km
    else:
        target.country = area.country
        target.center = None
        target.radius_km = None
    return target


def _area_to_domain(row: SearchAreaRow) -> SearchArea:
    """One row back into the union member its `kind` names.

    Raises `ValueError` on a row the kind's own columns do not support, as
    `company_location_to_domain` does: `ck_search_areas_shape_matches_kind` makes
    it unreachable through PostgreSQL, and a hand-built row in a test deserves a
    sentence rather than a Pydantic traceback.
    """
    kind = SearchAreaKind(row.kind)
    if kind is SearchAreaKind.COUNTRY:
        if row.country is None:
            raise ValueError(f"search_areas row {row.id} is COUNTRY with no country")
        return CountrySearchArea(country=row.country, label=row.label)
    if kind is SearchAreaKind.RADIUS:
        if row.center is None or row.radius_km is None:
            raise ValueError(
                f"search_areas row {row.id} is RADIUS with no centre or radius")
        return RadiusSearchArea(center=row.center, radius_km=row.radius_km,
                                label=row.label)
    return RemoteOnlySearchArea(country=row.country, label=row.label)


def search_profile_to_row(profile: SearchProfile,
                          row: SearchProfileRow | None = None) -> SearchProfileRow:
    """A `SearchProfile` and its areas onto rows.

    The eight filter tuples become `list[str]` because that is what `ARRAY(Text)`
    binds; the enum tuples are written as `member.value` so what lands in the
    column is what `ck_search_profiles_*_members` checks against. An empty tuple
    becomes an empty array, never NULL — the two would mean the same thing and
    only one of them survives a `= ANY`.
    """
    target = SearchProfileRow(id=profile.id) if row is None else row
    target.user_id = profile.user_id
    target.name = profile.name
    target.is_active = profile.is_active
    target.queries = list(profile.queries)
    target.title_keywords = list(profile.title_keywords)
    target.excluded_keywords = list(profile.excluded_keywords)
    target.opportunity_types = [member.value for member in profile.opportunity_types]
    target.contract_types = [member.value for member in profile.contract_types]
    target.workplace_modes = [member.value for member in profile.workplace_modes]
    target.posting_languages = list(profile.posting_languages)
    target.source_keys = list(profile.source_keys)
    _apply_workload(target, profile.workload)
    target.created_at = profile.created_at
    target.updated_at = profile.updated_at
    # Matched by `ordinal`, which is this table's natural key: two radius areas can
    # differ only by their radius, so there is nothing else to identify a row by.
    search_profile_id = SearchProfileId(target.id)
    existing = {child.ordinal: child for child in (row.areas if row else [])}
    target.areas = [_area_to_row(area, search_profile_id, ordinal,
                                 existing.get(ordinal))
                    for ordinal, area in enumerate(profile.areas)]
    return target


def search_profile_to_domain(row: SearchProfileRow) -> SearchProfile:
    """A row and its areas as a `SearchProfile`.

    The enum arrays are re-validated on the way out rather than trusted: the CHECK
    constraints police the column, but a row written by a migration or by hand
    would fail here with the offending value named instead of surfacing later as an
    enum comparison that quietly never matches.
    """
    return SearchProfile(
        id=SearchProfileId(row.id),
        user_id=UserId(row.user_id),
        name=row.name,
        is_active=row.is_active,
        areas=tuple(_area_to_domain(child) for child in row.areas),
        queries=tuple(row.queries),
        title_keywords=tuple(row.title_keywords),
        excluded_keywords=tuple(row.excluded_keywords),
        opportunity_types=tuple(OpportunityType(value)
                                for value in row.opportunity_types),
        contract_types=tuple(ContractType(value) for value in row.contract_types),
        workplace_modes=tuple(WorkplaceMode(value) for value in row.workplace_modes),
        posting_languages=tuple(row.posting_languages),
        workload=_read_workload(row),
        source_keys=tuple(row.source_keys),
        created_at=row.created_at,
        updated_at=row.updated_at)









