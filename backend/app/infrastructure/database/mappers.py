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

from pydantic import SecretStr, TypeAdapter
from sqlalchemy.orm.attributes import flag_modified

from backend.app.domain.application import (
    Application,
    PinnedDocument,
)
from backend.app.domain.application_answer import ApplicationAnswer
from backend.app.domain.application_event import (
    ApplicationEvent,
    SubmissionAttempt,
)
from backend.app.domain.candidate import (
    Availability,
    CandidateClaim,
    CandidateEvidence,
    CandidateProfile,
    ClaimType,
    EvidenceKind,
    EvidenceProvenance,
    WeeklyAvailabilitySlot,
    WorkAuthorization,
    WorkAuthorizationStatus,
)
from backend.app.domain.common import (
    LanguageLevel,
    LanguageProficiency,
    LanguageRequirement,
    Location,
    LocationPrecision,
    LocationProvenance,
    Reason,
    SalaryRange,
    Weekday,
    WorkloadRange,
)
from backend.app.domain.company import (
    CareerSite,
    Company,
    CompanyAlias,
    CompanyDiscoveryRecord,
    CompanyLocation,
    DetectedATS,
    Evidence,
    SpontaneousApplicationChannel,
)
from backend.app.domain.decision import ApplicationDecision
from backend.app.domain.documents import (
    CandidateDocument,
    CandidateDocumentType,
    DocumentArtifactRef,
    DocumentContent,
    DocumentGuardReport,
    DocumentStatus,
    DocumentVersion,
)
from backend.app.domain.eligibility import (
    DeterminationSource,
    EligibilityCheck,
    EligibilityRequirement,
    EligibilityResult,
    EligibilityStatus,
    RuleAuthority,
)
from backend.app.domain.identifiers import (
    SURROGATE_KEY_NAMESPACE,
    ApplicationDecisionId,
    ApplicationEventId,
    ApplicationId,
    ApplicationPolicyId,
    CandidateDocumentId,
    CandidateProfileId,
    CareerSiteId,
    ClaimId,
    CompanyAliasId,
    CompanyDiscoveryRecordId,
    CompanyId,
    CompanyLocationId,
    DocumentVersionId,
    EligibilityResultId,
    EvidenceId,
    LLMConnectionId,
    LLMRunId,
    MatchEvaluationId,
    OpportunityId,
    ProviderSessionId,
    SearchProfileId,
    SubmissionAttemptId,
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
from backend.app.domain.policy import ApplicationPolicy, DimensionThreshold
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
    ApplicationDecisionRow,
    ApplicationEventRow,
    ApplicationPolicyRow,
    ApplicationRow,
    CandidateAvailabilitySlotRow,
    CandidateClaimRow,
    CandidateDocumentRow,
    CandidateEvidenceRow,
    CandidateLanguageRow,
    CandidateProfileRow,
    CandidateWorkAuthorizationRow,
    CompanyAliasRow,
    CompanyCareerSiteRow,
    CompanyDiscoveryRecordRow,
    CompanyLocationRow,
    CompanyRow,
    DocumentVersionRow,
    EligibilityCheckRow,
    EligibilityResultRow,
    LLMConnectionRow,
    LLMRunRow,
    LocationColumnsMixin,
    MatchDimensionScoreRow,
    MatchEvaluationRow,
    OpportunityRow,
    OpportunitySourceRecordRow,
    ProviderSessionRow,
    SearchAreaRow,
    SearchProfileRow,
    SubmissionAttemptRow,
    UserRow,
    UserSessionRow,
)
from backend.app.llm.connection import LLMConnection, LLMProviderType
from backend.app.llm.contracts import TaskPurpose
from backend.app.llm.failures import LLMFailureCode
from backend.app.llm.sessions import ProviderSession
from backend.app.llm.telemetry import LLMRun, LLMRunStatus

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


def eligibility_check_row_id(result_id: EligibilityResultId, ordinal: int) -> UUID:
    """The stable key of one check of one eligibility result.

    Keyed by `(result_id, ordinal)` — the pair the unique constraint covers — for
    the reason `search_area_row_id` is keyed by position: a requirement can repeat
    (two required languages are two `LANGUAGE_MINIMUM` gates), so the requirement
    cannot identify a row and its ordinal is what is left. Re-evaluating a pair
    therefore updates the existing rows in place, and a gate the re-run no longer
    emits is deleted rather than duplicated.
    """
    return uuid5(SURROGATE_KEY_NAMESPACE,
                 f"eligibility_check:{result_id}:{ordinal}")


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


def apply_location_to_row(
        row: LocationColumnsMixin, location: Location | None) -> None:
    """Write a `Location` into the `location_*` columns, or clear them.

    Eleven columns since Phase 7: the six components and the five that describe
    where the coordinates came from. The provenance group is written from the same
    value object in the same call, which is what stops a row keeping the geocoder
    and the timestamp of a point that a later save replaced with the source's own.
    """
    row.location_country = None if location is None else location.country
    row.location_region = None if location is None else location.region
    row.location_city = None if location is None else location.city
    row.location_postal_code = None if location is None else location.postal_code
    row.location_point = None if location is None else location.point
    row.location_raw = None if location is None else location.raw

    # A cleared location is `SOURCE_PROVIDED`/`UNKNOWN` rather than NULL: the two
    # columns are NOT NULL, and those are exactly the defaults `Location` itself
    # applies when nothing is stated.
    row.location_provenance = (LocationProvenance.SOURCE_PROVIDED if location is None
                               else location.provenance)
    row.location_precision = (LocationPrecision.UNKNOWN if location is None
                              else location.precision)
    row.location_confidence = None if location is None else location.confidence
    row.location_geocoder = None if location is None else location.geocoder
    row.location_geocoded_at = None if location is None else location.geocoded_at


