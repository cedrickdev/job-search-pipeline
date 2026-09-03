# tests/test_v2_policy.py
"""`ApplicationPolicy` — the user's standing rules, and the brakes on them.

Every default here is the cautious one, and that is the first thing tested: a
policy built with nothing but its identity must not permit an autonomous
submission, because the bug that skips loading a real policy has to fail safe.
"""
import pytest
from pydantic import ValidationError

from backend.app.domain.matching import DimensionScore, MatchDimension
from backend.app.domain.opportunity import OpportunityType
from backend.app.domain.policy import ApplicationPolicy, AutomationMode, DimensionThreshold
from tests.v2_builders import LATER, NOW, POLICY, USER, an_evaluation


def a_policy(**overrides):
    fields = {
        "id": POLICY,
        "user_id": USER,
        "name": "Student jobs, supervised",
        "created_at": NOW,
        "updated_at": LATER,
    }
    fields.update(overrides)
    return ApplicationPolicy(**fields)


def test_a_bare_policy_permits_nothing_on_its_own():
    policy = a_policy()
    assert policy.mode is AutomationMode.MANUAL
    assert policy.require_approval_before_submission is True
    assert policy.allow_incomplete_eligibility is False
    assert policy.allow_spontaneous_applications is False
    assert policy.allowed_opportunity_types == ()
    assert policy.dimension_thresholds == ()
    assert policy.minimum_overall_score is None
    assert policy.max_applications_per_day is None
    assert policy.max_applications_per_week is None
    assert policy.permits_unattended_submission is False
    assert policy.permits_prepared_materials is False


@pytest.mark.parametrize("mode", [AutomationMode.MANUAL, AutomationMode.COPY_ASSISTED,
                                  AutomationMode.SUPERVISED])
def test_only_autopilot_may_release_the_approval_brake(mode):
    with pytest.raises(ValidationError) as failure:
        a_policy(mode=mode, require_approval_before_submission=False)
    assert "only AUTOPILOT may submit without approval" in str(failure.value)


def test_autopilot_may_keep_the_brake_on():
    """Supervised behaviour with autopilot intent stays representable."""
    policy = a_policy(mode=AutomationMode.AUTOPILOT,
                      require_approval_before_submission=True)
    assert policy.permits_unattended_submission is False


def test_both_switches_must_agree_for_an_unattended_submission():
    policy = a_policy(mode=AutomationMode.AUTOPILOT,
                      require_approval_before_submission=False)
    assert policy.permits_unattended_submission is True


def test_an_inactive_policy_permits_nothing():
    policy = a_policy(mode=AutomationMode.AUTOPILOT,
                      require_approval_before_submission=False, is_active=False)
    assert policy.permits_unattended_submission is False
    assert policy.permits_prepared_materials is False


@pytest.mark.parametrize("mode,expected", [
    (AutomationMode.MANUAL, False),
    (AutomationMode.COPY_ASSISTED, True),
    (AutomationMode.SUPERVISED, True),
    (AutomationMode.AUTOPILOT, True),
])
def test_preparing_materials_is_the_mode_the_product_needs_most(mode, expected):
    """Prepare everything, submit nothing — the rung "automation on/off" misses."""
    assert a_policy(mode=mode).permits_prepared_materials is expected


def test_a_dimension_may_have_only_one_floor():
    with pytest.raises(ValidationError) as failure:
        a_policy(dimension_thresholds=(
            DimensionThreshold(dimension=MatchDimension.LANGUAGE_FIT, minimum=0.6),
            DimensionThreshold(dimension=MatchDimension.LANGUAGE_FIT, minimum=0.8),
        ))
    assert "must not repeat a MatchDimension" in str(failure.value)


@pytest.mark.parametrize("minimum", [-0.01, 1.01, 75])
def test_a_threshold_is_a_unit_interval_score(minimum):
    with pytest.raises(ValidationError):
        a_policy(minimum_overall_score=minimum)
    with pytest.raises(ValidationError):
        DimensionThreshold(dimension=MatchDimension.SKILLS_FIT, minimum=minimum)


def test_a_daily_limit_may_not_exceed_the_weekly_one():
    with pytest.raises(ValidationError) as failure:
        a_policy(max_applications_per_day=10, max_applications_per_week=5)
    assert "must not exceed" in str(failure.value)
    assert a_policy(max_applications_per_day=5, max_applications_per_week=5)


def test_zero_is_a_real_setting():
    """How a user pauses without deleting a policy."""
    policy = a_policy(max_applications_per_day=0, max_applications_per_week=0)
    assert policy.remaining_submissions(0, 0) == 0


