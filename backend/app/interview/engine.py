"""The adaptive question engine — the pure brain that decides the next turn (§26-32, §64-72).

The engine holds the whole "what happens next" logic of a session and *nothing else*: no
repository, no provider, no clock. Given the session's plan, the questions asked so far and
the last evaluation, it answers three deterministic questions — may the session ask another
question at all, what should the next one be, and how hard should it be — and returns those
answers as small value objects the service then realizes by calling a provider and writing a
row. Keeping it pure is what makes the adaptive behaviour testable without a model: a test
feeds it a history and asserts the request, exactly as the readiness aggregator is tested.

Three rules the engine enforces so no surface has to:

- **Bounds.** A session never exceeds `MAX_QUESTIONS_PER_SESSION`, and a follow-up chain never
  exceeds `MAX_FOLLOW_UP_DEPTH` — a provider stuck drilling one topic is caught here, not left
  to ask forever (§64-72).
- **Coverage drives primaries.** Primary questions march through the plan's topics in order,
  one topic discharged per its `target_questions`; when every topic is covered the engine says
  so, and the session can close having actually exercised its plan (§30-31).
- **Difficulty adapts to the evidence.** After an evaluated answer the engine steps difficulty
  up on a strong answer and down on a weak one, along the ordered ladder and clamped at its
  ends (§32) — and holds steady when nothing could be evaluated, because "we could not assess"
  is not "that was weak" (the same `NOT_EVALUATED` ≠ zero discipline the readiness math keeps).

The engine proposes; the service disposes. It never decides *whether a follow-up is
warranted* — that judgement is the provider's `FollowUpDecision` — it decides only whether one
is *allowed* (depth and bounds) and, when it is, the shape of the follow-up question. So a
provider can want to drill forever and the session still converges on its plan.
"""
from collections.abc import Sequence
from dataclasses import dataclass

from backend.app.domain.interview import (
    MAX_FOLLOW_UP_DEPTH,
    MAX_QUESTIONS_PER_SESSION,
    EvaluationStatus,
    InterviewAnswerEvaluation,
    InterviewDifficulty,
    InterviewPlan,
    InterviewQuestion,
    InterviewQuestionType,
    adapt_difficulty,
)
from backend.app.interview.llm import FollowUpDecision

# The mean-evaluated-score thresholds that move difficulty. A strong answer (at or above
# STRONG) earns a harder next question; a weak one (at or below WEAK) earns an easier one;
# anything between holds the rung. They are coaching gates, not hiring bars — the point is to
# keep practice in the candidate's stretch zone, not to score them (§32, §123).
STRONG_ANSWER_SCORE: float = 0.75
WEAK_ANSWER_SCORE: float = 0.45


@dataclass(frozen=True)
class QuestionRequest:
    """The engine's decision about the next question, before a provider writes its text.

    A value the service realizes: it hands the type, difficulty and topic to the LLM adapter,
    which returns a `ProposedQuestion`, and then stamps the engine-owned identity fields —
    `sequence`, `depth`, `follows_sequence` — that this request already fixed. The engine owns
    identity and position; the model owns only prose, which is why those fields live here and
    not on what the model returns.
    """

    question_type: InterviewQuestionType
    difficulty: InterviewDifficulty
    topic_label: str | None
    sequence: int
    depth: int
    follows_sequence: int | None