def _read_location(row: LocationColumnsMixin) -> Location | None:
    """The `location_*` columns as a `Location`, or `None` if the six are NULL.

    All-NULL has to become `None` rather than an empty `Location`: the domain
    refuses a `Location` that locates nothing, and "no location recorded" is what
    those six NULLs mean.

    Emptiness is judged on the six components alone, deliberately. The provenance
    pair is NOT NULL with a default, so including it would make every row look
    located — and a row whose only content is "the source provided nothing at
    UNKNOWN precision" still locates nothing.
    """
    components = (row.location_country, row.location_region, row.location_city,
                  row.location_postal_code, row.location_point, row.location_raw)
    if not any(component is not None for component in components):
        return None
    return Location(country=row.location_country, region=row.location_region,
                    city=row.location_city, postal_code=row.location_postal_code,
                    point=row.location_point, raw=row.location_raw,
                    provenance=row.location_provenance,
                    precision=row.location_precision,
                    confidence=row.location_confidence,
                    geocoder=row.location_geocoder,
                    geocoded_at=row.location_geocoded_at)



def company_location_to_row(
        location: CompanyLocation,
        row: CompanyLocationRow | None = None) -> CompanyLocationRow:
    """A `CompanyLocation` onto its row, creating it if none is given."""
    target = CompanyLocationRow(id=location.id) if row is None else row
    target.company_id = location.company_id
    target.is_headquarters = location.is_headquarters
    apply_location_to_row(target, location.location)
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


def evidence_to_json(evidence: tuple[Evidence, ...]) -> list[dict[str, Any]]:
    """`Evidence` objects as JSON-safe dicts.

    A separate function from `reasons_to_json` even though the body would be
    identical: `Evidence` and `Reason` are deliberately different shapes (a detection
    points at a URL, a score points at candidate evidence), and one function typed
    over both would have to be typed over `BaseModel`, which is how a `Reason` ends
    up in an `ats_evidence` column.
    """
    return [item.model_dump(mode="json") for item in evidence]


def evidence_from_json(payload: list[dict[str, Any]]) -> tuple[Evidence, ...]:
    """Validate stored evidence back into domain objects, refusing extra keys."""
    return tuple(Evidence.model_validate(item) for item in payload)


def _apply_detected_ats(row: CompanyRow, detected: DetectedATS | None) -> None:
    """Write a `DetectedATS` into the five `ats_*` columns, or clear them.

    All five together, always: `ck_companies_ats_complete_or_absent` refuses a
    partial group, which is what makes a caller unable to leave a stale status behind
    a cleared platform.
    """
    row.ats_platform = None if detected is None else detected.platform
    row.ats_organization_id = None if detected is None else detected.organization_id
    row.ats_status = None if detected is None else detected.status
    row.ats_detected_by = None if detected is None else detected.detected_by
    row.ats_evidence = ([] if detected is None
                        else evidence_to_json(detected.evidence))


def _read_detected_ats(row: CompanyRow) -> DetectedATS | None:
    """The `ats_*` columns as a `DetectedATS`, or `None` when no ATS was detected.

    `ats_platform` decides, and the CHECK guarantees the rest of the group came with
    it, so there is no partial state to reconstruct.
    """
    if row.ats_platform is None or row.ats_status is None \
            or row.ats_detected_by is None:
        return None
    return DetectedATS(platform=row.ats_platform,
                       organization_id=row.ats_organization_id,
                       status=row.ats_status,
                       detected_by=row.ats_detected_by,
                       evidence=evidence_from_json(row.ats_evidence))


def _apply_spontaneous(row: CompanyRow,
                       channel: SpontaneousApplicationChannel | None) -> None:
    """Write a `SpontaneousApplicationChannel` into the four `spontaneous_*` columns."""
    row.spontaneous_support = None if channel is None else channel.support
    row.spontaneous_url = None if channel is None else channel.url
    row.spontaneous_observed_by = None if channel is None else channel.observed_by
    row.spontaneous_evidence = ([] if channel is None
                                else evidence_to_json(channel.evidence))


def _read_spontaneous(row: CompanyRow) -> SpontaneousApplicationChannel | None:
    """The `spontaneous_*` columns as a channel, or `None` when there is none.

    `None` and a channel whose support is `UNKNOWN` are both "nobody has looked", and
    both round-trip unchanged: the column group is NULL for the first and holds
    `UNKNOWN` for the second, so a company that was explicitly examined and found
    undecidable stays distinguishable from one nobody has read.
    """
    if row.spontaneous_support is None:
        return None
    return SpontaneousApplicationChannel(
        support=row.spontaneous_support,
        url=row.spontaneous_url,
        observed_by=row.spontaneous_observed_by,
        evidence=evidence_from_json(row.spontaneous_evidence))


def company_to_row(company: Company, row: CompanyRow | None = None) -> CompanyRow:
    """A `Company` and its locations onto rows.

    When `row` is given its `locations` collection must already be loaded: the
    existing children are matched by id so a location that is still present is
    updated in place, and one that has disappeared from the domain object is
    deleted by the `delete-orphan` cascade.

    `normalized_name` and `normalized_domain` are read off the domain properties and
    never computed here. That is the whole reason they are properties: this function
    cannot write a comparison form that disagrees with the name beside it, because it
    has no normalization code of its own to get wrong.

    Aliases, career sites and discovery records are *not* written from here even
    though they belong to the same employer. Each is its own idempotent upsert (§5,
    §11), because a provider that knows one careers endpoint must not delete the
    endpoints another provider found — which is exactly what a wholesale
    `delete-orphan` reassignment would do.
    """
    target = CompanyRow(id=company.id) if row is None else row
    target.name = company.name
    target.normalized_name = company.normalized_name
    target.website = company.website
    target.careers_url = company.careers_url
    target.normalized_domain = company.normalized_domain
    target.country = company.country
    target.identity_status = company.identity_status
    _apply_detected_ats(target, company.detected_ats)
    target.accepts_spontaneous_applications = company.accepts_spontaneous_applications
    _apply_spontaneous(target, company.spontaneous_application_channel)
    existing = {} if row is None else {child.id: child for child in row.locations}
    target.locations = [company_location_to_row(location, existing.get(location.id))
                        for location in company.locations]
    return target


