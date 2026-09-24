# tests/v2_interview.py
"""Shared test doubles for the Phase 14 adaptive interview simulator.

Two things every interview flow test needs and no single test should re-build: a
`FakeInterviewLLM` that duck-types `backend.app.interview.llm.InterviewLLM` and returns
domain objects directly — deterministic, per-task configurable, with typed error injection —
and `build_interview_service`, which composes a real `InterviewService` over the in-memory
repositories in `tests.v2_fakes`, a `DeterministicTranscriber`, and that fake LLM, seeding
the one profile and posting a session is grounded in.

Why a purpose-built fake rather than the `FakeProvider` the chat tests use: the interview
adapter drives five *distinct* tasks (plan, question, evaluation, follow-up, summary), each
expecting a different JSON shape, and one canned completion cannot satisfy all five. So the
flow tests talk to a fake at the adapter's own seam — it returns the very `InterviewPlan`,
`ProposedQuestion`, `InterviewAnswerEvaluation`, `FollowUpDecision` and `ProposedSummary` the
real adapter would, having re-validated them — and the real `InterviewLLM` over a smart
`FakeProvider` is exercised on its own, in `test_v2_interview_llm.py`, to prove the
re-validation and the readiness-rejection path this fake takes for granted.

The fake sets the platform-owned evaluation fields (`id`, `answer_id`, `session_id`,
`user_id`, `evaluator_key`, `evaluated_at`) exactly as the adapter does, so the service sees
the same object shape whichever LLM it holds; the readiness a provider may never author has
no field to set, here or there.
"""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.identifiers import interview_answer_evaluation_id
from backend.app.domain.interview import (
    DimensionEvaluation,
    EvaluationDimension,
    EvaluationStatus,
    InterviewAnswer,
    InterviewAnswerEvaluation,
    InterviewDifficulty,
    InterviewMode,
    InterviewPlan,
    InterviewQuestion,
    InterviewQuestionType,
    InterviewTopic,
)
from backend.app.domain.opportunity import Opportunity
from backend.app.interview.context import InterviewContext, InterviewContextBuilder
from backend.app.interview.engine import InterviewQuestionEngine
from backend.app.interview.guard import InterviewCoachingGuard
from backend.app.interview.llm import (
    LLM_INTERVIEW_KEY,
    FollowUpDecision,
    ProposedQuestion,
    ProposedSummary,
)
from backend.app.interview.service import InterviewService
from backend.app.interview.transcriber import DeterministicTranscriber, SpeechTranscriber
from backend.app.llm.failures import LLMError
from tests.v2_builders import a_candidate_profile, an_opportunity
from tests.v2_fakes import (
    FakeCandidateProfileRepository,
    FakeInterviewAnswerEvaluationRepository,
    FakeInterviewAnswerRepository,
    FakeInterviewQuestionRepository,
    FakeInterviewSessionRepository,
    FakeInterviewSessionSummaryRepository,
    FakeOpportunityRepository,
)

# The two axes the fake grades by default. Both are weighed by every mode's readiness profile,
# so a session graded on them alone still yields a defined `overall` — the common case a flow
# test wants; a test probing the `NOT_EVALUATED` ≠ zero rule overrides the axes or the score.
_DEFAULT_EVAL_AXES: tuple[EvaluationDimension, ...] = (
    EvaluationDimension.CLARITY,
    EvaluationDimension.RELEVANCE,
)


def _default_plan(mode: InterviewMode) -> InterviewPlan:
    """A two-topic plan for `mode`, so `plan.mode` agrees with the session it opens."""
    return InterviewPlan(
        mode=mode,
        topics=(
            InterviewTopic(label="past teamwork",
                           question_type=InterviewQuestionType.BEHAVIORAL,
                           target_questions=2),
            InterviewTopic(label="motivation",
                           question_type=InterviewQuestionType.MOTIVATION),
        ))
