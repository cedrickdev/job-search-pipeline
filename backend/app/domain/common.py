"""Value objects shared by more than one part of the V2 domain.

Everything here is a value: two instances with equal fields are the same thing,
and none of them has an identity or an owner. Entities (`Opportunity`,
`Company`, `CandidateProfile`, …) live in their own modules and compose these.
"""
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.base import (
    CountryCode,
    CurrencyCode,
    DomainModel,
    LanguageCode,
    NonEmptyStr,
    ReasonCode,
)
from backend.app.domain.identifiers import EvidenceId


class ReasonImpact(StrEnum):
    """Whether a reason argues for a candidate/opportunity pair or against it."""

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"


class Reason(DomainModel):
    """One explainable statement behind a score, verdict or decision.

    docs/V2_SPECIFICATION.md §9 requires every score to carry human-readable
    reasons, and §13 requires a decision to say why. Both use this single shape
    so a UI can render "why" identically wherever it appears, and so analytics
    can group by `code` without parsing prose.

    `evidence_ids` is what makes a reason auditable: a claim-based reason points
    at the `CandidateEvidence` records it rests on.
    """

    code: ReasonCode
    detail: NonEmptyStr
    impact: ReasonImpact = ReasonImpact.NEUTRAL
    evidence_ids: tuple[EvidenceId, ...] = ()


class Weekday(StrEnum):
    MONDAY = "MONDAY"
    TUESDAY = "TUESDAY"
    WEDNESDAY = "WEDNESDAY"
    THURSDAY = "THURSDAY"
    FRIDAY = "FRIDAY"
    SATURDAY = "SATURDAY"
    SUNDAY = "SUNDAY"


class GeoPoint(DomainModel):
    """WGS84 coordinates.

    Stored as two floats rather than a geometry type: the domain must not depend
    on PostGIS (docs/ARCHITECTURE.md §11 puts the geometry in the persistence
    layer, which Phase 2 introduces).
    """

    latitude: Annotated[float, Field(ge=-90.0, le=90.0)]
    longitude: Annotated[float, Field(ge=-180.0, le=180.0)]


class Location(DomainModel):
    """A place, at whatever precision the source actually gave us.

    Every field is optional but the whole must say something: a source that
    reports only "Yverdon-les-Bains, Suisse" produces `raw="…"` with nothing
    parsed, and a geocoding pass later fills `city`/`country`/`point`. Keeping
    `raw` is what allows that pass to be re-run and audited instead of guessed
    once and forgotten.
    """

    country: CountryCode | None = None
    region: NonEmptyStr | None = None
    city: NonEmptyStr | None = None
    postal_code: NonEmptyStr | None = None
    point: GeoPoint | None = None
    raw: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _must_locate_something(self) -> Self:
        if not any((self.country, self.region, self.city, self.postal_code,
                    self.point, self.raw)):
            raise ValueError("Location needs at least one of country, region, city, "
                             "postal_code, point or raw")
        return self


class SalaryPeriod(StrEnum):
    HOURLY = "HOURLY"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"


class SalaryRange(DomainModel):
    """Advertised pay, with the currency and the period it is quoted in.

    `Decimal`, not float: a monthly gross of 4500.10 must survive being stored
    and displayed. Both bounds are optional because postings routinely give only
    one ("from 25/hour"), but a range with neither bound carries no information
    and is rejected — an opportunity with no salary data uses `salary=None`
    instead, which is a different and honest statement.
    """

    currency: CurrencyCode
    period: SalaryPeriod
    minimum: Annotated[Decimal, Field(ge=0)] | None = None
    maximum: Annotated[Decimal, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _bounds_are_usable(self) -> Self:
        if self.minimum is None and self.maximum is None:
            raise ValueError("SalaryRange needs a minimum, a maximum, or both")
        if self.minimum is not None and self.maximum is not None \
                and self.minimum > self.maximum:
            raise ValueError("SalaryRange minimum must not exceed maximum")
        return self


class WorkloadRange(DomainModel):
    """How much of a week the opportunity asks for.

    Two scales, because sources use both and neither converts safely: a
    percentage band (a 60-80% position) and a weekly-hours band (a student job
    "8-12h/week"). Percent cannot be turned into hours without knowing the
    local full-time week, which is Country Pack knowledge (Phase 5), so the
    domain keeps whichever the source gave and lets the matcher decide.
    """

    min_percent: Annotated[int, Field(ge=1, le=100)] | None = None
    max_percent: Annotated[int, Field(ge=1, le=100)] | None = None
    min_weekly_hours: Annotated[float, Field(gt=0.0, le=168.0)] | None = None
    max_weekly_hours: Annotated[float, Field(gt=0.0, le=168.0)] | None = None

    @model_validator(mode="after")
    def _bounds_are_usable(self) -> Self:
        if not any((self.min_percent, self.max_percent,
                    self.min_weekly_hours, self.max_weekly_hours)):
            raise ValueError("WorkloadRange needs at least one bound")
        if self.min_percent is not None and self.max_percent is not None \
                and self.min_percent > self.max_percent:
            raise ValueError("WorkloadRange min_percent must not exceed max_percent")
        if self.min_weekly_hours is not None and self.max_weekly_hours is not None \
                and self.min_weekly_hours > self.max_weekly_hours:
            raise ValueError("WorkloadRange min_weekly_hours must not exceed "
                             "max_weekly_hours")
        return self


class LanguageLevel(StrEnum):
    """CEFR levels plus NATIVE.

    CEFR is a cross-border scale rather than one country's vocabulary, so it is
    safe in the universal model; a Country Pack that publishes its own scale maps
    onto these levels instead of adding members here.
    """

    A1 = "A1"
    A2 = "A2"
    B1 = "B1"
    B2 = "B2"
    C1 = "C1"
    C2 = "C2"
    NATIVE = "NATIVE"

    @property
    def rank(self) -> int:
        """Position on the scale. Comparable; the string values are not."""
        return _LANGUAGE_LEVEL_RANK[self]

    def meets(self, minimum: "LanguageLevel") -> bool:
        return self.rank >= minimum.rank


_LANGUAGE_LEVEL_RANK: dict[LanguageLevel, int] = {
    LanguageLevel.A1: 1,
    LanguageLevel.A2: 2,
    LanguageLevel.B1: 3,
    LanguageLevel.B2: 4,
    LanguageLevel.C1: 5,
    LanguageLevel.C2: 6,
    LanguageLevel.NATIVE: 7,
}


class LanguageProficiency(DomainModel):
    """A language the candidate actually has, at the level they have it."""

    language: LanguageCode
    level: LanguageLevel


class LanguageRequirement(DomainModel):
    """A language an opportunity asks for.

    Deliberately distinct from `LanguageProficiency`: conflating "what the job
    needs" with "what the candidate has" is what produces language scores nobody
    can explain. `required=False` marks a nice-to-have, which must not be able to
    fail eligibility.
    """

    language: LanguageCode
    minimum_level: LanguageLevel
    required: bool = True

    def is_satisfied_by(self, proficiencies: tuple[LanguageProficiency, ...]) -> bool:
        """True when one of the candidate's languages reaches the minimum."""
        return any(p.language == self.language and p.level.meets(self.minimum_level)
                   for p in proficiencies)



