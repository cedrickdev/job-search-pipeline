# tests/test_v2_common.py
"""The shared value objects: `Reason`, `Location`, salary, workload, languages.

Each of these carries one rule that exists to stop a specific fabrication — a
location that locates nothing, a salary range with no bound, a language level
compared as a string — so the tests are written per rule rather than per field.
"""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.app.domain.common import (
    GeoPoint,
    LanguageLevel,
    LanguageProficiency,
    LanguageRequirement,
    Location,
    Reason,
    ReasonImpact,
    SalaryPeriod,
    SalaryRange,
    Weekday,
    WorkloadRange,
)
from backend.app.domain.identifiers import new_evidence_id


def test_a_reason_is_neutral_and_unsourced_by_default():
    reason = Reason(code="LANGUAGE_BELOW_MINIMUM", detail="asks for C1, holds B2")
    assert reason.impact is ReasonImpact.NEUTRAL
    assert reason.evidence_ids == ()


def test_a_reason_can_point_at_the_evidence_behind_it():
    evidence_id = new_evidence_id()
    reason = Reason(code="SKILL_SUPPORTED", detail="two years of it",
                    impact=ReasonImpact.POSITIVE, evidence_ids=(evidence_id,))
    assert reason.evidence_ids == (evidence_id,)


def test_a_reason_needs_a_detail_a_human_can_read():
    with pytest.raises(ValidationError):
        Reason(code="SKILL_SUPPORTED", detail="   ")


def test_weekday_covers_the_week():
    assert len(list(Weekday)) == 7


def test_a_location_must_locate_something():
    with pytest.raises(ValidationError) as failure:
        Location()
    assert "at least one" in str(failure.value)


@pytest.mark.parametrize("field,value", [
    ("country", "CH"),
    ("region", "Vaud"),
    ("city", "Yverdon-les-Bains"),
    ("postal_code", "1400"),
    ("point", GeoPoint(latitude=46.78, longitude=6.64)),
    ("raw", "Yverdon-les-Bains, Suisse"),
])
def test_any_single_field_is_enough_to_locate(field, value):
    """Discovery is incremental: one usable fragment is a valid location."""
    assert getattr(Location(**{field: value}), field) == value


def test_a_scraped_string_survives_unparsed():
    """The V1 case: free text now, geocoding in Phase 7."""
    location = Location(raw="Yverdon-les-Bains, Suisse")
    assert location.city is None
    assert location.point is None
    assert location.raw == "Yverdon-les-Bains, Suisse"


@pytest.mark.parametrize("latitude,longitude", [
    (90.0, 180.0), (-90.0, -180.0), (0.0, 0.0), (46.78, 6.64),
])
def test_geo_point_accepts_the_wgs84_envelope(latitude, longitude):
    point = GeoPoint(latitude=latitude, longitude=longitude)
    assert (point.latitude, point.longitude) == (latitude, longitude)


@pytest.mark.parametrize("latitude,longitude", [
    (90.1, 0.0), (-90.1, 0.0), (0.0, 180.1), (0.0, -180.1),
])
def test_geo_point_refuses_impossible_coordinates(latitude, longitude):
    with pytest.raises(ValidationError):
        GeoPoint(latitude=latitude, longitude=longitude)


def test_a_salary_keeps_exact_decimals():
    """Money is `Decimal`: a monthly gross of 4500.10 must survive storage."""
    salary = SalaryRange(currency="CHF", period=SalaryPeriod.MONTHLY,
                         minimum=Decimal("4500.10"), maximum=Decimal("5200.00"))
    assert salary.minimum == Decimal("4500.10")
    assert isinstance(salary.minimum, Decimal)


@pytest.mark.parametrize("bounds", [
    {"minimum": Decimal("25")},           # "from 25/hour"
    {"maximum": Decimal("30")},           # "up to 30/hour"
    {"minimum": Decimal("25"), "maximum": Decimal("30")},
])
def test_one_advertised_bound_is_enough(bounds):
    salary = SalaryRange(currency="CHF", period=SalaryPeriod.HOURLY, **bounds)
    assert salary.currency == "CHF"


