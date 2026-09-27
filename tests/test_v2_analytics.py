# tests/test_v2_analytics.py
"""The funnel arithmetic, pinned: count applications not events, and never count silence as failure.

`CareerAnalytics` is the deterministic layer the recommendation engine trusts, so these tests
hold the rules that make a number quotable: the furthest-stage rule collapses repeated
milestones so the funnel counts applications, the negative terminals never advance progress,
maturity is one centralized clock-free predicate, a rate carries its own denominator and reads
`None` rather than 0% on an empty base, a timing's quartiles are present exactly when there is a
sample and stay ordered, and every self-describing metric refuses to contradict itself
(numerator within denominator, a full ordered ladder, no repeated dimension).
"""
from datetime import timedelta

import pytest
from pydantic import ValidationError

from backend.app.domain.analytics import (
    CAREER_ANALYTICS_VERSION,
    DEFAULT_OBSERVATION_HORIZON_DAYS,
    CareerAnalytics,
    CareerFunnel,
    ConversionRate,
    DimensionBreakdown,
    DimensionCell,
    DimensionKind,
    FunnelStage,
    FunnelStageCount,
    MaturityCensoring,
    ObservationWindow,
    RateKind,
    TimingKind,
    TimingStat,
    furthest_funnel_stage,
    is_application_mature,
    outcome_stage,
    rate_stages,
    stage_rank,
)
from backend.app.domain.outcome import OutcomeKind
from tests.v2_builders import NOW, USER

WINDOW = ObservationWindow(earliest_applied_at=NOW - timedelta(days=60),
                           latest_applied_at=NOW)


def a_funnel(**counts):
    """A full-ladder funnel; pass any subset of stage counts, defaulting to a plausible slope."""
    defaults = {"SUBMITTED": 40, "ACKNOWLEDGED": 20, "SCREEN": 12, "ASSESSMENT": 9,
                    "INTERVIEW": 6, "OFFER": 2, "ACCEPTED": 1}
    defaults.update(counts)
    stages = tuple(FunnelStageCount(stage=stage, applications=defaults[stage.value])
                   for stage in FunnelStage)
    return CareerFunnel(
        stages=stages,
        window=WINDOW,
        censoring=MaturityCensoring(as_of=NOW, mature_count=40, censored_count=0))


# --- the pure stage rules --------------------------------------------------------------


def test_the_stage_ladder_is_ordered_shallow_to_deep():
    assert stage_rank(FunnelStage.SUBMITTED) == 0
    assert stage_rank(FunnelStage.ACCEPTED) == max(stage_rank(s) for s in FunnelStage)


def test_negative_terminals_do_not_map_to_a_progress_stage():
    """A rejection or withdrawal concludes without placing an application on a rung (§13)."""
    assert outcome_stage(OutcomeKind.REJECTED) is None
    assert outcome_stage(OutcomeKind.WITHDRAWN) is None
    assert outcome_stage(OutcomeKind.OFFER_DECLINED) is FunnelStage.OFFER


def test_furthest_stage_counts_applications_not_events():
    """Three interview rounds advance an application to INTERVIEW exactly once."""
    kinds = [OutcomeKind.INTERVIEW, OutcomeKind.INTERVIEW, OutcomeKind.INTERVIEW]
    assert furthest_funnel_stage(kinds) is FunnelStage.INTERVIEW


def test_furthest_stage_ignores_a_rejection_after_an_interview():
    """A rejection does not un-reach the interview that preceded it."""
    kinds = [OutcomeKind.SCREEN, OutcomeKind.INTERVIEW, OutcomeKind.REJECTED]
    assert furthest_funnel_stage(kinds) is FunnelStage.INTERVIEW


def test_an_application_with_no_outcomes_sits_at_submitted():
    assert furthest_funnel_stage([]) is FunnelStage.SUBMITTED


def test_rate_stages_anchor_later_conversions_on_a_later_base():
    """OFFER_CONVERSION is offers per interview, not per application (§23)."""
    assert rate_stages(RateKind.OFFER_CONVERSION) == (FunnelStage.INTERVIEW, FunnelStage.OFFER)
    assert rate_stages(RateKind.RESPONSE) == (FunnelStage.SUBMITTED, FunnelStage.ACKNOWLEDGED)


# --- maturity / censoring --------------------------------------------------------------


def test_a_concluded_application_is_mature_immediately():
    assert is_application_mature(reference=NOW, as_of=NOW, has_terminal_outcome=True) is True


def test_a_fresh_silent_application_is_censored_until_the_horizon():
    horizon = DEFAULT_OBSERVATION_HORIZON_DAYS
    just_before = NOW + timedelta(days=horizon - 1)
    at_horizon = NOW + timedelta(days=horizon)
    assert is_application_mature(reference=NOW, as_of=just_before,
                                 has_terminal_outcome=False) is False
    assert is_application_mature(reference=NOW, as_of=at_horizon,
                                 has_terminal_outcome=False) is True