class InterviewQuestionEngine:
    """Decides the next question and the next difficulty — purely, from session state.

    Stateless: every method is a function of the arguments it is given, so one engine serves
    every session and a test constructs one freely. The service owns the I/O (loading history,
    calling the provider, persisting); the engine owns the decisions those actions carry out.
    """

    def has_capacity(self, asked: Sequence[InterviewQuestion]) -> bool:
        """Whether the session may ask one more question at all (§64-72)."""
        return len(asked) < MAX_QUESTIONS_PER_SESSION

    def may_follow_up(self, *, last: InterviewQuestion,
                      asked: Sequence[InterviewQuestion]) -> bool:
        """Whether a follow-up to `last` is *allowed* — depth and bounds, not merit.

        Whether one is *warranted* is the provider's `FollowUpDecision`; this only gates it, so
        the service does not even ask a provider to decide a follow-up the depth cap forbids.
        """
        return self.has_capacity(asked) and last.depth < MAX_FOLLOW_UP_DEPTH

    def next_follow_up(self, *, last: InterviewQuestion,
                       asked: Sequence[InterviewQuestion],
                       difficulty: InterviewDifficulty,
                       decision: FollowUpDecision) -> QuestionRequest | None:
        """The shape of a follow-up to `last`, or `None` when none should be asked.

        `None` on any of: the provider chose not to drill (`ask_follow_up=False`), the depth
        cap is reached, or the session is out of capacity. When a request is returned it drills
        into `last` (its sequence, so the chain is reconstructable) one rung deeper, staying in
        `last`'s topic — a follow-up refines a topic, it does not open a new one.
        """
        if not decision.ask_follow_up:
            return None
        if not self.may_follow_up(last=last, asked=asked):
            return None
        if decision.question_type is None:  # guarded by FollowUpDecision, re-checked for mypy
            return None
        return QuestionRequest(
            question_type=decision.question_type,
            difficulty=difficulty,
            topic_label=last.topic_label,
            sequence=len(asked),
            depth=last.depth + 1,
            follows_sequence=last.sequence,
        )

    def next_primary(self, *, plan: InterviewPlan,
                     asked: Sequence[InterviewQuestion],
                     difficulty: InterviewDifficulty) -> QuestionRequest | None:
        """The next uncovered plan topic as a primary question, or `None` when the plan is done.

        Marches the plan's topics in order: the first whose primary questions asked so far fall
        short of its `target_questions` is the one to probe next. `None` when every topic has
        met its target or the session is out of capacity — the signal the session may close
        having covered its plan (§30-31).
        """
        if not self.has_capacity(asked):
            return None
        counts = self._primary_counts_by_topic(asked)
        for topic in plan.topics:
            if counts.get(topic.label.casefold(), 0) < topic.target_questions:
                return QuestionRequest(
                    question_type=topic.question_type,
                    difficulty=difficulty,
                    topic_label=topic.label,
                    sequence=len(asked),
                    depth=0,
                    follows_sequence=None,
                )
        return None

    def is_plan_covered(self, *, plan: InterviewPlan,
                        asked: Sequence[InterviewQuestion]) -> bool:
        """Whether every plan topic has met its target of primary questions."""
        counts = self._primary_counts_by_topic(asked)
        return all(counts.get(topic.label.casefold(), 0) >= topic.target_questions
                   for topic in plan.topics)

    def adapt_difficulty_after(self, *, current: InterviewDifficulty,
                               evaluation: InterviewAnswerEvaluation) -> InterviewDifficulty:
        """The difficulty for the next question, adapted from one answer's grade (§32).

        Steps up on a strong answer, down on a weak one, holds otherwise — and holds when the
        answer had no evaluated dimension at all, because an unassessable answer is not a weak
        one. Clamped at the ladder's ends by `adapt_difficulty`.
        """
        mean = self._mean_evaluated_score(evaluation)
        if mean is None:
            return current
        if mean >= STRONG_ANSWER_SCORE:
            return adapt_difficulty(current, steps=1)
        if mean <= WEAK_ANSWER_SCORE:
            return adapt_difficulty(current, steps=-1)
        return current

    @staticmethod
    def _primary_counts_by_topic(
            asked: Sequence[InterviewQuestion]) -> dict[str, int]:
        """How many *primary* questions each topic label has drawn (follow-ups excluded).

        Only depth-0 questions count toward coverage: a follow-up refines a topic already
        opened, so counting it would let a provider "cover" a plan by drilling one topic.
        """
        counts: dict[str, int] = {}
        for question in asked:
            if question.depth == 0 and question.topic_label is not None:
                key = question.topic_label.casefold()
                counts[key] = counts.get(key, 0) + 1
        return counts

    @staticmethod
    def _mean_evaluated_score(evaluation: InterviewAnswerEvaluation) -> float | None:
        """The mean of the answer's *evaluated* dimension scores, or `None` if none were.

        A `NOT_EVALUATED` dimension contributes nothing — never a zero — so a warm-up graded on
        one axis adapts difficulty on that axis alone, and an answer nothing could be judged on
        leaves difficulty untouched.
        """
        scores = [
            entry.score
            for entry in evaluation.dimensions
            if entry.status is EvaluationStatus.EVALUATED and entry.score is not None
        ]
        if not scores:
            return None
        return sum(scores) / len(scores)