def company_to_domain(row: CompanyRow) -> Company:
    """A `companies` row as a `Company`.

    `normalized_name` and `normalized_domain` are deliberately not passed: the domain
    derives them, and a constructor that accepted them would let a stale column
    override the name it is supposed to describe. A row whose column disagrees is a
    bug the schema-drift and round-trip tests catch, not something to propagate.
    """
    return Company(
        id=CompanyId(row.id),
        name=row.name,
        website=row.website,
        careers_url=row.careers_url,
        country=row.country,
        identity_status=row.identity_status,
        detected_ats=_read_detected_ats(row),
        spontaneous_application_channel=_read_spontaneous(row),
        locations=tuple(company_location_to_domain(child) for child in row.locations),
        accepts_spontaneous_applications=row.accepts_spontaneous_applications)


def company_alias_to_row(alias: CompanyAlias,
                         row: CompanyAliasRow | None = None) -> CompanyAliasRow:
    """A `CompanyAlias` onto its row.

    `first_seen_at` is written like any other column, which makes this function a
    plain projection; keeping the *earliest* first sighting across passes is the
    repository's job, because only it can see what is already stored.
    """
    target = CompanyAliasRow(id=alias.id) if row is None else row
    target.company_id = alias.company_id
    target.alias = alias.alias
    target.normalized_alias = alias.normalized_alias
    target.source_key = alias.source_key
    target.first_seen_at = alias.first_seen_at
    target.last_seen_at = alias.last_seen_at
    return target


def company_alias_to_domain(row: CompanyAliasRow) -> CompanyAlias:
    return CompanyAlias(id=CompanyAliasId(row.id),
                        company_id=CompanyId(row.company_id),
                        alias=row.alias,
                        source_key=row.source_key,
                        first_seen_at=row.first_seen_at,
                        last_seen_at=row.last_seen_at)


def career_site_to_row(site: CareerSite,
                       row: CompanyCareerSiteRow | None = None,
                       ) -> CompanyCareerSiteRow:
    """A `CareerSite` onto its row."""
    target = CompanyCareerSiteRow(id=site.id) if row is None else row
    target.company_id = site.company_id
    target.url = site.url
    target.kind = site.kind
    target.platform = site.platform
    target.source_key = site.source_key
    target.verification_status = site.verification_status
    target.discovered_at = site.discovered_at
    target.last_checked_at = site.last_checked_at
    return target


def career_site_to_domain(row: CompanyCareerSiteRow) -> CareerSite:
    return CareerSite(id=CareerSiteId(row.id),
                      company_id=CompanyId(row.company_id),
                      url=row.url,
                      kind=row.kind,
                      platform=row.platform,
                      source_key=row.source_key,
                      verification_status=row.verification_status,
                      discovered_at=row.discovered_at,
                      last_checked_at=row.last_checked_at)


def discovery_record_to_row(record: CompanyDiscoveryRecord,
                            row: CompanyDiscoveryRecordRow | None = None,
                            ) -> CompanyDiscoveryRecordRow:
    """A `CompanyDiscoveryRecord` onto its row.

    `raw` is copied into a new dict rather than assigned: the domain object is frozen
    but its `dict` field is not, and handing the same object to SQLAlchemy would let
    a later mutation of the domain model change what is flushed.
    """
    target = (CompanyDiscoveryRecordRow(id=record.id) if row is None else row)
    target.provider_key = record.provider_key
    target.external_id = record.external_id
    target.seed_kind = record.seed_kind
    target.company_id = record.company_id
    target.company_name = record.company_name
    target.source_url = record.source_url
    target.discovered_at = record.discovered_at
    target.confidence = record.confidence
    target.raw = dict(record.raw)
    return target


def discovery_record_to_domain(
        row: CompanyDiscoveryRecordRow) -> CompanyDiscoveryRecord:
    """A `company_discovery_records` row as a domain record.

    Re-validating `raw` through the model is what re-applies
    `_raw_carries_no_secrets` to a payload written by an older version of this code:
    a credential-shaped key that somehow reached the table fails here rather than
    being handed to an API response.
    """
    return CompanyDiscoveryRecord(
        id=CompanyDiscoveryRecordId(row.id),
        provider_key=row.provider_key,
        external_id=row.external_id,
        seed_kind=row.seed_kind,
        company_id=None if row.company_id is None else CompanyId(row.company_id),
        company_name=row.company_name,
        source_url=row.source_url,
        discovered_at=row.discovered_at,
        confidence=row.confidence,
        raw=dict(row.raw))


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
    apply_location_to_row(target, opportunity.location)
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


def eligibility_check_to_row(
        check: EligibilityCheck, result_id: EligibilityResultId, ordinal: int,
        row: EligibilityCheckRow | None = None) -> EligibilityCheckRow:
    """One `EligibilityCheck` onto its row, keyed by position within the result.

    `evidence_ids` is stored as a `TEXT[]` since Phase 10 gave the evidence store a
    home — the provenance of the candidate records a gate rested on. Every other
    field is a plain projection, and the three `ck_eligibility_checks_*` constraints
    police the combinations the domain validator does — so a caller cannot write a
    refusal with no reason, an LLM-decided verdict or a pack-blocked one that is not
    verified, whether or not the value came through the model.
    """
    target = (EligibilityCheckRow(id=eligibility_check_row_id(result_id, ordinal))
              if row is None else row)
    target.result_id = result_id
    target.ordinal = ordinal
    target.requirement = check.requirement
    target.status = check.status
    target.determined_by = check.determined_by
    target.authority = check.authority
    target.detail = check.detail
    target.reasons = reasons_to_json(check.reasons)
    target.evidence_ids = [str(evidence_id) for evidence_id in check.evidence_ids]
    return target


