# tests/test_v2_interview_engine.py
"""Le moteur adaptatif pur : capacité, relances, couverture, difficulté (§26-32, §64-72).

Le moteur ne fait aucune E/S — il décide, à partir de l'état d'une session, quelle est la
prochaine question et à quelle difficulté. On le teste donc comme la fonction d'agrégation de
readiness : on lui donne un historique et on affirme la décision, sans provider ni base. La
règle centrale de la phase transparaît ici aussi — « on coache, on ne prédit pas » : la
difficulté monte sur une réponse forte et descend sur une faible, mais *tient* quand rien
n'a pu être évalué, car « non évalué » n'est pas « faible ».
"""
from backend.app.domain.interview import (
    MAX_FOLLOW_UP_DEPTH,
    MAX_QUESTIONS_PER_SESSION,
    EvaluationDimension,
    EvaluationStatus,
    InterviewDifficulty,
    InterviewQuestionType,
    adapt_difficulty,
)
from backend.app.interview.engine import (
    STRONG_ANSWER_SCORE,
    WEAK_ANSWER_SCORE,
    InterviewQuestionEngine,
)
from backend.app.interview.llm import FollowUpDecision
from tests.v2_builders import (
    a_dimension_evaluation,
    an_answer_evaluation,
    an_interview_plan,
    an_interview_question,
)

INTRODUCTORY = InterviewDifficulty.INTRODUCTORY
INTERMEDIATE = InterviewDifficulty.INTERMEDIATE
ADVANCED = InterviewDifficulty.ADVANCED


def _evaluation_with_mean(*scores):
    """An evaluation whose evaluated dimensions carry `scores`, so its mean is theirs."""
    dimensions = tuple(
        a_dimension_evaluation(dimension=dim, score=score)
        for dim, score in zip(
            (EvaluationDimension.CLARITY, EvaluationDimension.RELEVANCE,
             EvaluationDimension.COMPLETENESS), scores, strict=False))
    return an_answer_evaluation(dimensions=dimensions)
def test_has_capacity_holds_until_the_session_bound():
    """A session may ask questions until it has `MAX_QUESTIONS_PER_SESSION`, then no more."""
    engine = InterviewQuestionEngine()
    assert engine.has_capacity([]) is True
    below = [an_interview_question()] * (MAX_QUESTIONS_PER_SESSION - 1)
    assert engine.has_capacity(below) is True
    at_bound = [an_interview_question()] * MAX_QUESTIONS_PER_SESSION
    assert engine.has_capacity(at_bound) is False


def test_may_follow_up_is_gated_by_depth_not_merit():
    """A follow-up is allowed while depth and capacity permit — warrant is the provider's call."""
    engine = InterviewQuestionEngine()
    primary = an_interview_question(sequence=0, depth=0)
    assert engine.may_follow_up(last=primary, asked=[primary]) is True
    # A question already at the depth cap admits no deeper follow-up.
    deep = an_interview_question(sequence=2, depth=MAX_FOLLOW_UP_DEPTH, follows_sequence=0)
    assert engine.may_follow_up(last=deep, asked=[deep]) is False


def test_next_follow_up_declined_when_the_provider_says_no():
    """`ask_follow_up=False` yields no follow-up, whatever the depth allows."""
    engine = InterviewQuestionEngine()
    last = an_interview_question(sequence=0, depth=0, topic_label="past teamwork")
    decision = FollowUpDecision(ask_follow_up=False)
    assert engine.next_follow_up(last=last, asked=[last], difficulty=ADVANCED,
                                 decision=decision) is None


def test_next_follow_up_drills_into_the_last_question_one_rung_deeper():
    """A warranted follow-up carries the engine's identity: deeper, pointing back, same topic."""
    engine = InterviewQuestionEngine()
    last = an_interview_question(sequence=0, depth=0, topic_label="past teamwork")
    decision = FollowUpDecision(ask_follow_up=True, prompt="Et ensuite ?",
                                question_type=InterviewQuestionType.SITUATIONAL)
    request = engine.next_follow_up(last=last, asked=[last], difficulty=ADVANCED,
                                    decision=decision)
    assert request is not None
    assert request.depth == 1
    assert request.follows_sequence == 0
    assert request.sequence == 1
    assert request.topic_label == "past teamwork"
    assert request.difficulty == ADVANCED
    assert request.question_type == InterviewQuestionType.SITUATIONAL


def test_next_follow_up_refused_at_the_depth_cap_even_when_warranted():
    """Depth wins over the provider: a follow-up past `MAX_FOLLOW_UP_DEPTH` is not asked."""
    engine = InterviewQuestionEngine()
    deep = an_interview_question(sequence=2, depth=MAX_FOLLOW_UP_DEPTH, follows_sequence=0)
    decision = FollowUpDecision(ask_follow_up=True, prompt="Encore ?",
                                question_type=InterviewQuestionType.BEHAVIORAL)
    assert engine.next_follow_up(last=deep, asked=[deep], difficulty=INTERMEDIATE,
                                 decision=decision) is None
