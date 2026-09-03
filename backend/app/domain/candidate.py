"""The candidate side of the model: profile, evidence and claims.

docs/V2_SPECIFICATION.md §9 and docs/ARCHITECTURE.md §5 require every candidate
fact to be traceable, and CLAUDE.md states the rule bluntly: never fabricate
candidate facts. V1 already enforces that for CVs — `pipeline/tailor_io.py`
rejects tailored content that selects an unknown bullet id, invents a number, or
names a technology absent from the base library. Those gates are the strength
Phase 9 must reuse, so the contracts here are shaped to receive them:

- `CandidateEvidence` is the V2 form of a base-library record, keyed by the same
  stable `reference_key` V1 uses ("acme-checkout");
- `CandidateClaim` cannot exist without at least one evidence id, and
  `CandidateProfile` refuses claims whose evidence it does not hold. V1's
  "unknown bullet id" gate therefore becomes a type-level invariant rather than
  a function someone has to remember to call.

Contact details (email, phone, postal address) are deliberately absent. They are
PII with storage and redaction rules of their own
(docs/ENGINEERING_STANDARDS.md §Security), and nothing in Phase 1 needs them;
modelling them before those rules exist would only spread them into logs.
"""
from datetime import date
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.base import (
    CountryCode,
    DomainModel,
    NonEmptyStr,
    UtcDatetime,
)
from backend.app.domain.common import LanguageProficiency, Location, Weekday
from backend.app.domain.identifiers import (
    CandidateProfileId,
    ClaimId,
    EvidenceId,
    UserId,
)


class EvidenceKind(StrEnum):
    """What sort of record backs a claim.

    `SELF_DECLARATION` is the weakest member and is not a loophole: a candidate
    stating a fact about themselves is a legitimate, *attributed* source. What the
    platform may never do is manufacture evidence — no member of this enum means
    "an LLM inferred it", and Phase 9's extraction pipeline must attach the
    document it read, not its own conclusion.
    """

    CV_BULLET = "CV_BULLET"
    CV_SUMMARY = "CV_SUMMARY"
    EMPLOYMENT_RECORD = "EMPLOYMENT_RECORD"
    DIPLOMA = "DIPLOMA"
    CERTIFICATE = "CERTIFICATE"
    LANGUAGE_ASSESSMENT = "LANGUAGE_ASSESSMENT"
    PORTFOLIO_ITEM = "PORTFOLIO_ITEM"
    REFERENCE = "REFERENCE"
    PERMIT_DOCUMENT = "PERMIT_DOCUMENT"
    SELF_DECLARATION = "SELF_DECLARATION"


class CandidateEvidence(DomainModel):
    """One record attesting something about the candidate.

    `reference_key` is the bridge to V1: the base CV library gives every bullet a
    stable human-authored id, and reusing it means Phase 9 can import the library
    without inventing identities or losing the link back to the YAML.

    `source_document` is a label (a path or a URL), never file content: the
    domain describes where proof lives, it does not carry it.
    """

    id: EvidenceId
    user_id: UserId
    kind: EvidenceKind
    reference_key: NonEmptyStr | None = None
    summary: NonEmptyStr
    detail: NonEmptyStr | None = None
    issued_on: date | None = None
    valid_until: date | None = None
    source_document: NonEmptyStr | None = None
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def _validity_window_is_ordered(self) -> Self:
        if self.issued_on is not None and self.valid_until is not None \
                and self.issued_on > self.valid_until:
            raise ValueError("evidence valid_until must not precede issued_on")
        return self


class ClaimType(StrEnum):
    """The kind of assertion a claim makes."""

    SKILL = "SKILL"
    EXPERIENCE = "EXPERIENCE"
    EDUCATION = "EDUCATION"
    CERTIFICATION = "CERTIFICATION"
    LANGUAGE = "LANGUAGE"
    AVAILABILITY = "AVAILABILITY"
    WORK_AUTHORIZATION = "WORK_AUTHORIZATION"
    ACHIEVEMENT = "ACHIEVEMENT"