def eligibility_check_to_domain(row: EligibilityCheckRow) -> EligibilityCheck:
    """A row as an `EligibilityCheck`, re-validated through the domain.

    `evidence_ids` is read back from the `TEXT[]` column. Re-validating re-applies
    `_verdict_is_accountable`, so a row that reached the table past the CHECKs (a
    hand-built one in a test) still fails here rather than producing a check the
    engine could never have built.
    """
    return EligibilityCheck(
        requirement=EligibilityRequirement(row.requirement),
        status=EligibilityStatus(row.status),
        determined_by=DeterminationSource(row.determined_by),
        authority=RuleAuthority(row.authority),
        detail=row.detail,
        reasons=reasons_from_json(row.reasons),
        evidence_ids=_evidence_ids_from_strings(row.evidence_ids))


def eligibility_result_to_row(result: EligibilityResult,
                              row: EligibilityResultRow | None = None
                              ) -> EligibilityResultRow:
    """An `EligibilityResult` and its checks onto rows.

    `status` is written from the domain's *derived* property, never a second field:
    the denormalized column exists so a list can rank and filter without loading
    every check, and the property is the one source of truth it copies. Existing
    children are matched by `ordinal` — the natural key, because a requirement can
    repeat — so re-evaluating a pair updates the rows already there and a gate the
    re-run dropped is deleted by the cascade.
    """
    target = EligibilityResultRow(id=result.id) if row is None else row
    target.user_id = result.user_id
    target.candidate_profile_id = result.candidate_profile_id
    target.opportunity_id = result.opportunity_id
    target.status = result.status
    target.policy_version = result.policy_version
    target.determined_at = result.determined_at
    # As in `match_evaluation_to_row`: the children hang from the row being written
    # — the one found by (candidate_profile_id, opportunity_id) — not necessarily
    # from `result.id`, so the id used to key them is the row's.
    result_id = EligibilityResultId(target.id)
    existing = ({} if row is None
                else {child.ordinal: child for child in row.checks})
    target.checks = [
        eligibility_check_to_row(check, result_id, ordinal, existing.get(ordinal))
        for ordinal, check in enumerate(result.checks)]
    return target


def eligibility_result_to_domain(row: EligibilityResultRow) -> EligibilityResult:
    """A row and its loaded checks as an `EligibilityResult`.

    `status` is *not* passed: it is a derived property, and the domain recomputes it
    from the checks. The stored column is a denormalized copy for querying, never an
    input the reconstruction could disagree with.
    """
    return EligibilityResult(
        id=EligibilityResultId(row.id),
        user_id=UserId(row.user_id),
        candidate_profile_id=CandidateProfileId(row.candidate_profile_id),
        opportunity_id=OpportunityId(row.opportunity_id),
        checks=tuple(eligibility_check_to_domain(child) for child in row.checks),
        policy_version=row.policy_version,
        determined_at=row.determined_at)


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
    target.evidence_ids = [str(evidence_id)
                           for evidence_id in authorization.evidence_ids]
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


def _evidence_to_row(evidence: CandidateEvidence, profile_id: CandidateProfileId,
                     ordinal: int, row: CandidateEvidenceRow | None = None
                     ) -> CandidateEvidenceRow:
    """One `CandidateEvidence` onto its row.

    Keyed by the evidence's own id, which the domain carries — no surrogate is
    derived, unlike the language and slot rows, because an `EvidenceId` is a real
    identity a claim points at. `user_id` is not written: the record's owner is the
    profile's owner, and `CandidateProfile` refuses any other, so a column here
    could only disagree.
    """
    target = CandidateEvidenceRow(id=evidence.id) if row is None else row
    target.profile_id = profile_id
    target.ordinal = ordinal
    target.kind = evidence.kind
    target.provenance = evidence.provenance
    target.reference_key = evidence.reference_key
    target.summary = evidence.summary
    target.detail = evidence.detail
    target.issued_on = evidence.issued_on
    target.valid_until = evidence.valid_until
    target.source_document = evidence.source_document
    target.recorded_at = evidence.recorded_at
    return target


def _claim_to_row(claim: CandidateClaim, profile_id: CandidateProfileId,
                  ordinal: int, row: CandidateClaimRow | None = None
                  ) -> CandidateClaimRow:
    """One `CandidateClaim` onto its row, its citations as a `TEXT[]` of ids.

    Keyed by the claim's own `ClaimId`. The evidence ids are stored as strings, the
    form the `TEXT[]` column and psycopg both take; `candidate_profile_to_domain`
    turns them back into `EvidenceId`, and `CandidateProfile` re-checks the profile
    holds each one.
    """
    target = CandidateClaimRow(id=claim.id) if row is None else row
    target.profile_id = profile_id
    target.ordinal = ordinal
    target.claim_type = claim.claim_type
    target.label = claim.label
    target.detail = claim.detail
    target.evidence_ids = [str(evidence_id) for evidence_id in claim.evidence_ids]
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
    """A `CandidateProfile` and its five child collections onto rows.

    Evidence and claims are child tables since Phase 10, so a profile carrying
    either is written whole — the gate that once refused them is gone. The claim's
    `evidence_ids` travel as a `TEXT[]` on `candidate_claims`, and because the whole
    profile is reconciled in one call, a claim and the evidence it cites are always
    written together; `_claims_rest_on_held_evidence` re-checks the link on read.

    `updated_at` is written from the domain object and `created_at` is left to the
    server default, for the reason `user_session_to_row` gives: the domain models
    the field it actually has.
    """
    target = CandidateProfileRow(id=profile.id) if row is None else row
    target.user_id = profile.user_id
    target.display_name = profile.display_name
    target.headline = profile.headline
    apply_location_to_row(target, profile.base_location)
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
    evidence = {child.id: child for child in (row.evidence if row else [])}
    target.evidence = [
        _evidence_to_row(item, profile_id, ordinal, evidence.get(item.id))
        for ordinal, item in enumerate(profile.evidence)]
    claims = {child.id: child for child in (row.claims if row else [])}
    target.claims = [
        _claim_to_row(claim, profile_id, ordinal, claims.get(claim.id))
        for ordinal, claim in enumerate(profile.claims)]
    return target


