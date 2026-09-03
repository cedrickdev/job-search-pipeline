# tests/test_v2_matching.py
"""`MatchEvaluation` — the multidimensional score, and what it refuses to be.

V1 stores one 0-100 integer per job. The phase order says avoid a single opaque
score, so the domain refuses to represent one: an evaluation without at least one
dimension cannot be constructed. The second property tested here is the boundary
with eligibility — no axis in this module answers "is this allowed?".
"""
import pytest
from pydantic import ValidationError

from backend.app.domain.common import ReasonImpact
from backend.app.domain.matching import DimensionScore, MatchDimension
from tests.v2_builders import EVALUATION, NOW, OPPORTUNITY, PROFILE, USER, a_reason, an_evaluation


def test_the_six_compatibility_axes_are_present_and_alone():
    assert {member.value for member in MatchDimension} == {
        "SKILLS_FIT", "EXPERIENCE_FIT", "EDUCATION_FIT", "LANGUAGE_FIT",
        "LOCATION_FIT", "SCHEDULE_FIT",
    }


def test_no_gate_masquerades_as_a_score():
    """A permit that forbids the work is not a low number on an axis.

    Averaging eligibility into `overall` is exactly how "great fit, cannot
    legally do it" becomes "good enough", so these names must never appear here.
    """
    values = {member.value for member in MatchDimension}
    for gate in ("ELIGIBILITY", "WORK_AUTHORIZATION", "PERMIT_HOURS_CAP",
                 "MINIMUM_AGE", "LEGAL"):
        assert gate not in values


def test_an_evaluation_needs_at_least_one_dimension():
    with pytest.raises(ValidationError) as failure:
        an_evaluation(dimensions=())
    assert "dimensions" in str(failure.value)


def test_a_dimension_is_scored_once():
    with pytest.raises(ValidationError) as failure:
        an_evaluation(dimensions=(
            DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.9),
            DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.4),
        ))
    assert "must not repeat a MatchDimension" in str(failure.value)


@pytest.mark.parametrize("score", [-0.01, 1.01, 100, -1.0])
def test_a_dimension_score_stays_on_the_unit_interval(score):
    with pytest.raises(ValidationError):
        DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=score)


@pytest.mark.parametrize("overall", [-0.01, 1.01, 75])
def test_an_overall_score_stays_on_the_unit_interval(overall):
    with pytest.raises(ValidationError):
        an_evaluation(overall=overall)


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_evidence_confidence_stays_on_the_unit_interval(confidence):
    with pytest.raises(ValidationError):
        an_evaluation(evidence_confidence=confidence)


def test_a_dimension_carries_full_weight_by_default():
    scored = DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.9)
    assert scored.weight == 1.0
    assert scored.reasons == ()


def test_a_zero_weight_means_computed_and_deliberately_ignored():
    """Different from omitting the dimension, which means "not computed"."""
    evaluation = an_evaluation(dimensions=(
        DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.9),
        DimensionScore(dimension=MatchDimension.EDUCATION_FIT, score=0.1,
                       weight=0.0),
    ))
    assert evaluation.score_for(MatchDimension.EDUCATION_FIT).weight == 0.0
    assert evaluation.score_for(MatchDimension.EDUCATION_FIT).score == 0.1


def test_an_unscored_dimension_reads_as_none_not_as_zero():
    evaluation = an_evaluation()
    assert evaluation.score_for(MatchDimension.SKILLS_FIT).score == 0.92
    assert evaluation.score_for(MatchDimension.LOCATION_FIT) is None


def test_each_axis_carries_its_own_reasons():
    """§9: a candidate must be told why, per axis, not once for the total."""
    evaluation = an_evaluation(dimensions=(
        DimensionScore(dimension=MatchDimension.LANGUAGE_FIT, score=0.4,
                       reasons=(a_reason(code="LANGUAGE_BELOW_MINIMUM",
                                         impact=ReasonImpact.NEGATIVE),)),),
        reasons=(a_reason(code="OVERALL_STRONG", impact=ReasonImpact.POSITIVE),))
    assert evaluation.dimensions[0].reasons[0].code == "LANGUAGE_BELOW_MINIMUM"
    assert evaluation.reasons[0].impact is ReasonImpact.POSITIVE


def test_the_weighted_mean_is_offered_as_a_recipe():
    evaluation = an_evaluation(dimensions=(
        DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.8),
        DimensionScore(dimension=MatchDimension.EDUCATION_FIT, score=0.4,
                       weight=0.5),
    ))
    # (0.8*1.0 + 0.4*0.5) / 1.5
    assert evaluation.weighted_dimension_mean() == pytest.approx(0.6666666, abs=1e-6)


def test_an_unweighted_mean_is_the_plain_average():
    evaluation = an_evaluation(dimensions=(
        DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.9),
        DimensionScore(dimension=MatchDimension.LANGUAGE_FIT, score=0.5),
    ))
    assert evaluation.weighted_dimension_mean() == pytest.approx(0.7)


def test_there_is_no_honest_average_of_nothing():
    evaluation = an_evaluation(dimensions=(
        DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.9, weight=0.0),))
    assert evaluation.weighted_dimension_mean() is None


def test_overall_is_stored_and_may_disagree_with_the_recipe():
    """A matcher may apply penalties the domain knows nothing about.

    Recomputing `overall` here would let the domain quietly contradict the engine
    that produced the number, so the stored value wins and both stay visible.
    """
    evaluation = an_evaluation(overall=0.4, dimensions=(
        DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.9),))
    assert evaluation.overall == 0.4
    assert evaluation.weighted_dimension_mean() == pytest.approx(0.9)


def test_an_evaluation_is_user_scoped_and_names_its_pair():
    """The same posting scores differently for two candidates."""
    evaluation = an_evaluation(evaluator_key="deterministic-v1",
                               evidence_confidence=0.75)
    assert (evaluation.id, evaluation.user_id) == (EVALUATION, USER)
    assert evaluation.candidate_profile_id == PROFILE
    assert evaluation.opportunity_id == OPPORTUNITY
    assert evaluation.evaluated_at == NOW
    assert evaluation.evaluator_key == "deterministic-v1"
    assert evaluation.evidence_confidence == 0.75


def test_the_evaluator_is_named_but_not_typed():
    """Provenance without provider coupling: a plain string, like `source_key`.

    It still has to be possible to tell a deterministic score from a
    model-assisted one when auditing, which is what the key is for.
    """
    for evaluator_key in ("deterministic-v1", "claude-code", "codex",
                          "openai-compatible:local"):
        assert an_evaluation(evaluator_key=evaluator_key).evaluator_key == evaluator_key
    assert an_evaluation().evaluator_key is None
    with pytest.raises(ValidationError):
        an_evaluation(evaluator_key="   ")
