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
    UtcDatetime,
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


class GeoDistance(DomainModel):
    """A distance on the ground, held in metres and converted explicitly.

    Phase 7 §1 forbids mixing kilometres and metres silently, and the mixture is
    not hypothetical: `ST_DWithin` on a `geography` column takes metres, a saved
    `RadiusSearchArea` states kilometres, and an API query parameter states
    kilometres too. A bare `float` named `radius` is one refactor away from being
    a thousand times too small.

    So the unit lives in the type. `meters` is the only field, `from_kilometers`
    is the only way a kilometre value enters, and `kilometers` is the only way one
    leaves — every conversion is therefore one of two call sites that can be
    grepped.
    """

    meters: Annotated[float, Field(ge=0.0)]

    @classmethod
    def from_kilometers(cls, kilometers: float) -> "GeoDistance":
        return cls(meters=kilometers * 1000.0)

    @property
    def kilometers(self) -> float:
        return self.meters / 1000.0


class GeoBounds(DomainModel):
    """A map viewport, as four WGS84 edges.

    Phase 7 §20 allows a bounds query beside the radius one, and Phase 8 will send
    exactly this when the user pans. It is a rectangle in coordinate space rather
    than a shape on the ground, which is why it is not expressed with a
    `GeoDistance`: a viewport is what the screen shows, not how far something is.

    `west <= east` is required rather than wrapped. A box crossing the
    antimeridian is a real thing and PostGIS handles it, but "west greater than
    east" is far more often a swapped pair of arguments — and silently treating a
    typo as a box around the Pacific would return an empty result nobody can
    explain. Switzerland is nowhere near ±180°, so the restriction costs this
    deployment nothing and is stated instead of assumed.
    """

    north: Annotated[float, Field(ge=-90.0, le=90.0)]
    south: Annotated[float, Field(ge=-90.0, le=90.0)]
    east: Annotated[float, Field(ge=-180.0, le=180.0)]
    west: Annotated[float, Field(ge=-180.0, le=180.0)]

    @model_validator(mode="after")
    def _the_corners_are_the_right_way_round(self) -> Self:
        if self.south > self.north:
            raise ValueError("GeoBounds south must not be north of north")
        if self.west > self.east:
            raise ValueError(
                "GeoBounds west must not be east of east; a box crossing the "
                "antimeridian is refused rather than guessed at")
        return self

    def contains(self, point: GeoPoint) -> bool:
        """Whether a point falls inside the box.

        Convenience for callers that already hold the coordinates — the database
        is still what decides which rows a bounds query returns (§2), and this
        must never become a second answer to that question.
        """
        return (self.south <= point.latitude <= self.north
                and self.west <= point.longitude <= self.east)


class LocationProvenance(StrEnum):
    """Where a location's coordinates came from (Phase 7 §7).

    The distinction has teeth: §7 forbids overwriting source-provided coordinates
    with lower-confidence geocoder output, and a column that only stored a point
    could not tell the two apart. `MANUAL` is a human correction and outranks
    both — nothing automated may replace it.
    """

    SOURCE_PROVIDED = "SOURCE_PROVIDED"
    GEOCODED = "GEOCODED"
    MANUAL = "MANUAL"


class LocationPrecision(StrEnum):
    """How precisely the coordinates locate the thing (Phase 7 §32).

    A city centroid and a building entrance are both a latitude and a longitude,
    and displaying the first as if it were the second is the specific mistake §32
    names. Carrying the precision is what lets a UI draw a disc instead of a pin,
    and what lets a distance be reported as approximate.

    `UNKNOWN` is the honest answer when there are no coordinates at all, and is
    the only value permitted in that case.
    """

    EXACT_ADDRESS = "EXACT_ADDRESS"
    POSTAL_CODE = "POSTAL_CODE"
    CITY = "CITY"
    REGION = "REGION"
    COUNTRY = "COUNTRY"
    UNKNOWN = "UNKNOWN"