def candidate_profile_to_domain(row: CandidateProfileRow) -> CandidateProfile:
    """A row and its children as a `CandidateProfile`.

    Re-validating through the aggregate re-applies every invariant, including
    `_claims_rest_on_held_evidence`: a stored claim citing an id no evidence row
    carries fails here with a sentence rather than producing a profile the guard
    would then trust. The `TEXT[]` citation columns hold strings, so each is turned
    back into an `EvidenceId` (a `UUID`) on the way out.
    """
    user_id = UserId(row.user_id)
    return CandidateProfile(
        id=CandidateProfileId(row.id),
        user_id=user_id,
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
                              permit_hours_cap=child.permit_hours_cap,
                              evidence_ids=_evidence_ids_from_strings(
                                  child.evidence_ids))
            for child in row.work_authorizations),
        availability=_read_availability(row),
        evidence=tuple(_evidence_to_domain(child, user_id) for child in row.evidence),
        claims=tuple(_claim_to_domain(child, user_id) for child in row.claims),
        updated_at=row.updated_at)


def _evidence_ids_from_strings(values: list[str]) -> tuple[EvidenceId, ...]:
    """A stored `TEXT[]` of citation strings back into typed `EvidenceId`s."""
    return tuple(EvidenceId(UUID(value)) for value in values)


def _evidence_to_domain(row: CandidateEvidenceRow, user_id: UserId
                        ) -> CandidateEvidence:
    """One evidence row as a `CandidateEvidence`, its owner taken from the profile."""
    return CandidateEvidence(
        id=EvidenceId(row.id),
        user_id=user_id,
        kind=EvidenceKind(row.kind),
        provenance=EvidenceProvenance(row.provenance),
        reference_key=row.reference_key,
        summary=row.summary,
        detail=row.detail,
        issued_on=row.issued_on,
        valid_until=row.valid_until,
        source_document=row.source_document,
        recorded_at=row.recorded_at)


def _claim_to_domain(row: CandidateClaimRow, user_id: UserId) -> CandidateClaim:
    """One claim row as a `CandidateClaim`, its owner taken from the profile."""
    return CandidateClaim(
        id=ClaimId(row.id),
        user_id=user_id,
        claim_type=ClaimType(row.claim_type),
        label=row.label,
        detail=row.detail,
        evidence_ids=_evidence_ids_from_strings(row.evidence_ids))


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


# `DocumentContent` is a discriminated union, so a plain `model_validate` would not
# know which member a stored dict is. A `TypeAdapter` over the annotated union reads
# the `kind` discriminator and validates into the right shape, the same way the
# field does inside `DocumentVersion`.
_DOCUMENT_CONTENT_ADAPTER: TypeAdapter[DocumentContent] = TypeAdapter(DocumentContent)


def _document_version_to_row(version: DocumentVersion,
                             document_id: CandidateDocumentId,
                             row: DocumentVersionRow | None = None
                             ) -> DocumentVersionRow:
    """One `DocumentVersion` onto its row.

    Content and the guard report are stored as JSONB documents (`mode="json"` turns
    the evidence-id UUIDs into strings psycopg will take), and `guard_ok` is lifted
    out of the report so the status CHECKs can read the verdict without a JSONB path
    expression. The artifact ref is flattened into the five `artifact_*` columns,
    all NULL until the version is rendered.
    """
    target = DocumentVersionRow(id=version.id) if row is None else row
    target.document_id = document_id
    target.version = version.version
    target.status = version.status
    target.language = version.language
    target.content = version.content.model_dump(mode="json")
    report = version.guard_report
    target.guard_report = None if report is None else report.model_dump(mode="json")
    target.guard_ok = None if report is None else report.ok
    target.generator_key = version.generator_key
    target.created_at = version.created_at
    artifact = version.artifact
    target.artifact_storage_key = None if artifact is None else artifact.storage_key
    target.artifact_media_type = None if artifact is None else artifact.media_type
    target.artifact_byte_size = None if artifact is None else artifact.byte_size
    target.artifact_page_count = None if artifact is None else artifact.page_count
    target.artifact_rendered_at = None if artifact is None else artifact.rendered_at
    return target


def _document_version_to_domain(row: DocumentVersionRow) -> DocumentVersion:
    """One version row back into a `DocumentVersion`, re-validated through the domain.

    `_status_agrees_with_verdict_and_artifact` runs again here, so a row whose
    status, verdict and artifact were made to disagree by hand fails with a sentence
    rather than serving a rejected version as usable.
    """
    report = (None if row.guard_report is None
              else DocumentGuardReport.model_validate(row.guard_report))
    artifact = None
    if row.artifact_storage_key is not None:
        # The artifact columns are written and cleared as one group, gated by the
        # `(status = 'RENDERED') = (artifact_storage_key IS NOT NULL)` constraint, so
        # a row with a storage key always carries its `rendered_at`. Asserting it
        # keeps the invariant legible rather than coercing a `None` into a bad ref.
        assert row.artifact_rendered_at is not None, (
            "a rendered version row must carry artifact_rendered_at")
        artifact = DocumentArtifactRef(
            storage_key=row.artifact_storage_key,
            media_type=row.artifact_media_type or "application/pdf",
            byte_size=row.artifact_byte_size or 0,
            page_count=row.artifact_page_count,
            rendered_at=row.artifact_rendered_at)
    return DocumentVersion(
        id=DocumentVersionId(row.id),
        version=row.version,
        status=DocumentStatus(row.status),
        language=row.language,
        content=_DOCUMENT_CONTENT_ADAPTER.validate_python(row.content),
        guard_report=report,
        artifact=artifact,
        generator_key=row.generator_key,
        created_at=row.created_at)


