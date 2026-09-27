# tests/test_v2_recommendation.py
"""The recommendation guardrails, pinned: evidence or nothing, and no claim from weak data.

`CareerRecommendation` is a suggestion with zero mutation authority, so these tests hold what
makes it trustworthy: every recommendation carries at least one evidence item, each cited metric
must clear `MIN_RECOMMENDATION_SAMPLE_SIZE`, confidence is a pure function of the weakest
evidence's sample (visible uncertainty, never stored), an evidence item cites exactly one metric
shape (a rate or a timing, never both), and the causal-language guard flags cause-and-effect
prose the observational funnel cannot support.
"""
import pytest
from pydantic import ValidationError

from backend.app.domain.analytics import (
    DEFAULT_OBSERVATION_HORIZON_DAYS,
    DimensionKind,
    RateKind,
    TimingKind,
)
from backend.app.domain.identifiers import (
    career_recommendation_evidence_id,
    new_career_recommendation_id,
)
from backend.app.domain.recommendation import (
    HIGH_CONFIDENCE_MIN_SAMPLE,
    MEDIUM_CONFIDENCE_MIN_SAMPLE,
    MIN_RECOMMENDATION_SAMPLE_SIZE,
    CareerRecommendation,
    RecommendationConfidence,
    RecommendationEvidence,
    RecommendationKind,
    asserts_causation,
    confidence_for_sample,
)
from tests.v2_builders import NOW, USER

RECOMMENDATION = new_career_recommendation_id()


def an_evidence(*, ordinal=0, sample_size=20, recommendation_id=RECOMMENDATION, **overrides):
    fields = {
        "id": career_recommendation_evidence_id(recommendation_id, ordinal),
        "recommendation_id": recommendation_id,
        "ordinal": ordinal,
        "dimension": DimensionKind.ROLE_FAMILY,
        "dimension_key": "DATA_AND_ANALYTICS",
        "rate_kind": RateKind.INTERVIEW_CONVERSION,
        "numerator": min(3, sample_size),
        "denominator": sample_size,
        "sample_size": sample_size,
        "detail": "Data & Analytics: interviews from applications",
    }
    fields.update(overrides)
    return RecommendationEvidence(**fields)


_UNSET = object()


def a_recommendation(evidence=_UNSET, **overrides):
    if evidence is _UNSET:
        evidence = (an_evidence(),)
    fields = {
        "id": RECOMMENDATION,
        "user_id": USER,
        "kind": RecommendationKind.PRIORITIZE_ROLE_FAMILY,
        "analytics_version": "career-analytics/1.0",
        "summary": "Your Data & Analytics applications reach interviews more often.",
        "evidence": evidence,
        "analytics_computed_at": NOW,
        "observation_horizon_days": DEFAULT_OBSERVATION_HORIZON_DAYS,
        "created_at": NOW,
    }
    fields.update(overrides)
    return CareerRecommendation(**fields)


# --- confidence is sample strength, made visible ---------------------------------------


@pytest.mark.parametrize(("sample", "confidence"), [
    (MIN_RECOMMENDATION_SAMPLE_SIZE, RecommendationConfidence.LOW),
    (MEDIUM_CONFIDENCE_MIN_SAMPLE, RecommendationConfidence.MEDIUM),
    (HIGH_CONFIDENCE_MIN_SAMPLE, RecommendationConfidence.HIGH),
])
def test_confidence_rises_with_sample_size(sample, confidence):
    assert confidence_for_sample(sample) is confidence


def test_confidence_follows_the_weakest_evidence():
    """A comparison resting on one thin slice is only as confident as that slice."""
    recommendation = a_recommendation(evidence=(
        an_evidence(ordinal=0, sample_size=HIGH_CONFIDENCE_MIN_SAMPLE),
        an_evidence(ordinal=1, sample_size=MIN_RECOMMENDATION_SAMPLE_SIZE),
    ))
    assert recommendation.min_evidence_sample_size == MIN_RECOMMENDATION_SAMPLE_SIZE
    assert recommendation.confidence is RecommendationConfidence.LOW


# --- evidence or nothing, and never from weak data -------------------------------------


def test_a_recommendation_requires_at_least_one_evidence_item():
    with pytest.raises(ValidationError):
        a_recommendation(evidence=())


def test_no_cited_metric_may_be_thinner_than_the_minimum_sample():
    with pytest.raises(ValidationError):
        a_recommendation(evidence=(
            an_evidence(sample_size=MIN_RECOMMENDATION_SAMPLE_SIZE - 1,
                        numerator=1, denominator=MIN_RECOMMENDATION_SAMPLE_SIZE - 1),))


def test_evidence_must_reference_its_own_recommendation():
    stranger = new_career_recommendation_id()
    with pytest.raises(ValidationError):
        a_recommendation(evidence=(an_evidence(recommendation_id=stranger),))


def test_evidence_ordinals_must_not_repeat():
    with pytest.raises(ValidationError):
        a_recommendation(evidence=(an_evidence(ordinal=0), an_evidence(ordinal=0)))


# --- an evidence item cites exactly one metric shape -----------------------------------


def test_rate_evidence_carries_a_numerator_and_denominator_and_no_median():
    with pytest.raises(ValidationError):
        an_evidence(rate_kind=RateKind.RESPONSE, numerator=None, denominator=None)


def test_timing_evidence_carries_a_median_and_no_counts():
    timing = an_evidence(
        rate_kind=None, timing_kind=TimingKind.TIME_TO_OFFER,
        numerator=None, denominator=None, median_days=14.0)
    assert timing.timing_kind is TimingKind.TIME_TO_OFFER
    with pytest.raises(ValidationError):
        an_evidence(rate_kind=None, timing_kind=TimingKind.TIME_TO_OFFER,
                    numerator=6, denominator=20, median_days=14.0)


def test_evidence_must_cite_exactly_one_metric_shape():
    with pytest.raises(ValidationError):
        an_evidence(rate_kind=RateKind.RESPONSE, timing_kind=TimingKind.TIME_TO_OFFER)
    with pytest.raises(ValidationError):
        an_evidence(rate_kind=None, timing_kind=None)


def test_an_overall_metric_carries_no_dimension_key():
    with pytest.raises(ValidationError):
        an_evidence(dimension=None, dimension_key="DATA_AND_ANALYTICS")


# --- the causal-language guard ---------------------------------------------------------


def test_the_causal_guard_flags_cause_and_effect_prose():
    assert asserts_causation("Applying to data roles guarantees an interview") is True
    assert asserts_causation("because it converts better") is True
    assert asserts_causation("Your data applications reach interviews more often") is False