def test_a_salary_range_with_no_bound_is_not_a_fact():
    """`salary=None` on an opportunity is the honest way to say "not published"."""
    with pytest.raises(ValidationError) as failure:
        SalaryRange(currency="CHF", period=SalaryPeriod.MONTHLY)
    assert "needs a minimum, a maximum, or both" in str(failure.value)


def test_a_salary_range_must_be_ordered():
    with pytest.raises(ValidationError):
        SalaryRange(currency="CHF", period=SalaryPeriod.MONTHLY,
                    minimum=Decimal("5200"), maximum=Decimal("4500"))


@pytest.mark.parametrize("bounds", [
    {"min_percent": 60, "max_percent": 80},        # a 60-80% position
    {"min_weekly_hours": 8.0, "max_weekly_hours": 12.0},   # a student job
    {"max_percent": 50},
    {"min_percent": 40, "min_weekly_hours": 16.0},  # both scales, neither converted
])
def test_a_workload_may_be_quoted_on_either_scale(bounds):
    workload = WorkloadRange(**bounds)
    assert any(value is not None for value in
               (workload.min_percent, workload.max_percent,
                workload.min_weekly_hours, workload.max_weekly_hours))


@pytest.mark.parametrize("bounds", [
    {},                                                    # no bound at all
    {"min_percent": 80, "max_percent": 60},                # unordered
    {"min_weekly_hours": 20.0, "max_weekly_hours": 12.0},  # unordered
    {"min_percent": 0},                                    # 0% is not a workload
    {"max_percent": 101},
    {"min_weekly_hours": 0.0},
    {"max_weekly_hours": 169.0},                           # longer than a week
])
def test_an_unusable_workload_is_refused(bounds):
    with pytest.raises(ValidationError):
        WorkloadRange(**bounds)


def test_language_levels_are_ordered_by_rank_not_by_string():
    ranks = [level.rank for level in LanguageLevel]
    assert ranks == sorted(ranks), "declaration order must follow the CEFR scale"
    assert len(set(ranks)) == len(ranks)
    assert LanguageLevel.NATIVE.rank == max(ranks)
    # The reason `rank` exists: "B2" < "C1" happens to hold as a string, "NATIVE"
    # sorts before both, and nobody should have to know which.
    assert LanguageLevel.NATIVE.rank > LanguageLevel.C2.rank


@pytest.mark.parametrize("held,minimum,expected", [
    (LanguageLevel.B2, LanguageLevel.B1, True),
    (LanguageLevel.B2, LanguageLevel.B2, True),
    (LanguageLevel.B1, LanguageLevel.B2, False),
    (LanguageLevel.NATIVE, LanguageLevel.C2, True),
    (LanguageLevel.A1, LanguageLevel.NATIVE, False),
])
def test_meets_compares_on_the_scale(held, minimum, expected):
    assert held.meets(minimum) is expected


def test_a_requirement_is_satisfied_by_a_high_enough_proficiency():
    held = (LanguageProficiency(language="fr", level=LanguageLevel.NATIVE),
            LanguageProficiency(language="en", level=LanguageLevel.B2))
    assert LanguageRequirement(language="fr",
                               minimum_level=LanguageLevel.C1).is_satisfied_by(held)
    assert LanguageRequirement(language="en",
                               minimum_level=LanguageLevel.B2).is_satisfied_by(held)
    # Below the bar, and a language the candidate never declared.
    assert not LanguageRequirement(language="en",
                                   minimum_level=LanguageLevel.C1).is_satisfied_by(held)
    assert not LanguageRequirement(language="de",
                                   minimum_level=LanguageLevel.A2).is_satisfied_by(held)
    assert not LanguageRequirement(language="fr",
                                   minimum_level=LanguageLevel.A1).is_satisfied_by(())


def test_a_nice_to_have_language_is_marked_as_such():
    """`required=False` is what keeps a bonus language out of eligibility."""
    assert LanguageRequirement(language="de", minimum_level=LanguageLevel.A2).required
    optional = LanguageRequirement(language="de", minimum_level=LanguageLevel.A2,
                                   required=False)
    assert optional.required is False
