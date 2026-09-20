# tests/test_v2_match_engine.py
"""`evaluate_match` — the deterministic compatibility engine.

The domain test (`test_v2_matching.py`) pins what a `MatchEvaluation` may hold;
this pins what the *engine* computes from real profiles and postings. Four rules
from the phase order are the spine of it: a dimension nobody can evaluate is
omitted rather than scored zero, `overall` is the weighted mean over the
dimensions that were evaluated, `evidence_confidence` is the separate coverage
axis, and nothing about eligibility ever appears here.
"""
from datetime import UTC, datetime

from backend.app.domain.candidate import Availability
from backend.app.domain.common import (
    LanguageLevel,
    LanguageProficiency,
    LanguageRequirement,
    Location,
    WorkloadRange,
)
from backend.app.domain.matching import (
    DEFAULT_MATCH_PROFILE,
    MatchClassification,
    MatchDimension,
    to_percent,
)
from backend.app.domain.opportunity import WorkplaceMode
from backend.app.matching import MATCH_ENGINE_KEY, evaluate_match
from tests.v2_builders import LAUSANNE, a_candidate_profile, an_opportunity

NOW = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


def _dimensions(evaluation):
    return {score.dimension: score for score in evaluation.dimensions}


def test_nothing_scorable_is_none_not_a_zero_evaluation():
    """The phase order's sharpest rule: absence is not a score of zero.

    A profile and posting that share no evaluable dimension yields `None`, which
    the caller renders as UNKNOWN — not a `MatchEvaluation` with `overall=0.0`,
    which would read as "assessed, and a terrible fit".
    """
    profile = a_candidate_profile(languages=(), availability=None,
                                  base_location=None)
    opportunity = an_opportunity(language_requirements=(), workload=None,
                                 location=None, workplace_mode=WorkplaceMode.ON_SITE)
    assert evaluate_match(profile, opportunity, pack=None, now=NOW) is None


def test_only_evaluable_dimensions_are_scored():
    """Skills, experience and education have no data yet, so they are omitted.

    Their weight still counts against evidence coverage — the shortfall shows up
    there, not as an invented score.
    """
    evaluation = evaluate_match(a_candidate_profile(), an_opportunity(),
                                pack=None, now=NOW)
    assert evaluation is not None
    scored = set(_dimensions(evaluation))
    assert scored <= {MatchDimension.LANGUAGE_FIT, MatchDimension.LOCATION_FIT,
                      MatchDimension.SCHEDULE_FIT}
    assert MatchDimension.SKILLS_FIT not in scored
    assert MatchDimension.EDUCATION_FIT not in scored


def test_overall_is_the_weighted_mean_of_the_evaluated_dimensions():
    """`overall` normalizes over the weights of the dimensions that were scored.

    A remote posting in the candidate's own language, with overlapping hours,
    scores every evaluable dimension at 1.0 — so the mean is 1.0 regardless of the
    weights, and `to_percent` renders it 100.
    """
    profile = a_candidate_profile(
        languages=(LanguageProficiency(language="fr", level=LanguageLevel.C2),),
        availability=Availability(min_weekly_hours=20.0, max_weekly_hours=40.0))
    opportunity = an_opportunity(
        workplace_mode=WorkplaceMode.REMOTE,
        language_requirements=(
            LanguageRequirement(language="fr", minimum_level=LanguageLevel.B2),),
        workload=WorkloadRange(min_weekly_hours=20.0, max_weekly_hours=30.0))
    evaluation = evaluate_match(profile, opportunity, pack=None, now=NOW)
    assert evaluation is not None
    assert evaluation.overall == 1.0
    assert to_percent(evaluation.overall) == 100
    assert DEFAULT_MATCH_PROFILE.classify(evaluation.overall) \
        is MatchClassification.EXCELLENT


def test_evidence_confidence_is_coverage_not_score():
    """The separate axis: how much of the profile's weight could be assessed.

    Three dimensions out of six carry weight here (language, location, schedule),
    so coverage is well under 1.0 even when every scored dimension is perfect —
    "confident about what we saw" and "we saw a lot" are different facts.
    """
    evaluation = evaluate_match(a_candidate_profile(), an_opportunity(),
                                pack=None, now=NOW)
    assert evaluation is not None
    assert evaluation.evidence_confidence is not None
    # Coverage is the share of the profile's total weight the scored dimensions
    # carry — derived from what was actually evaluated, not assumed, because the
    # default posting states its workload in percent and no pack is passed, so
    # SCHEDULE_FIT is omitted rather than scored.
    covered = sum(DEFAULT_MATCH_PROFILE.weight_for(score.dimension)
                  for score in evaluation.dimensions)
    total = sum(weight.weight for weight in DEFAULT_MATCH_PROFILE.weights)
    assert evaluation.evidence_confidence == covered / total
    assert evaluation.evidence_confidence < 1.0