class CandidateClaim(DomainModel):
    """Something the platform is willing to say on the candidate's behalf.

    `evidence_ids` is constrained to at least one entry, so an unsupported claim
    is not merely discouraged — it cannot be constructed. This is the domain-level
    form of V1's truth gate, and it is why matching can report an
    `evidence_confidence` that means something.
    """

    id: ClaimId
    user_id: UserId
    claim_type: ClaimType
    label: NonEmptyStr
    detail: NonEmptyStr | None = None
    evidence_ids: Annotated[tuple[EvidenceId, ...], Field(min_length=1)]


class WorkAuthorizationStatus(StrEnum):
    """Right-to-work status, in country-neutral terms.

    A Swiss B/C/L permit, a French titre de séjour and a US H-1B all map onto
    these members from a Country Pack (docs/V2_SPECIFICATION.md §5); the local
    name travels in `WorkAuthorization.permit_label`. `UNKNOWN` exists because
    "not asked yet" must not be silently read as authorized.
    """

    CITIZEN = "CITIZEN"
    PERMANENT_RESIDENT = "PERMANENT_RESIDENT"
    WORK_PERMIT_HELD = "WORK_PERMIT_HELD"
    STUDENT_PERMIT_WITH_WORK_RIGHTS = "STUDENT_PERMIT_WITH_WORK_RIGHTS"
    REQUIRES_SPONSORSHIP = "REQUIRES_SPONSORSHIP"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    UNKNOWN = "UNKNOWN"


class WorkAuthorization(DomainModel):
    """The candidate's right to work in one country.

    `permit_hours_cap` is what makes a whole class of eligibility deterministic:
    a student permit that caps paid work at 15h/week makes a 20h/week student job
    *ineligible*, not merely a poor schedule fit. The cap itself is a legal fact
    a Country Pack supplies (Phase 5); the domain only carries it.
    """

    country: CountryCode
    status: WorkAuthorizationStatus
    permit_label: NonEmptyStr | None = None
    valid_until: date | None = None
    permit_hours_cap: Annotated[float, Field(gt=0.0, le=168.0)] | None = None
    evidence_ids: tuple[EvidenceId, ...] = ()


class WeeklyAvailabilitySlot(DomainModel):
    """A recurring window the candidate can work, in whole local hours.

    Hour granularity, and no timezone: a candidate saying "Saturday mornings"
    means it in the shop's local time, and modelling that as an instant would
    invent precision nobody supplied.
    """

    weekday: Weekday
    start_hour: Annotated[int, Field(ge=0, le=23)]
    end_hour: Annotated[int, Field(ge=1, le=24)]

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Self:
        if self.start_hour >= self.end_hour:
            raise ValueError("availability slot start_hour must precede end_hour")
        return self


class Availability(DomainModel):
    """When, and how much, the candidate wants to work.

    Distinct from `WorkAuthorization.permit_hours_cap` on purpose: this is a
    preference and feeds the `SCHEDULE_FIT` score, while a permit cap is a legal
    limit and feeds eligibility. Collapsing the two would turn "I would rather
    not work Sundays" into "this candidate may not work Sundays".
    """

    earliest_start: date | None = None
    latest_end: date | None = None
    weekly_slots: tuple[WeeklyAvailabilitySlot, ...] = ()
    min_weekly_hours: Annotated[float, Field(ge=0.0, le=168.0)] | None = None
    max_weekly_hours: Annotated[float, Field(gt=0.0, le=168.0)] | None = None
    notice_period_days: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _bounds_are_coherent(self) -> Self:
        if self.earliest_start is not None and self.latest_end is not None \
                and self.earliest_start > self.latest_end:
            raise ValueError("Availability latest_end must not precede earliest_start")
        if self.min_weekly_hours is not None and self.max_weekly_hours is not None \
                and self.min_weekly_hours > self.max_weekly_hours:
            raise ValueError("Availability min_weekly_hours must not exceed "
                             "max_weekly_hours")
        by_day: dict[Weekday, list[WeeklyAvailabilitySlot]] = {}
        for slot in self.weekly_slots:
            by_day.setdefault(slot.weekday, []).append(slot)
        for weekday, slots in by_day.items():
            ordered = sorted(slots, key=lambda s: s.start_hour)
            for earlier, later in zip(ordered, ordered[1:], strict=False):
                if later.start_hour < earlier.end_hour:
                    raise ValueError(f"overlapping availability slots on {weekday}")
        return self