def candidate_document_to_row(document: CandidateDocument,
                              row: CandidateDocumentRow | None = None
                              ) -> CandidateDocumentRow:
    """A `CandidateDocument` and its versions onto rows.

    The versions hang from the row being written and are matched by their version
    number, so appending a version updates the parent and inserts one child rather
    than rewriting the history. `updated_at` is written from the domain object;
    `created_at` is left to the server default, as elsewhere.
    """
    target = CandidateDocumentRow(id=document.id) if row is None else row
    target.user_id = document.user_id
    target.candidate_profile_id = document.candidate_profile_id
    target.opportunity_id = document.opportunity_id
    target.document_type = document.document_type
    target.updated_at = document.updated_at
    document_id = CandidateDocumentId(target.id)
    existing = {child.version: child for child in (row.versions if row else [])}
    target.versions = [
        _document_version_to_row(version, document_id, existing.get(version.version))
        for version in document.versions]
    return target


def candidate_document_to_domain(row: CandidateDocumentRow) -> CandidateDocument:
    """A document row and its versions as a `CandidateDocument`.

    Re-validating through the aggregate re-applies `_versions_are_ordered_and_typed`
    — strictly increasing numbers, unique ids, every version's content matching the
    document's declared type — so a hand-built row that violated any of them fails
    here rather than producing a document a surface would misrender.
    """
    return CandidateDocument(
        id=CandidateDocumentId(row.id),
        user_id=UserId(row.user_id),
        candidate_profile_id=CandidateProfileId(row.candidate_profile_id),
        opportunity_id=OpportunityId(row.opportunity_id),
        document_type=CandidateDocumentType(row.document_type),
        versions=tuple(_document_version_to_domain(child) for child in row.versions),
        created_at=row.created_at,
        updated_at=row.updated_at)


# Phase 11: the provider-neutral LLM platform's persisted values. Three plain
# projections — a connection, a provider session, a telemetry run — each re-validated
# through its model on the way out, so a row that reached the table past the CHECKs
# (a hand-built one in a test) still fails here rather than producing a value the
# router or the factory would then trust.
#
# The credential is the one field handled with care: `encrypted_api_key` is copied as
# the opaque ciphertext it is, never decrypted here — decryption is the factory's job,
# at the instant a provider is built (docs/LLM_PROVIDER_ARCHITECTURE.md §21). This
# module only moves the ciphertext between the row and the value.


def llm_connection_to_row(connection: LLMConnection,
                          row: LLMConnectionRow | None = None) -> LLMConnectionRow:
    """An `LLMConnection` onto its row.

    `created_at` and `updated_at` are written from the domain object, as on `users`:
    the caller owns the clock, so a test can store a connection created last week.
    `custom_headers` is copied into a new dict rather than assigned, for the reason
    `discovery_record_to_row` copies `raw`: the domain mapping is not frozen, and
    handing the same object to SQLAlchemy would let a later mutation change what is
    flushed.
    """
    target = LLMConnectionRow(id=connection.id) if row is None else row
    target.user_id = connection.user_id
    target.provider_type = connection.provider_type
    target.display_name = connection.display_name
    target.base_url = connection.base_url
    target.model = connection.model
    target.encrypted_api_key = connection.encrypted_api_key
    target.secret_version = connection.secret_version
    target.custom_headers = dict(connection.custom_headers)
    target.enabled = connection.enabled
    target.is_default = connection.is_default
    target.priority = connection.priority
    target.created_at = connection.created_at
    target.updated_at = connection.updated_at
    return target


def llm_connection_to_domain(row: LLMConnectionRow) -> LLMConnection:
    """An `llm_connections` row as an `LLMConnection`, re-validated through the model.

    The transport-shape and secret-pair invariants run again here, so a row that
    somehow reached the table with a CLI connection carrying a base URL fails with a
    sentence rather than being handed to the factory.
    """
    return LLMConnection(
        id=LLMConnectionId(row.id),
        user_id=UserId(row.user_id),
        provider_type=LLMProviderType(row.provider_type),
        display_name=row.display_name,
        base_url=row.base_url,
        model=row.model,
        encrypted_api_key=row.encrypted_api_key,
        secret_version=row.secret_version,
        custom_headers=dict(row.custom_headers),
        enabled=row.enabled,
        is_default=row.is_default,
        priority=row.priority,
        created_at=row.created_at,
        updated_at=row.updated_at)


def provider_session_to_row(session: ProviderSession,
                            row: ProviderSessionRow | None = None
                            ) -> ProviderSessionRow:
    """A `ProviderSession` onto its row.

    `created_at`/`updated_at` are domain-supplied, as on the connection. The id is
    already derived from `(connection_id, conversation_key)` by the domain, so this is
    a plain projection with no key to compute.
    """
    target = ProviderSessionRow(id=session.id) if row is None else row
    target.user_id = session.user_id
    target.connection_id = session.connection_id
    target.conversation_key = session.conversation_key
    target.purpose = session.purpose
    target.external_session_id = session.external_session_id
    target.created_at = session.created_at
    target.updated_at = session.updated_at
    return target


def provider_session_to_domain(row: ProviderSessionRow) -> ProviderSession:
    """A `provider_sessions` row as a `ProviderSession`, re-validated on the way out.

    `_id_is_derived_from_its_key` runs again, so a row whose id disagrees with its
    `(connection_id, conversation_key)` — one a resume would never find — fails here.
    """
    return ProviderSession(
        id=ProviderSessionId(row.id),
        user_id=UserId(row.user_id),
        connection_id=LLMConnectionId(row.connection_id),
        conversation_key=row.conversation_key,
        purpose=TaskPurpose(row.purpose),
        external_session_id=row.external_session_id,
        created_at=row.created_at,
        updated_at=row.updated_at)