def test_next_primary_marches_the_plan_in_order():
    """Primaries discharge topics in order, each up to its `target_questions`, then stop."""
    engine = InterviewQuestionEngine()
    plan = an_interview_plan()  # "past teamwork" (BEHAVIORAL, x2), then "motivation" (x1)

    first = engine.next_primary(plan=plan, asked=[], difficulty=INTERMEDIATE)
    assert first is not None
    assert first.topic_label == "past teamwork"
    assert first.question_type == InterviewQuestionType.BEHAVIORAL
    assert first.sequence == 0 and first.depth == 0 and first.follows_sequence is None

    asked = [an_interview_question(sequence=0, topic_label="past teamwork",
                                   question_type=InterviewQuestionType.BEHAVIORAL)]
    second = engine.next_primary(plan=plan, asked=asked, difficulty=INTERMEDIATE)
    assert second is not None and second.topic_label == "past teamwork" and second.sequence == 1

    asked.append(an_interview_question(sequence=1, topic_label="past teamwork",
                                       question_type=InterviewQuestionType.BEHAVIORAL))
    third = engine.next_primary(plan=plan, asked=asked, difficulty=INTERMEDIATE)
    assert third is not None
    assert third.topic_label == "motivation"
    assert third.question_type == InterviewQuestionType.MOTIVATION
    assert third.sequence == 2


def test_next_primary_none_and_plan_covered_when_every_topic_met_its_target():
    """When all topics have met their target, the plan is covered and no primary remains."""
    engine = InterviewQuestionEngine()
    plan = an_interview_plan()
    asked = [
        an_interview_question(sequence=0, topic_label="past teamwork",
                              question_type=InterviewQuestionType.BEHAVIORAL),
        an_interview_question(sequence=1, topic_label="past teamwork",
                              question_type=InterviewQuestionType.BEHAVIORAL),
        an_interview_question(sequence=2, topic_label="motivation",
                              question_type=InterviewQuestionType.MOTIVATION),
    ]
    assert engine.is_plan_covered(plan=plan, asked=asked) is True
    assert engine.next_primary(plan=plan, asked=asked, difficulty=INTERMEDIATE) is None


def test_follow_ups_do_not_count_toward_plan_coverage():
    """Only depth-0 questions cover a topic — a follow-up refines it, it does not discharge it."""
    engine = InterviewQuestionEngine()
    plan = an_interview_plan()
    asked = [
        an_interview_question(sequence=0, topic_label="past teamwork",
                              question_type=InterviewQuestionType.BEHAVIORAL),
        an_interview_question(sequence=1, topic_label="past teamwork",
                              question_type=InterviewQuestionType.SITUATIONAL,
                              depth=1, follows_sequence=0),
    ]
    # One primary + one follow-up on the same topic: coverage still needs a second primary.
    assert engine.is_plan_covered(plan=plan, asked=asked) is False
    nxt = engine.next_primary(plan=plan, asked=asked, difficulty=INTERMEDIATE)
    assert nxt is not None and nxt.topic_label == "past teamwork"
def test_adapt_difficulty_climbs_on_a_strong_answer():
    """A mean at or above the strong threshold earns a harder next question."""
    engine = InterviewQuestionEngine()
    strong = _evaluation_with_mean(STRONG_ANSWER_SCORE, STRONG_ANSWER_SCORE)
    assert engine.adapt_difficulty_after(current=INTERMEDIATE, evaluation=strong) == ADVANCED


def test_adapt_difficulty_descends_on_a_weak_answer():
    """A mean at or below the weak threshold earns an easier next question."""
    engine = InterviewQuestionEngine()
    weak = _evaluation_with_mean(WEAK_ANSWER_SCORE, WEAK_ANSWER_SCORE)
    assert engine.adapt_difficulty_after(current=INTERMEDIATE, evaluation=weak) == INTRODUCTORY


def test_adapt_difficulty_holds_between_the_thresholds():
    """A mean in the middle band leaves difficulty where it was."""
    engine = InterviewQuestionEngine()
    middling = _evaluation_with_mean(0.6, 0.6)
    assert engine.adapt_difficulty_after(current=INTERMEDIATE, evaluation=middling) \
        == INTERMEDIATE


def test_adapt_difficulty_holds_when_nothing_was_evaluated():
    """A wholly `NOT_EVALUATED` answer holds difficulty — "not assessed" is not "weak"."""
    engine = InterviewQuestionEngine()
    unassessed = an_answer_evaluation(dimensions=(
        a_dimension_evaluation(dimension=EvaluationDimension.CLARITY,
                               status=EvaluationStatus.NOT_EVALUATED, score=None),))
    assert engine.adapt_difficulty_after(current=INTERMEDIATE, evaluation=unassessed) \
        == INTERMEDIATE


def test_adapt_difficulty_clamps_at_the_ends_of_the_ladder():
    """Climbing past `ADVANCED` or descending past `INTRODUCTORY` stays put."""
    engine = InterviewQuestionEngine()
    strong = _evaluation_with_mean(0.95, 0.95)
    assert engine.adapt_difficulty_after(current=ADVANCED, evaluation=strong) == ADVANCED
    weak = _evaluation_with_mean(0.1, 0.1)
    assert engine.adapt_difficulty_after(current=INTRODUCTORY, evaluation=weak) == INTRODUCTORY


def test_adapt_difficulty_pure_function_steps_and_clamps():
    """The ladder helper the engine leans on: step within bounds, clamp at the ends."""
    assert adapt_difficulty(INTERMEDIATE, steps=1) == ADVANCED
    assert adapt_difficulty(INTERMEDIATE, steps=-1) == INTRODUCTORY
    assert adapt_difficulty(ADVANCED, steps=1) == ADVANCED
    assert adapt_difficulty(INTRODUCTORY, steps=-1) == INTRODUCTORY
    assert adapt_difficulty(INTRODUCTORY, steps=2) == ADVANCED