class FakeInterviewLLM:
    """A deterministic stand-in for `InterviewLLM`, configured per task (Phase 14 tests).

    Duck-types the five public methods the service calls. Every returned value is a real
    domain object or proposal, so the service exercises its true generate→guard→persist path;
    only the provider round trip is replaced. Any task can be made to raise an injected
    `LLMError` to drive the service's degradation (`_try_evaluate`, follow-up fall-through,
    summary fallback) and hard-failure (`propose_plan`, `generate_question`) branches. The
    evaluation's score and coaching prose are configurable so a test can steer difficulty
    adaptation or feed the evidence guard something to reject.
    """

    def __init__(
            self, *,
            plan: InterviewPlan | None = None,
            question_prompt: str = "Décrivez une situation professionnelle marquante.",
            evaluation_score: float | None = 0.8,
            evaluation_axes: Sequence[EvaluationDimension] = _DEFAULT_EVAL_AXES,
            evaluation_confidence: float | None = 0.6,
            evaluation_strengths: Sequence[str] = ("réponse claire et bien structurée",),
            evaluation_improvements: Sequence[str] = ("donner un exemple plus concret",),
            evaluation_suggested_answer: str | None = None,
            follow_up: FollowUpDecision | None = None,
            follow_ups: Sequence[FollowUpDecision] = (),
            summary: ProposedSummary | None = None,
            plan_error: LLMError | None = None,
            question_error: LLMError | None = None,
            evaluation_error: LLMError | None = None,
            follow_up_error: LLMError | None = None,
            summary_error: LLMError | None = None) -> None:
        self._plan = plan
        self._question_prompt = question_prompt
        self.evaluation_score = evaluation_score
        self.evaluation_axes = tuple(evaluation_axes)
        self.evaluation_confidence = evaluation_confidence
        self.evaluation_strengths = tuple(evaluation_strengths)
        self.evaluation_improvements = tuple(evaluation_improvements)
        self.evaluation_suggested_answer = evaluation_suggested_answer
        self._follow_up = (follow_up if follow_up is not None
                           else FollowUpDecision(ask_follow_up=False))
        self._follow_ups = list(follow_ups)
        self._summary = summary
        self.plan_error = plan_error
        self.question_error = question_error
        self.evaluation_error = evaluation_error
        self.follow_up_error = follow_up_error
        self.summary_error = summary_error
        self.calls: dict[str, int] = {
            "propose_plan": 0, "generate_question": 0, "evaluate_answer": 0,
            "decide_follow_up": 0, "generate_summary": 0}
        self._question_serial = 0
    @property
    def key(self) -> str:
        return LLM_INTERVIEW_KEY

    async def propose_plan(self, *, context: InterviewContext,
                           mode: InterviewMode) -> InterviewPlan:
        self.calls["propose_plan"] += 1
        if self.plan_error is not None:
            raise self.plan_error
        return self._plan if self._plan is not None else _default_plan(mode)

    async def generate_question(self, *, context: InterviewContext, mode: InterviewMode,
                                question_type: InterviewQuestionType,
                                difficulty: InterviewDifficulty,
                                topic_label: str | None = None,
                                already_asked: Sequence[str] = ()) -> ProposedQuestion:
        self.calls["generate_question"] += 1
        if self.question_error is not None:
            raise self.question_error
        self._question_serial += 1
        focus = f" [{topic_label}]" if topic_label else ""
        return ProposedQuestion(
            prompt=f"{self._question_prompt}{focus} (#{self._question_serial})",
            question_type=question_type, difficulty=difficulty, topic_label=topic_label)

    async def evaluate_answer(self, *, context: InterviewContext,
                              question: InterviewQuestion, answer: InterviewAnswer,
                              evaluated_at: datetime) -> InterviewAnswerEvaluation:
        self.calls["evaluate_answer"] += 1
        if self.evaluation_error is not None:
            raise self.evaluation_error
        return self._build_evaluation(answer, evaluated_at)

    async def decide_follow_up(self, *, context: InterviewContext,
                               question: InterviewQuestion,
                               answer: InterviewAnswer) -> FollowUpDecision:
        self.calls["decide_follow_up"] += 1
        if self.follow_up_error is not None:
            raise self.follow_up_error
        if self._follow_ups:
            return self._follow_ups.pop(0)
        return self._follow_up

    async def generate_summary(self, *, context: InterviewContext, mode: InterviewMode,
                               transcript: str) -> ProposedSummary:
        self.calls["generate_summary"] += 1
        if self.summary_error is not None:
            raise self.summary_error
        if self._summary is not None:
            return self._summary
        return ProposedSummary(
            headline="Séance de pratique menée avec clarté.",
            strengths=("garde une structure nette",),
            focus_areas=("préciser davantage les exemples",))
    def _build_evaluation(self, answer: InterviewAnswer,
                          evaluated_at: datetime) -> InterviewAnswerEvaluation:
        """Build the evaluation the platform-owned fields and all, as the adapter would.

        `evaluation_score is None` grades every configured axis `NOT_EVALUATED` (no score) —
        the honest "we could not assess this", never a zero — which leaves difficulty untouched
        and the answer un-counted for readiness. A score grades them `EVALUATED` at that value,
        so a test can drive a strong or weak answer and watch the engine adapt.
        """
        if self.evaluation_score is None:
            dimensions = tuple(
                DimensionEvaluation(dimension=axis, status=EvaluationStatus.NOT_EVALUATED)
                for axis in self.evaluation_axes)
        else:
            dimensions = tuple(
                DimensionEvaluation(dimension=axis, status=EvaluationStatus.EVALUATED,
                                    score=self.evaluation_score)
                for axis in self.evaluation_axes)
        return InterviewAnswerEvaluation(
            id=interview_answer_evaluation_id(answer.id),
            answer_id=answer.id, session_id=answer.session_id, user_id=answer.user_id,
            dimensions=dimensions, confidence=self.evaluation_confidence,
            strengths=self.evaluation_strengths, improvements=self.evaluation_improvements,
            suggested_answer=self.evaluation_suggested_answer,
            evaluator_key=self.key, evaluated_at=evaluated_at)