def llm_run_to_row(run: LLMRun, row: LLMRunRow | None = None) -> LLMRunRow:
    """An `LLMRun` onto its row.

    `started_at` and `finished_at` are domain facts (the call's own window), so they
    are written from the object; `created_at`/`updated_at` are left to the server
    defaults, the row-write bookkeeping. Every token, cost and latency field is copied
    as-is, keeping the unknown as NULL (§58) rather than a fabricated 0.
    """
    target = LLMRunRow(id=run.id) if row is None else row
    target.user_id = run.user_id
    target.connection_id = run.connection_id
    target.provider_key = run.provider_key
    target.provider_type = run.provider_type
    target.model = run.model
    target.purpose = run.purpose
    target.status = run.status
    target.prompt_name = run.prompt_name
    target.prompt_version = run.prompt_version
    target.prompt_tokens = run.prompt_tokens
    target.completion_tokens = run.completion_tokens
    target.total_tokens = run.total_tokens
    target.cost_usd = run.cost_usd
    target.latency_ms = run.latency_ms
    target.failure_code = run.failure_code
    target.failure_detail = run.failure_detail
    target.fallback_from = run.fallback_from
    target.fallback_reason = run.fallback_reason
    target.started_at = run.started_at
    target.finished_at = run.finished_at
    return target


def llm_run_to_domain(row: LLMRunRow) -> LLMRun:
    """An `llm_runs` row as an `LLMRun`, re-validated through the model.

    `_status_agrees_with_shape` and `_fallback_pair_is_complete` run again, so a row
    that reached the table past the CHECKs still cannot become a run claiming a
    success with a failure code or a fallback with no reason.
    """
    return LLMRun(
        id=LLMRunId(row.id),
        user_id=None if row.user_id is None else UserId(row.user_id),
        connection_id=(None if row.connection_id is None
                       else LLMConnectionId(row.connection_id)),
        provider_key=row.provider_key,
        provider_type=(None if row.provider_type is None
                       else LLMProviderType(row.provider_type)),
        model=row.model,
        purpose=TaskPurpose(row.purpose),
        status=LLMRunStatus(row.status),
        prompt_name=row.prompt_name,
        prompt_version=row.prompt_version,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        total_tokens=row.total_tokens,
        cost_usd=row.cost_usd,
        latency_ms=row.latency_ms,
        failure_code=(None if row.failure_code is None
                      else LLMFailureCode(row.failure_code)),
        failure_detail=row.failure_detail,
        fallback_from=row.fallback_from,
        fallback_reason=(None if row.fallback_reason is None
                         else LLMFailureCode(row.fallback_reason)),
        started_at=row.started_at,
        finished_at=row.finished_at)











# ---------------------------------------------------------------------------
# Phase 12 — the application engine. A policy, a decision, an application and its
# two append-only child records. `pinned_documents`, `answers`, `reasons` and
# `dimension_thresholds` are JSONB carrying the domain value objects verbatim —
# `model_dump(mode="json")` in, `model_validate` out — so a value that reached the
# table is re-validated through the domain on the way back, never trusted raw.
# The event and attempt rows are written one at a time by their own repositories
# (the trail only grows), so unlike `match_evaluations` there is no wholesale
# child reconciliation on the application row.
# ---------------------------------------------------------------------------


def application_policy_to_row(policy: ApplicationPolicy,
                              row: ApplicationPolicyRow | None = None
                              ) -> ApplicationPolicyRow:
    target = ApplicationPolicyRow(id=policy.id) if row is None else row
    target.user_id = policy.user_id
    target.name = policy.name
    target.is_active = policy.is_active
    target.mode = policy.mode
    target.require_approval_before_submission = \
        policy.require_approval_before_submission
    target.allowed_opportunity_types = [t.value for t in policy.allowed_opportunity_types]
    target.minimum_overall_score = policy.minimum_overall_score
    target.dimension_thresholds = [
        threshold.model_dump(mode="json") for threshold in policy.dimension_thresholds]
    target.allow_incomplete_eligibility = policy.allow_incomplete_eligibility
    target.allow_spontaneous_applications = policy.allow_spontaneous_applications
    target.max_applications_per_day = policy.max_applications_per_day
    target.max_applications_per_week = policy.max_applications_per_week
    target.created_at = policy.created_at
    target.updated_at = policy.updated_at
    return target


def application_policy_to_domain(row: ApplicationPolicyRow) -> ApplicationPolicy:
    return ApplicationPolicy(
        id=ApplicationPolicyId(row.id),
        user_id=UserId(row.user_id),
        name=row.name,
        is_active=row.is_active,
        mode=row.mode,
        require_approval_before_submission=row.require_approval_before_submission,
        allowed_opportunity_types=tuple(
            OpportunityType(value) for value in row.allowed_opportunity_types),
        minimum_overall_score=row.minimum_overall_score,
        dimension_thresholds=tuple(
            DimensionThreshold.model_validate(entry)
            for entry in row.dimension_thresholds),
        allow_incomplete_eligibility=row.allow_incomplete_eligibility,
        allow_spontaneous_applications=row.allow_spontaneous_applications,
        max_applications_per_day=row.max_applications_per_day,
        max_applications_per_week=row.max_applications_per_week,
        created_at=row.created_at,
        updated_at=row.updated_at)


def application_decision_to_row(decision: ApplicationDecision,
                                row: ApplicationDecisionRow | None = None
                                ) -> ApplicationDecisionRow:
    """A decision onto its row. The embedded match/eligibility snapshots are not
    persisted here — they live in their own tables and the engine re-reads them —
    so a row is the intent plus the ids and reasons it is about."""
    target = ApplicationDecisionRow(id=decision.id) if row is None else row
    target.user_id = decision.user_id
    target.candidate_profile_id = decision.candidate_profile_id
    target.opportunity_id = decision.opportunity_id
    target.company_id = decision.company_id
    target.policy_id = decision.policy_id
    target.kind = decision.kind
    target.reasons = reasons_to_json(decision.reasons)
    target.confidence = decision.confidence
    target.requires_human_review = decision.requires_human_review
    target.decided_by = decision.decided_by
    target.decided_at = decision.decided_at
    return target