class CandidateProfile(DomainModel):
    """Everything the platform knows about one candidate, in one aggregate.

    User-owned, so it carries `user_id` explicitly: V2 is multi-user and V1's
    single-tenant tables are exactly what must not be reproduced
    (docs/ARCHITECTURE.md §5). The validators below refuse to aggregate another
    user's evidence, which makes cross-user leakage a construction error rather
    than something an authorization check has to catch later.
    """

    id: CandidateProfileId
    user_id: UserId
    display_name: NonEmptyStr
    headline: NonEmptyStr | None = None
    base_location: Location | None = None
    languages: tuple[LanguageProficiency, ...] = ()
    work_authorizations: tuple[WorkAuthorization, ...] = ()
    availability: Availability | None = None
    evidence: tuple[CandidateEvidence, ...] = ()
    claims: tuple[CandidateClaim, ...] = ()
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _identities_are_unique_and_owned(self) -> Self:
        evidence_ids = [item.id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence ids must be unique within a profile")
        claim_ids = [claim.id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim ids must be unique within a profile")
        # Rendered as text: the two id types are deliberately not interchangeable,
        # and this list only ever feeds an error message.
        foreign = [str(item.id) for item in self.evidence
                   if item.user_id != self.user_id]
        foreign += [str(claim.id) for claim in self.claims
                    if claim.user_id != self.user_id]
        if foreign:
            raise ValueError(f"profile carries records owned by another user: {foreign}")
        return self
    @model_validator(mode="after")
    def _claims_rest_on_held_evidence(self) -> Self:
        """No claim may cite evidence this profile does not hold.

        This is V1's "unknown bullet id" gate, promoted from a validation function
        to an invariant: a fabricated claim is unconstructible, so no downstream
        service can forget to check.
        """
        held = {item.id for item in self.evidence}
        for claim in self.claims:
            missing = [eid for eid in claim.evidence_ids if eid not in held]
            if missing:
                raise ValueError(
                    f"claim {claim.id} cites evidence absent from the profile: {missing}")
        for authorization in self.work_authorizations:
            missing = [eid for eid in authorization.evidence_ids if eid not in held]
            if missing:
                raise ValueError(
                    f"work authorization for {authorization.country} cites evidence "
                    f"absent from the profile: {missing}")
        return self

    @model_validator(mode="after")
    def _one_entry_per_language_and_country(self) -> Self:
        languages = [proficiency.language for proficiency in self.languages]
        if len(languages) != len(set(languages)):
            raise ValueError("languages must not repeat a language")
        countries = [item.country for item in self.work_authorizations]
        if len(countries) != len(set(countries)):
            raise ValueError("work_authorizations must not repeat a country")
        return self

    def evidence_for(self, claim: CandidateClaim) -> tuple[CandidateEvidence, ...]:
        """The records backing `claim`, in profile order.

        Pure lookup, and never empty for a claim this profile holds — the
        validators above guarantee it.
        """
        wanted = set(claim.evidence_ids)
        return tuple(item for item in self.evidence if item.id in wanted)

    def authorization_for(self, country: CountryCode) -> WorkAuthorization | None:
        """The candidate's status in one country, or `None` if never recorded.

        `None` means unrecorded, which an eligibility check must report as
        INCOMPLETE rather than treat as a refusal.
        """
        for authorization in self.work_authorizations:
            if authorization.country == country:
                return authorization
        return None