# --- self-describing metrics -----------------------------------------------------------


def test_an_observation_window_sets_both_bounds_or_neither():
    assert ObservationWindow().is_empty is True
    with pytest.raises(ValidationError):
        ObservationWindow(earliest_applied_at=NOW)


def test_a_funnel_must_be_the_full_ordered_ladder():
    with pytest.raises(ValidationError):
        CareerFunnel(
            stages=(FunnelStageCount(stage=FunnelStage.SUBMITTED, applications=10),),
            window=WINDOW,
            censoring=MaturityCensoring(as_of=NOW, mature_count=10, censored_count=0))


def test_funnel_counts_may_not_rise_down_the_ladder():
    """Reaching OFFER implies reaching INTERVIEW, so count(OFFER) <= count(INTERVIEW)."""
    with pytest.raises(ValidationError):
        a_funnel(INTERVIEW=2, OFFER=5)


def test_count_at_reads_cumulative_reach():
    funnel = a_funnel()
    assert funnel.submitted == 40
    assert funnel.count_at(FunnelStage.INTERVIEW) == 6


def test_a_rate_carries_its_denominator_and_reads_none_on_an_empty_base():
    empty = ConversionRate(kind=RateKind.OFFER_CONVERSION, numerator=0, denominator=0,
                           window=WINDOW)
    assert empty.rate is None
    assert empty.rate_percent() is None
    assert empty.sample_size == 0
    real = ConversionRate(kind=RateKind.RESPONSE, numerator=1, denominator=2, window=WINDOW)
    assert real.rate == 0.5
    assert real.rate_percent() == 50
    assert real.sample_size == 2


def test_a_rate_numerator_cannot_exceed_its_denominator():
    with pytest.raises(ValidationError):
        ConversionRate(kind=RateKind.RESPONSE, numerator=3, denominator=2, window=WINDOW)


def test_a_timing_has_quartiles_exactly_when_it_has_a_sample():
    empty = TimingStat(kind=TimingKind.TIME_TO_OFFER, sample_size=0, window=WINDOW)
    assert empty.median_days is None
    with pytest.raises(ValidationError):
        TimingStat(kind=TimingKind.TIME_TO_OFFER, sample_size=0, median_days=5.0,
                   window=WINDOW)
    with pytest.raises(ValidationError):
        TimingStat(kind=TimingKind.TIME_TO_OFFER, sample_size=3, median_days=5.0,
                   window=WINDOW)


def test_timing_quartiles_must_be_ordered():
    with pytest.raises(ValidationError):
        TimingStat(kind=TimingKind.TIME_TO_INTERVIEW, sample_size=5,
                   median_days=3.0, p25_days=9.0, p75_days=12.0, window=WINDOW)


def test_a_dimension_cell_keeps_an_unclassified_key_as_none_and_dedups_rates():
    cell = DimensionCell(dimension=DimensionKind.ROLE_FAMILY, key=None, applications=4)
    assert cell.key is None
    assert cell.rate_for(RateKind.RESPONSE) is None
    with pytest.raises(ValidationError):
        DimensionCell(
            dimension=DimensionKind.ROLE_FAMILY, key="DATA_AND_ANALYTICS", applications=4,
            rates=(ConversionRate(kind=RateKind.RESPONSE, numerator=1, denominator=4,
                                  window=WINDOW),
                   ConversionRate(kind=RateKind.RESPONSE, numerator=2, denominator=4,
                                  window=WINDOW)))


def test_a_breakdown_rejects_a_cell_of_the_wrong_dimension_or_a_repeated_key():
    with pytest.raises(ValidationError):
        DimensionBreakdown(
            dimension=DimensionKind.SOURCE,
            cells=(DimensionCell(dimension=DimensionKind.ROLE_FAMILY, key="x", applications=1),),
            window=WINDOW)


def test_career_analytics_defaults_its_version_and_dedups_its_metrics():
    report = CareerAnalytics(user_id=USER, window=WINDOW, funnel=a_funnel(), computed_at=NOW)
    assert report.analytics_version == CAREER_ANALYTICS_VERSION
    assert report.rate_for(RateKind.RESPONSE) is None
    with pytest.raises(ValidationError):
        CareerAnalytics(
            user_id=USER, window=WINDOW, funnel=a_funnel(), computed_at=NOW,
            rates=(ConversionRate(kind=RateKind.RESPONSE, numerator=1, denominator=2,
                                  window=WINDOW),
                   ConversionRate(kind=RateKind.RESPONSE, numerator=2, denominator=2,
                                  window=WINDOW)))