def test_a_missing_required_language_lowers_the_score_it_does_not_gate():
    """Below-minimum on a required language is a low LANGUAGE_FIT, not a refusal.

    Matching grades the shortfall; the *gate* is eligibility's, and this engine
    has no way to reach it.
    """
    profile = a_candidate_profile(
        languages=(LanguageProficiency(language="de", level=LanguageLevel.A1),),
        availability=None, base_location=None)
    opportunity = an_opportunity(
        workplace_mode=WorkplaceMode.ON_SITE, location=None, workload=None,
        language_requirements=(
            LanguageRequirement(language="de", minimum_level=LanguageLevel.C2),))
    evaluation = evaluate_match(profile, opportunity, pack=None, now=NOW)
    assert evaluation is not None
    language = _dimensions(evaluation)[MatchDimension.LANGUAGE_FIT]
    assert 0.0 <= language.score < 1.0


def test_location_is_graded_by_administrative_proximity():
    """Same city beats same country beats different country — categorical, not km.

    The engine reads the administrative fields only; distance is Phase 7's, and the
    engine must not recompute it.
    """
    profile = a_candidate_profile(
        base_location=Location(country="CH", region="Vaud", city="Lausanne",
                               point=LAUSANNE),
        languages=(), availability=None)
    same_city = an_opportunity(
        workplace_mode=WorkplaceMode.ON_SITE, language_requirements=(), workload=None,
        location=Location(country="CH", region="Vaud", city="Lausanne",
                          point=LAUSANNE))
    other_country = an_opportunity(
        workplace_mode=WorkplaceMode.ON_SITE, language_requirements=(), workload=None,
        location=Location(country="FR", region="Auvergne-Rhône-Alpes", city="Lyon"))
    near = evaluate_match(profile, same_city, pack=None, now=NOW)
    far = evaluate_match(profile, other_country, pack=None, now=NOW)
    assert near is not None and far is not None
    near_score = _dimensions(near)[MatchDimension.LOCATION_FIT].score
    far_score = _dimensions(far)[MatchDimension.LOCATION_FIT].score
    assert near_score == 1.0
    assert far_score < near_score


def test_a_remote_posting_is_a_full_location_fit():
    """Remote does not constrain where the candidate lives, so it cannot penalize."""
    profile = a_candidate_profile(
        base_location=Location(country="FR", city="Lyon"),
        languages=(), availability=None)
    opportunity = an_opportunity(workplace_mode=WorkplaceMode.REMOTE, language_requirements=(),
                                 workload=None)
    evaluation = evaluate_match(profile, opportunity, pack=None, now=NOW)
    assert evaluation is not None
    assert _dimensions(evaluation)[MatchDimension.LOCATION_FIT].score == 1.0


def test_the_evaluation_names_the_pair_and_stamps_its_provenance():
    """Ids come from the profile and posting; the evaluator key carries the version."""
    profile = a_candidate_profile()
    opportunity = an_opportunity()
    evaluation = evaluate_match(profile, opportunity, pack=None, now=NOW)
    assert evaluation is not None
    assert evaluation.user_id == profile.user_id
    assert evaluation.candidate_profile_id == profile.id
    assert evaluation.opportunity_id == opportunity.id
    assert evaluation.evaluated_at == NOW
    assert evaluation.evaluator_key is not None
    assert evaluation.evaluator_key.startswith(MATCH_ENGINE_KEY)
    assert DEFAULT_MATCH_PROFILE.version in evaluation.evaluator_key


def test_re_evaluating_the_same_pair_is_idempotent():
    """A deterministic engine meeting the same pair twice writes the same id."""
    profile = a_candidate_profile()
    opportunity = an_opportunity()
    first = evaluate_match(profile, opportunity, pack=None, now=NOW)
    second = evaluate_match(profile, opportunity, pack=None,
                            now=datetime(2026, 6, 1, tzinfo=UTC))
    assert first is not None and second is not None
    assert first.id == second.id
    assert first.overall == second.overall