@dataclass
class InterviewHarness:
    """Everything an interview flow test drives and inspects.

    The composed `service` under test, the five interview repositories plus the profile and
    opportunity stores (so a test can assert on persisted rows or delete the grounding
    mid-session), the `FakeInterviewLLM` (so a test can reconfigure a task or read its call
    counts), the transcriber, and the seeded `profile` and `opportunity` a session is grounded
    in.
    """

    service: InterviewService
    sessions: FakeInterviewSessionRepository
    questions: FakeInterviewQuestionRepository
    answers: FakeInterviewAnswerRepository
    evaluations: FakeInterviewAnswerEvaluationRepository
    summaries: FakeInterviewSessionSummaryRepository
    profiles: FakeCandidateProfileRepository
    opportunities: FakeOpportunityRepository
    llm: FakeInterviewLLM
    transcriber: SpeechTranscriber
    profile: CandidateProfile
    opportunity: Opportunity
def build_interview_service(
        *, profile: CandidateProfile | None = None,
        opportunity: Opportunity | None = None,
        llm: FakeInterviewLLM | None = None,
        transcriber: SpeechTranscriber | None = None,
        engine: InterviewQuestionEngine | None = None,
        guard: InterviewCoachingGuard | None = None,
        seed_grounding: bool = True) -> InterviewHarness:
    """A ready `InterviewService` over in-memory repositories, its grounding seeded.

    Seeds the profile and posting a session is grounded in, so `create_session` resolves them.
    A test for the missing-grounding path passes `seed_grounding=False`, leaving the stores
    empty so `create_session` raises `InterviewGroundingNotFound`. The LLM defaults to a
    `FakeInterviewLLM` whose answers are strong and un-fabricated; the transcriber to a
    `DeterministicTranscriber`. The engine and guard default to the real, stateless ones — the
    flow tests exercise the true adaptive and truth-gating behaviour, not a stub of it.
    """
    profile = profile if profile is not None else a_candidate_profile()
    opportunity = opportunity if opportunity is not None else an_opportunity()

    profiles = FakeCandidateProfileRepository()
    opportunities = FakeOpportunityRepository()
    if seed_grounding:
        profiles.profiles[profile.id] = profile
        opportunities.opportunities[opportunity.id] = opportunity

    sessions = FakeInterviewSessionRepository()
    questions = FakeInterviewQuestionRepository()
    answers = FakeInterviewAnswerRepository()
    evaluations = FakeInterviewAnswerEvaluationRepository()
    summaries = FakeInterviewSessionSummaryRepository()

    llm = llm if llm is not None else FakeInterviewLLM()
    transcriber = transcriber if transcriber is not None else DeterministicTranscriber()

    service = InterviewService(
        sessions=sessions, questions=questions, answers=answers,
        evaluations=evaluations, summaries=summaries,
        context=InterviewContextBuilder(profiles=profiles, opportunities=opportunities),
        llm=llm, transcriber=transcriber, engine=engine, guard=guard)

    return InterviewHarness(
        service=service, sessions=sessions, questions=questions, answers=answers,
        evaluations=evaluations, summaries=summaries, profiles=profiles,
        opportunities=opportunities, llm=llm, transcriber=transcriber,
        profile=profile, opportunity=opportunity)