def test_policy_timestamps_must_not_run_backwards():
    with pytest.raises(ValidationError) as failure:
        a_policy(created_at=LATER, updated_at=NOW)
    assert "updated_at must not precede created_at" in str(failure.value)


def test_no_restriction_allows_every_type_including_the_unclassified():
    policy = a_policy()
    assert policy.allows_opportunity_type(OpportunityType.STUDENT_JOB)
    assert policy.allows_opportunity_type(None)


def test_a_restricted_policy_refuses_what_it_cannot_classify():
    """The opposite of discovery: applying to an unknown is the risky direction."""
    policy = a_policy(allowed_opportunity_types=(OpportunityType.STUDENT_JOB,
                                                 OpportunityType.PART_TIME))
    assert policy.allows_opportunity_type(OpportunityType.PART_TIME)
    assert not policy.allows_opportunity_type(OpportunityType.FREELANCE)
    assert not policy.allows_opportunity_type(None)


@pytest.mark.parametrize("day,week,used_today,used_week,expected", [
    (None, None, 0, 0, None),          # unlimited
    (3, None, 1, 1, 2),
    (None, 10, 0, 4, 6),
    (3, 10, 1, 9, 1),                  # the weekly budget binds
    (3, 10, 3, 4, 0),                  # today is spent
    (3, 10, 5, 20, 0),                 # over budget never goes negative
])
def test_remaining_submissions_is_pure_arithmetic(day, week, used_today, used_week,
                                                  expected):
    """The caller owns the clock; a domain object that read one is untestable."""
    policy = a_policy(max_applications_per_day=day, max_applications_per_week=week)
    assert policy.remaining_submissions(used_today, used_week) == expected


def test_a_policy_with_no_thresholds_finds_nothing_unmet():
    assert a_policy().unmet_thresholds(an_evaluation()) == ()


def test_an_overall_score_below_the_floor_is_reported_as_a_reason():
    """"below your language floor", not "policy rejected"."""
    policy = a_policy(minimum_overall_score=0.8)
    unmet = policy.unmet_thresholds(an_evaluation(overall=0.5))
    assert [reason.code for reason in unmet] == ["POLICY_OVERALL_BELOW_MINIMUM"]
    assert "0.50" in unmet[0].detail and "0.80" in unmet[0].detail
    assert policy.unmet_thresholds(an_evaluation(overall=0.8)) == ()


def test_a_dimension_below_its_floor_is_reported_per_dimension():
    policy = a_policy(dimension_thresholds=(
        DimensionThreshold(dimension=MatchDimension.LANGUAGE_FIT, minimum=0.7),))
    evaluation = an_evaluation(dimensions=(
        DimensionScore(dimension=MatchDimension.LANGUAGE_FIT, score=0.4),))
    unmet = policy.unmet_thresholds(evaluation)
    assert [reason.code for reason in unmet] == ["POLICY_DIMENSION_BELOW_MINIMUM"]
    assert "LANGUAGE_FIT" in unmet[0].detail


def test_a_missing_score_is_not_evidence_of_a_good_one():
    policy = a_policy(dimension_thresholds=(
        DimensionThreshold(dimension=MatchDimension.LOCATION_FIT, minimum=0.5),))
    unmet = policy.unmet_thresholds(an_evaluation())   # skills only
    assert [reason.code for reason in unmet] == ["POLICY_DIMENSION_NOT_SCORED"]
    assert "never scored" in unmet[0].detail


def test_every_unmet_floor_is_reported_at_once():
    policy = a_policy(minimum_overall_score=0.9, dimension_thresholds=(
        DimensionThreshold(dimension=MatchDimension.SKILLS_FIT, minimum=0.8),
        DimensionThreshold(dimension=MatchDimension.LOCATION_FIT, minimum=0.5),
    ))
    evaluation = an_evaluation(overall=0.4, dimensions=(
        DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.3),))
    codes = [reason.code for reason in policy.unmet_thresholds(evaluation)]
    assert codes == ["POLICY_OVERALL_BELOW_MINIMUM",
                     "POLICY_DIMENSION_BELOW_MINIMUM",
                     "POLICY_DIMENSION_NOT_SCORED"]


def test_a_met_floor_produces_no_reason():
    policy = a_policy(minimum_overall_score=0.5, dimension_thresholds=(
        DimensionThreshold(dimension=MatchDimension.SKILLS_FIT, minimum=0.9),))
    assert policy.unmet_thresholds(an_evaluation()) == ()