class GeocodingConfidence(StrEnum):
    """How sure the geocoder was, on a scale small enough to mean something.

    Three levels rather than a float: providers report confidence on scales that
    do not compare (Nominatim's `importance` is a popularity measure, not a
    probability), so a number here would imply an accuracy the input does not
    have. Three buckets are enough for the one decision that reads them — §7 and
    §39's "do not let a weaker answer replace a stronger one" — and `rank` is what
    makes that comparison explicit.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def rank(self) -> int:
        """Position on the scale. Comparable; the string values are not."""
        return _GEOCODING_CONFIDENCE_RANK[self]

    def outranks(self, other: "GeocodingConfidence") -> bool:
        return self.rank > other.rank


_GEOCODING_CONFIDENCE_RANK: dict[GeocodingConfidence, int] = {
    GeocodingConfidence.LOW: 1,
    GeocodingConfidence.MEDIUM: 2,
    GeocodingConfidence.HIGH: 3,
}


class Location(DomainModel):
    """A place, at whatever precision the source actually gave us.

    Every field is optional but the whole must say something: a source that
    reports only "Yverdon-les-Bains, Suisse" produces `raw="…"` with nothing
    parsed, and a geocoding pass later fills `city`/`country`/`point`. Keeping
    `raw` is what allows that pass to be re-run and audited instead of guessed
    once and forgotten.

    Phase 7 added the five fields after `raw`, and they describe the *coordinates*
    rather than the place: where the point came from, how precisely it locates
    anything, how sure the geocoder was, which geocoder it was and when. Together
    they are what makes the enrichment pass of §21 safe to re-run — it can see
    that a point is already better than anything it could produce and skip it —
    and what stops a city centroid being drawn as a street address (§32).

    Nothing here is required, because a `Location` that came off a job board has
    no provenance to state beyond "the source said so", which is the default.
    """

    country: CountryCode | None = None
    region: NonEmptyStr | None = None
    city: NonEmptyStr | None = None
    postal_code: NonEmptyStr | None = None
    point: GeoPoint | None = None
    raw: NonEmptyStr | None = None

    provenance: LocationProvenance = LocationProvenance.SOURCE_PROVIDED
    precision: LocationPrecision = LocationPrecision.UNKNOWN
    confidence: GeocodingConfidence | None = None
    geocoder: NonEmptyStr | None = None
    geocoded_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _must_locate_something(self) -> Self:
        if not any((self.country, self.region, self.city, self.postal_code,
                    self.point, self.raw)):
            raise ValueError("Location needs at least one of country, region, city, "
                             "postal_code, point or raw")
        return self

    @model_validator(mode="after")
    def _the_provenance_describes_coordinates_that_exist(self) -> Self:
        """The metadata is about the point, so it cannot outlive the point.

        Three rules, each closing a way the fields could lie. Precision without
        coordinates would claim an accuracy for nothing. Geocoding metadata on a
        location the geocoder never produced would survive a later correction and
        misreport who is responsible for the value. And `GEOCODED` with no point
        is a resolution that did not resolve — which is `NOT_FOUND` on a
        `GeocodingResult`, not a location.
        """
        if self.point is None and self.precision is not LocationPrecision.UNKNOWN:
            raise ValueError(
                f"precision={self.precision} describes coordinates, and this "
                "Location has none; use UNKNOWN")
        if self.provenance is LocationProvenance.GEOCODED:
            if self.point is None:
                raise ValueError("a GEOCODED location must carry the point the "
                                 "geocoder returned")
            if self.geocoder is None:
                raise ValueError("a GEOCODED location must name the geocoder that "
                                 "produced it (§7: provenance is auditable)")
        elif self.confidence is not None or self.geocoder is not None \
                or self.geocoded_at is not None:
            raise ValueError(
                f"provenance={self.provenance} carries geocoding metadata; only a "
                "GEOCODED location may state a geocoder, a confidence or a "
                "geocoded_at")
        return self

    @property
    def is_resolved(self) -> bool:
        """Whether this location can take part in a distance calculation at all."""
        return self.point is not None

    def outranks(self, other: "Location") -> bool:
        """Whether these coordinates must not be replaced by the other's (§7, §39).

        The comparison is deliberately conservative and one-directional: it answers
        "would accepting `other` be a downgrade?", and the enrichment service
        refuses the write when it is. A location with no point never outranks one
        that has a point; a `MANUAL` or `SOURCE_PROVIDED` point always outranks a
        geocoded one, because a human or the employer itself said where the place
        is; and between two geocoded points the confidence decides, with a missing
        confidence treated as the weakest.
        """
        if self.point is None:
            return False
        if other.point is None:
            return True
        automatic = LocationProvenance.GEOCODED
        if self.provenance is not automatic and other.provenance is automatic:
            return True
        if self.provenance is automatic and other.provenance is not automatic:
            return False
        mine = self.confidence
        theirs = other.confidence
        if mine is None:
            return False
        return theirs is None or mine.outranks(theirs)


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