def application_decision_to_domain(row: ApplicationDecisionRow) -> ApplicationDecision:
    return ApplicationDecision(
        id=ApplicationDecisionId(row.id),
        user_id=UserId(row.user_id),
        candidate_profile_id=CandidateProfileId(row.candidate_profile_id),
        opportunity_id=(None if row.opportunity_id is None
                        else OpportunityId(row.opportunity_id)),
        company_id=None if row.company_id is None else CompanyId(row.company_id),
        policy_id=(None if row.policy_id is None
                   else ApplicationPolicyId(row.policy_id)),
        kind=row.kind,
        reasons=reasons_from_json(row.reasons),
        confidence=row.confidence,
        requires_human_review=row.requires_human_review,
        decided_by=row.decided_by,
        decided_at=row.decided_at)


def application_to_row(application: Application,
                       row: ApplicationRow | None = None) -> ApplicationRow:
    """An `Application` onto its row, columns only.

    The `events` and `attempts` relationships are deliberately untouched: they are
    append-only and written by their own repositories, so an application upsert must
    not reconcile them (which, with `lazy="raise"`, it could not do without an eager
    load anyway).
    """
    target = ApplicationRow(id=application.id) if row is None else row
    target.user_id = application.user_id
    target.candidate_profile_id = application.candidate_profile_id
    target.decision_id = application.decision_id
    target.channel = application.channel
    target.state = application.state
    target.idempotency_key = application.idempotency_key
    target.opportunity_id = application.opportunity_id
    target.company_id = application.company_id
    target.policy_id = application.policy_id
    target.pinned_documents = [
        pin.model_dump(mode="json") for pin in application.pinned_documents]
    target.answers = [answer.model_dump(mode="json") for answer in application.answers]
    target.form_fingerprint = application.form_fingerprint
    target.attempt_count = application.attempt_count
    target.correlation_id = application.correlation_id
    target.created_at = application.created_at
    target.updated_at = application.updated_at
    if row is not None:
        # `updated_at` is domain-supplied here (the service controls the clock), but
        # `TimestampedMixin` also carries `onupdate=func.now()`. Submission moves an
        # application through SUBMITTING → SUBMITTED within one call at a single
        # instant, so the second upsert leaves `updated_at` unchanged; without this
        # flag SQLAlchemy would drop it from the SET clause, let the DB onupdate
        # overwrite it, and then expire it — which under asyncio is a MissingGreenlet
        # when the mapper reads it back. Forcing it into every UPDATE keeps the
        # domain's value authoritative and the attribute loaded.
        flag_modified(target, "updated_at")
    return target


def application_to_domain(row: ApplicationRow) -> Application:
    return Application(
        id=ApplicationId(row.id),
        user_id=UserId(row.user_id),
        candidate_profile_id=CandidateProfileId(row.candidate_profile_id),
        decision_id=ApplicationDecisionId(row.decision_id),
        channel=row.channel,
        state=row.state,
        idempotency_key=row.idempotency_key,
        opportunity_id=(None if row.opportunity_id is None
                        else OpportunityId(row.opportunity_id)),
        company_id=None if row.company_id is None else CompanyId(row.company_id),
        policy_id=(None if row.policy_id is None
                   else ApplicationPolicyId(row.policy_id)),
        pinned_documents=tuple(
            PinnedDocument.model_validate(entry) for entry in row.pinned_documents),
        answers=tuple(
            ApplicationAnswer.model_validate(entry) for entry in row.answers),
        form_fingerprint=row.form_fingerprint,
        attempt_count=row.attempt_count,
        correlation_id=row.correlation_id,
        created_at=row.created_at,
        updated_at=row.updated_at)


def application_event_to_row(event: ApplicationEvent,
                             row: ApplicationEventRow | None = None
                             ) -> ApplicationEventRow:
    target = ApplicationEventRow(id=event.id) if row is None else row
    target.application_id = event.application_id
    target.event_type = event.event_type
    target.actor = event.actor
    target.from_state = event.from_state
    target.to_state = event.to_state
    target.detail = event.detail
    target.reasons = reasons_to_json(event.reasons)
    target.correlation_id = event.correlation_id
    target.occurred_at = event.occurred_at
    return target


def application_event_to_domain(row: ApplicationEventRow) -> ApplicationEvent:
    return ApplicationEvent(
        id=ApplicationEventId(row.id),
        application_id=ApplicationId(row.application_id),
        event_type=row.event_type,
        actor=row.actor,
        from_state=row.from_state,
        to_state=row.to_state,
        detail=row.detail,
        reasons=reasons_from_json(row.reasons),
        correlation_id=row.correlation_id,
        occurred_at=row.occurred_at)


def submission_attempt_to_row(attempt: SubmissionAttempt,
                              row: SubmissionAttemptRow | None = None
                              ) -> SubmissionAttemptRow:
    target = SubmissionAttemptRow(id=attempt.id) if row is None else row
    target.application_id = attempt.application_id
    target.attempt_number = attempt.attempt_number
    target.adapter_key = attempt.adapter_key
    target.outcome = attempt.outcome
    target.detail = attempt.detail
    target.confirmation_reference = attempt.confirmation_reference
    target.human_required_reason = attempt.human_required_reason
    target.failure_code = attempt.failure_code
    target.correlation_id = attempt.correlation_id
    target.started_at = attempt.started_at
    target.finished_at = attempt.finished_at
    return target


def submission_attempt_to_domain(row: SubmissionAttemptRow) -> SubmissionAttempt:
    return SubmissionAttempt(
        id=SubmissionAttemptId(row.id),
        application_id=ApplicationId(row.application_id),
        attempt_number=row.attempt_number,
        adapter_key=row.adapter_key,
        outcome=row.outcome,
        detail=row.detail,
        confirmation_reference=row.confirmation_reference,
        human_required_reason=row.human_required_reason,
        failure_code=row.failure_code,
        correlation_id=row.correlation_id,
        started_at=row.started_at,
        finished_at=row.finished_at)
