"""The interview simulator's lifecycle service — plan, ask, answer, grade, aggregate (§1-6).

This is the one application service Phase 14 adds, and it is where the package's pure pieces
become a session with a database behind it. The engine holds every *decision* (may we ask
again, what next, how hard), the LLM adapter writes prose and re-validates it, the guard
judges coaching against the candidate's evidence, the transcriber turns audio into text, and
the domain's `aggregate_session_readiness` computes readiness — and this service decides *what
to feed them* and *in what order*, under the phase's one rule: **the simulator coaches, it
does not predict**.

Three orderings keep that rule physical, each mirroring a boundary a sibling service already
drew:

- **Generate, then guard, then persist.** An evaluation's coaching and a summary's prose are
  run through the Phase 10 evidence guard *before* they are stored, exactly as
  `DocumentService` guards a résumé before rendering it. A rejected evaluation is not stored
  at all (the answer stands, ungraded); a rejected summary falls back to safe, deterministic,
  fact-free prose — never a fabricated strength persisted as coaching (§40-44).
- **The engine owns identity; the model owns only prose.** Every question's type, difficulty,
  sequence, depth and follow-up link come from the engine's `QuestionRequest`; the model
  contributes the prompt text and nothing that positions or grades it. Readiness is computed
  here from the stored grades by the domain function — never read from a provider (§33-36).
- **The owner comes from the session, never the body.** Every method takes `user_id` and
  hands it to repositories whose signatures require it, so another account's session reads as
  absent (`SESSION_NOT_FOUND`) rather than "forbidden" — the same non-leaking discipline the
  chat executor uses (§90).

A provider hiccup never strands a session: a failed follow-up decision falls through to the
next planned question, a failed evaluation leaves the answer un-graded rather than lost, and a
failed summary completes the session with deterministic prose. Only a failed *question
generation* — where there is nothing to return — surfaces as a typed `InterviewError`.
"""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from backend.app.domain.base import LanguageCode
from backend.app.domain.identifiers import (
    ApplicationId,
    CandidateProfileId,
    InterviewSessionId,
    OpportunityId,
    UserId,
    interview_answer_id,
    interview_question_id,
    interview_session_summary_id,
    new_interview_session_id,
)
from backend.app.domain.interview import (
    InterviewAnswer,
    InterviewAnswerEvaluation,
    InterviewAnswerFormat,
    InterviewDifficulty,
    InterviewErrorCode,
    InterviewMode,
    InterviewPlan,
    InterviewQuestion,
    InterviewSession,
    InterviewSessionStatus,
    InterviewSessionSummary,
    SessionReadiness,
    SessionStyle,
    aggregate_session_readiness,
    can_transition_session,
)
from backend.app.interview.context import InterviewContext, InterviewContextBuilder
from backend.app.interview.engine import InterviewQuestionEngine, QuestionRequest
from backend.app.interview.guard import InterviewCoachingGuard
from backend.app.interview.llm import InterviewLLM
from backend.app.interview.transcriber import (
    InterviewAudio,
    SpeechTranscriber,
    TranscriptionError,
)
from backend.app.llm.failures import LLMError
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    InterviewAnswerEvaluationRepository,
    InterviewAnswerRepository,
    InterviewQuestionRepository,
    InterviewSessionRepository,
    InterviewSessionSummaryRepository,
)

# The provenance stamp of a deterministic, fact-free closing summary. Used only when a
# provider could not write the coaching prose or its prose failed the evidence guard: the
# session still completes, carrying a safe headline and the platform-computed readiness, and
# the key records honestly that no model authored the words. Distinct from `InterviewLLM.key`
# so an audit can tell a model summary from the safe fallback.
_DETERMINISTIC_SUMMARY_KEY = "deterministic-summary/1"

# The fallback headline: no candidate fact, no number, no skill term, so it needs no guard —
# it is platform prose, not model prose. It points the candidate at the readiness breakdown,
# which is the trustworthy signal even when the coaching prose could not be produced.
_FALLBACK_SUMMARY_HEADLINE = "Practice session complete — see your readiness breakdown below."

# The longest a derived session title may be, so a session list can show it whole. A caption
# the caller may override, never model-authored authority.
_MAX_TITLE_LENGTH = 120


class InterviewError(Exception):
    """A refused interview operation, carrying a stable `InterviewErrorCode` (§90).

    The one exception the service raises for a *domain* refusal — a missing or inactive
    session, an already-answered question, an unavailable provider — so the API maps a code
    to a status without parsing prose. `detail` is a short operator sentence; it never carries
    a provider's message, a candidate's words or a raw payload.
    """

    def __init__(self, code: InterviewErrorCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class InterviewGroundingNotFound(Exception):
    """The candidate profile or the posting to ground a new session was not found.

    Raised by `create_session` before a session exists, when the profile (read owner-first,
    so another account's reads as absent) or the opportunity cannot be resolved. Distinct from
    `InterviewError(SESSION_NOT_FOUND)` because no session is involved yet; the API maps it to
    a 404 that does not say which of the two was missing.
    """

    def __init__(self, candidate_profile_id: CandidateProfileId,
                 opportunity_id: OpportunityId) -> None:
        super().__init__(
            f"no grounding for candidate {candidate_profile_id} and opportunity "
            f"{opportunity_id}")
        self.candidate_profile_id = candidate_profile_id
        self.opportunity_id = opportunity_id


@dataclass(frozen=True)
class InterviewTurn:
    """The next step of a session: the current question, or the signal to complete it.

    `question` is the one question awaiting an answer — freshly generated, or the same one
    returned again when `next_question` is polled before the candidate has answered (the call
    is idempotent). `question is None` means the plan is covered or the session hit its bound:
    there is nothing more to ask, and the caller should complete the session.
    """

    session: InterviewSession
    question: InterviewQuestion | None


@dataclass(frozen=True)
class AnswerOutcome:
    """A recorded answer and its coaching — the result of submitting one answer.

    `evaluation is None` when coaching could not be produced for this answer (the provider
    failed, or its coaching did not survive the evidence guard): the answer is still stored
    and the session still advances, because a lost evaluation must never cost a candidate
    their answer. When an evaluation is present, `session` reflects any difficulty the engine
    adapted from it (§32).
    """

    session: InterviewSession
    answer: InterviewAnswer
    evaluation: InterviewAnswerEvaluation | None


@dataclass(frozen=True)
class SessionDetail:
    """One session with its whole exchange — questions, answers, evaluations, summary.

    The read a transcript or history view composes from: everything a session holds, loaded
    under one ownership gate, so the surface renders without re-checking who owns what.
    """

    session: InterviewSession
    questions: tuple[InterviewQuestion, ...]
    answers: tuple[InterviewAnswer, ...]
    evaluations: tuple[InterviewAnswerEvaluation, ...]
    summary: InterviewSessionSummary | None


class InterviewService:
    """Drives an adaptive interview-practice session over its repositories (§1-6, §54-59).

    Every collaborator is handed in: the five interview repositories (over PostgreSQL in
    production, over fakes in a flow test), the `InterviewContextBuilder` that loads the
    minimal per-session grounding, the provider-neutral `InterviewLLM`, and the pure engine
    and guard. The engine and guard default to fresh instances (they are stateless), so a
    test need not supply them. Nothing here authors readiness or executes anything outside the
    session; it plans, asks, records, grades and aggregates — coaching, never prediction.
    """

    def __init__(self, *, sessions: InterviewSessionRepository,
                 questions: InterviewQuestionRepository,
                 answers: InterviewAnswerRepository,
                 evaluations: InterviewAnswerEvaluationRepository,
                 summaries: InterviewSessionSummaryRepository,
                 context: InterviewContextBuilder,
                 llm: InterviewLLM,
                 transcriber: SpeechTranscriber,
                 engine: InterviewQuestionEngine | None = None,
                 guard: InterviewCoachingGuard | None = None) -> None:
        self._sessions = sessions
        self._questions = questions
        self._answers = answers
        self._evaluations = evaluations
        self._summaries = summaries
        self._context = context
        self._llm = llm
        self._transcriber = transcriber
        self._engine = engine or InterviewQuestionEngine()
        self._guard = guard or InterviewCoachingGuard()

    async def create_session(self, user_id: UserId, *,
                             candidate_profile_id: CandidateProfileId,
                             opportunity_id: OpportunityId,
                             mode: InterviewMode, now: datetime,
                             style: SessionStyle = SessionStyle.COACHING,
                             difficulty: InterviewDifficulty = InterviewDifficulty.INTERMEDIATE,
                             language: LanguageCode | None = None,
                             application_id: ApplicationId | None = None,
                             title: str | None = None) -> InterviewSession:
        """Plan and open a `CREATED` session for one posting (§20-25).

        Loads the grounding (owner-first, so a foreign or missing profile/posting raises
        `InterviewGroundingNotFound`), asks the provider for a coverage plan and re-validates
        it against the domain (a bad plan is `QUESTION_GENERATION_UNAVAILABLE`, never a
        half-built session), and stores a planned-but-not-started session. The plan's mode is
        stamped by the platform, never read from the payload, so the session and its plan can
        never disagree.
        """
        context = await self._context.build(
            user_id=user_id, candidate_profile_id=candidate_profile_id,
            opportunity_id=opportunity_id)
        if context is None:
            raise InterviewGroundingNotFound(candidate_profile_id, opportunity_id)
        plan = await self._propose_plan(context, mode)
        session = InterviewSession(
            id=new_interview_session_id(), user_id=user_id,
            candidate_profile_id=candidate_profile_id, opportunity_id=opportunity_id,
            application_id=application_id, mode=mode, style=style, difficulty=difficulty,
            status=InterviewSessionStatus.CREATED, language=language, plan=plan,
            title=_derive_title(title, context), created_at=now, updated_at=now)
        return await self._sessions.upsert(session)

    async def get_session(self, user_id: UserId,
                          session_id: InterviewSessionId) -> InterviewSession:
        """One session, or `InterviewError(SESSION_NOT_FOUND)` when it is not this account's."""
        return await self._require_session(user_id, session_id)

    async def list_sessions(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[InterviewSession, ...]:
        """This account's sessions, most recently updated first."""
        return await self._sessions.list_for_user(user_id, limit=limit)

    async def session_detail(self, user_id: UserId,
                             session_id: InterviewSessionId) -> SessionDetail:
        """One session with its whole exchange, after the ownership gate."""
        session = await self._require_session(user_id, session_id)
        questions = await self._questions.list_for_session(user_id, session_id)
        answers = await self._answers.list_for_session(user_id, session_id)
        evaluations = await self._evaluations.list_for_session(user_id, session_id)
        summary = await self._summaries.get(user_id, session_id)
        return SessionDetail(session=session, questions=questions, answers=answers,
                             evaluations=evaluations, summary=summary)

    async def readiness_history(self, user_id: UserId, *,
                                limit: int = DEFAULT_LIMIT
                                ) -> tuple[InterviewSessionSummary, ...]:
        """This account's completed-session summaries, most recent first — the history (§74)."""
        return await self._summaries.list_for_user(user_id, limit=limit)

    async def session_readiness(self, user_id: UserId, session_id: InterviewSessionId, *,
                                now: datetime) -> SessionReadiness:
        """The session's readiness as it stands now, computed live from stored grades (§33-36).

        The same deterministic aggregation `complete_session` uses, run without ending the
        session — so a UI can show progress mid-practice. No provider is in this call.
        """
        session = await self._require_session(user_id, session_id)
        answers = await self._answers.list_for_session(user_id, session_id)
        evaluations = await self._evaluations.list_for_session(user_id, session_id)
        return aggregate_session_readiness(
            evaluations=tuple(evaluations), plan=session.plan,
            profile=session.readiness_profile(), answered_questions=len(answers),
            computed_at=now)

    async def next_question(self, user_id: UserId, session_id: InterviewSessionId, *,
                            now: datetime) -> InterviewTurn:
        """The current question to answer, or the signal the plan is done (§26-32, §64-72).

        Idempotent: if the last question is still unanswered it is returned again, so polling
        never asks twice. Otherwise the session advances — a `CREATED` session becomes
        `IN_PROGRESS` on its first question — and the engine decides the next step: an allowed,
        provider-warranted follow-up drilling the last answer, else the next uncovered plan
        topic. When neither yields a question (the plan is covered or the bound is reached),
        the turn carries `question=None` and the caller completes the session.
        """
        session = await self._require_active(user_id, session_id)
        asked = await self._questions.list_for_session(user_id, session_id)
        answers = await self._answers.list_for_session(user_id, session_id)
        answered = {answer.question_id for answer in answers}
        if asked and asked[-1].id not in answered:
            return InterviewTurn(session=session, question=asked[-1])
        if session.status is InterviewSessionStatus.CREATED:
            session = await self._advance_to_in_progress(session, now)
        request = await self._next_request(session, asked, answers)
        if request is None:
            return InterviewTurn(session=session, question=None)
        question = await self._generate_question(session, request, asked, now)
        return InterviewTurn(session=session, question=question)

    async def submit_text_answer(self, user_id: UserId, session_id: InterviewSessionId,
                                 content: str, *, now: datetime) -> AnswerOutcome:
        """Record a typed answer to the current question and grade it best-effort (§13, §18)."""
        return await self._submit(user_id, session_id,
                                  fmt=InterviewAnswerFormat.TEXT, content=content,
                                  transcript_confidence=None, now=now)

    async def submit_voice_answer(self, user_id: UserId, session_id: InterviewSessionId,
                                  audio: InterviewAudio, *, now: datetime) -> AnswerOutcome:
        """Transcribe a spoken answer, then record and grade it exactly like text (§14-17).

        The session is confirmed active *before* a byte is transcribed, so a dead session
        spends no transcription. The raw audio is transcribed and discarded by the transcriber
        itself; only the text is stored. A transcriber refusal (unavailable, too large,
        unsupported, or an empty transcript) surfaces as the matching typed `InterviewError`.
        """
        await self._require_active(user_id, session_id)
        try:
            result = await self._transcriber.transcribe(audio)
        except TranscriptionError as exc:
            raise InterviewError(exc.code, exc.detail) from exc
        if not result.text.strip():
            raise InterviewError(InterviewErrorCode.TRANSCRIPTION_UNAVAILABLE,
                                 "the transcript was empty")
        return await self._submit(user_id, session_id,
                                  fmt=InterviewAnswerFormat.VOICE, content=result.text,
                                  transcript_confidence=result.confidence, now=now)

    async def evaluate_answer(self, user_id: UserId, session_id: InterviewSessionId, *,
                              question_sequence: int,
                              now: datetime) -> InterviewAnswerEvaluation:
        """(Re)grade one answered question, surfacing failure as a typed error (§19-24).

        The strict counterpart to the best-effort grading `submit_*` does: where a submit
        keeps an un-gradable answer and reports `evaluation=None`, this raises
        `EVALUATION_UNAVAILABLE` when the provider fails or its coaching does not survive the
        evidence guard — the path a caller uses to retry coaching for an answer that lacked
        it. It does not adapt difficulty (that is the submit's job, done once), so a retry
        never double-counts. `QUESTION_NOT_FOUND` when no question holds that sequence;
        `NO_CURRENT_QUESTION` when the question exists but has no answer to grade.
        """
        session = await self._require_active(user_id, session_id)
        question = await self._question_at(user_id, session_id, question_sequence)
        answer = await self._answers.get_for_question(user_id, question.id)
        if answer is None:
            raise InterviewError(InterviewErrorCode.NO_CURRENT_QUESTION,
                                 "that question has no answer to evaluate")
        return await self._evaluate(session, question, answer, now)

    async def complete_session(self, user_id: UserId, session_id: InterviewSessionId, *,
                               now: datetime) -> InterviewSessionSummary:
        """Aggregate readiness, write the closing summary, and finalize the session (§37-39).

        Readiness is computed by the domain from the stored grades — never a provider — and
        paired with coaching prose the provider writes and the guard clears. If the prose
        fails (the provider errored or its words were not grounded), the session still
        completes with a safe, deterministic, fact-free headline: the readiness stands either
        way. The summary is stored, then the session transitions `IN_PROGRESS → COMPLETED`
        with its `ended_at` stamped; a session not in progress raises
        `INVALID_STATUS_TRANSITION`.
        """
        session = await self._require_session(user_id, session_id)
        if not can_transition_session(session.status, InterviewSessionStatus.COMPLETED):
            raise InterviewError(
                InterviewErrorCode.INVALID_STATUS_TRANSITION,
                f"a {session.status.value} session cannot be completed")
        questions = await self._questions.list_for_session(user_id, session_id)
        answers = await self._answers.list_for_session(user_id, session_id)
        evaluations = await self._evaluations.list_for_session(user_id, session_id)
        readiness = aggregate_session_readiness(
            evaluations=tuple(evaluations), plan=session.plan,
            profile=session.readiness_profile(), answered_questions=len(answers),
            computed_at=now)
        summary = await self._compose_summary(
            session, readiness=readiness, questions=questions, answers=answers, now=now)
        stored = await self._summaries.upsert(summary)
        completed = session.model_copy(update={
            "status": InterviewSessionStatus.COMPLETED, "ended_at": now, "updated_at": now})
        await self._sessions.upsert(completed)
        return stored

    async def abandon_session(self, user_id: UserId, session_id: InterviewSessionId, *,
                              now: datetime) -> InterviewSession:
        """Walk away from a session, moving it to the terminal `ABANDONED` state (§6).

        A `CREATED` or `IN_PROGRESS` session becomes `ABANDONED` with `ended_at` stamped; a
        session already terminal raises `INVALID_STATUS_TRANSITION`, because nothing follows a
        terminal state. No summary is written — an abandoned session has no closing coaching.
        """
        session = await self._require_session(user_id, session_id)
        if not can_transition_session(session.status, InterviewSessionStatus.ABANDONED):
            raise InterviewError(
                InterviewErrorCode.INVALID_STATUS_TRANSITION,
                f"a {session.status.value} session cannot be abandoned")
        abandoned = session.model_copy(update={
            "status": InterviewSessionStatus.ABANDONED, "ended_at": now, "updated_at": now})
        return await self._sessions.upsert(abandoned)

    async def _submit(self, user_id: UserId, session_id: InterviewSessionId, *,
                      fmt: InterviewAnswerFormat, content: str,
                      transcript_confidence: float | None,
                      now: datetime) -> AnswerOutcome:
        """Record one answer to the current question and grade it best-effort.

        The current question is the last one asked; if it already has an answer the submit is
        `QUESTION_ALREADY_ANSWERED` (one answer per question in Phase 14), and if no question
        has been asked it is `NO_CURRENT_QUESTION`. The answer is stored first, then graded:
        grading is best-effort, so a provider or guard failure yields `evaluation=None` rather
        than losing the stored answer. A produced evaluation adapts the session's difficulty
        for the next question (§32).
        """
        session = await self._require_active(user_id, session_id)
        asked = await self._questions.list_for_session(user_id, session_id)
        if not asked:
            raise InterviewError(InterviewErrorCode.NO_CURRENT_QUESTION,
                                 "no question has been asked yet")
        current = asked[-1]
        if await self._answers.get_for_question(user_id, current.id) is not None:
            raise InterviewError(InterviewErrorCode.QUESTION_ALREADY_ANSWERED,
                                 "the current question already has an answer")
        answer = await self._answers.upsert(InterviewAnswer(
            id=interview_answer_id(current.id), question_id=current.id,
            session_id=session.id, user_id=session.user_id, format=fmt, content=content,
            transcript_confidence=transcript_confidence, answered_at=now))
        evaluation = await self._try_evaluate(session, current, answer, now)
        if evaluation is not None:
            session = await self._apply_difficulty(session, evaluation, now)
        return AnswerOutcome(session=session, answer=answer, evaluation=evaluation)

    async def _next_request(self, session: InterviewSession,
                            asked: Sequence[InterviewQuestion],
                            answers: Sequence[InterviewAnswer]) -> QuestionRequest | None:
        """The engine's decision for the next question: a follow-up, a primary, or nothing.

        A follow-up on the last answered question is considered first, but only when the
        engine allows it (depth and bounds) and the provider warrants it; a provider failure
        deciding the follow-up is swallowed and the session falls through to its plan, so a
        hiccup never stalls coverage. When no follow-up is taken, the next uncovered plan topic
        is asked; `None` means the plan is covered or the session is out of capacity.
        """
        if asked:
            last = asked[-1]
            by_question = {answer.question_id: answer for answer in answers}
            last_answer = by_question.get(last.id)
            if last_answer is not None and self._engine.may_follow_up(last=last, asked=asked):
                context = await self._context_for(session)
                try:
                    decision = await self._llm.decide_follow_up(
                        context=context, question=last, answer=last_answer)
                except LLMError:
                    decision = None
                if decision is not None:
                    follow_up = self._engine.next_follow_up(
                        last=last, asked=asked, difficulty=session.difficulty,
                        decision=decision)
                    if follow_up is not None:
                        return follow_up
        return self._engine.next_primary(
            plan=session.plan, asked=asked, difficulty=session.difficulty)

    async def _generate_question(self, session: InterviewSession, request: QuestionRequest,
                                 asked: Sequence[InterviewQuestion],
                                 now: datetime) -> InterviewQuestion:
        """Realize a `QuestionRequest`: the model writes the prompt, the engine owns the rest.

        The provider writes only the prompt text (and is told which prompts not to repeat);
        the type, difficulty, sequence, depth and follow-up link are the engine's and are
        stamped from the request, not read from the proposal. A provider failure here is
        `QUESTION_GENERATION_UNAVAILABLE` — there is no question to return, so this is the one
        generation failure that surfaces rather than degrading.
        """
        context = await self._context_for(session)
        already_asked = tuple(question.prompt for question in asked)
        try:
            proposal = await self._llm.generate_question(
                context=context, mode=session.mode, question_type=request.question_type,
                difficulty=request.difficulty, topic_label=request.topic_label,
                already_asked=already_asked)
        except LLMError as exc:
            raise InterviewError(InterviewErrorCode.QUESTION_GENERATION_UNAVAILABLE,
                                 "the question generator is unavailable") from exc
        question = InterviewQuestion(
            id=interview_question_id(session.id, request.sequence),
            session_id=session.id, user_id=session.user_id, sequence=request.sequence,
            question_type=request.question_type, difficulty=request.difficulty,
            prompt=proposal.prompt, topic_label=request.topic_label,
            follows_sequence=request.follows_sequence, depth=request.depth,
            generator_key=self._llm.key, asked_at=now)
        return await self._questions.upsert(question)

    async def _try_evaluate(self, session: InterviewSession, question: InterviewQuestion,
                            answer: InterviewAnswer,
                            now: datetime) -> InterviewAnswerEvaluation | None:
        """Grade an answer, returning `None` rather than raising when coaching is unavailable."""
        try:
            return await self._evaluate(session, question, answer, now)
        except InterviewError:
            return None

    async def _evaluate(self, session: InterviewSession, question: InterviewQuestion,
                        answer: InterviewAnswer,
                        now: datetime) -> InterviewAnswerEvaluation:
        """Grade one answer, guard its coaching, and persist it — or raise (§19-24, §40-44).

        The provider returns per-axis grades and coaching prose; it can never author readiness
        (the schema has no such field). Every candidate-facing string is run through the Phase
        10 evidence guard *before* the evaluation is stored, so a fabricated strength or a
        drafted answer that invents a fact is refused whole: a provider failure or a guard
        rejection is `EVALUATION_UNAVAILABLE`, and nothing ungrounded is ever persisted.
        """
        context = await self._context_for(session)
        try:
            evaluation = await self._llm.evaluate_answer(
                context=context, question=question, answer=answer, evaluated_at=now)
        except LLMError as exc:
            raise InterviewError(InterviewErrorCode.EVALUATION_UNAVAILABLE,
                                 "the answer evaluator is unavailable") from exc
        report = self._guard.review_evaluation(
            evaluation, profile=context.profile, opportunity=context.opportunity)
        if not report.ok:
            raise InterviewError(
                InterviewErrorCode.EVALUATION_UNAVAILABLE,
                "the evaluation's coaching was not grounded in the candidate's evidence")
        return await self._evaluations.upsert(evaluation)

    async def _compose_summary(self, session: InterviewSession, *,
                               readiness: SessionReadiness,
                               questions: Sequence[InterviewQuestion],
                               answers: Sequence[InterviewAnswer],
                               now: datetime) -> InterviewSessionSummary:
        """Build the closing summary: platform readiness paired with guarded coaching prose.

        The provider writes a headline, strengths and focus areas *about the practice*; they
        are accepted only if the evidence guard clears them. If the provider fails or its prose
        is rejected, the summary falls back to a safe, deterministic, fact-free headline — the
        readiness, computed by the platform, is the trustworthy signal in either case and is
        never touched by the fallback.
        """
        def build(*, headline: str, strengths: tuple[str, ...],
                  focus_areas: tuple[str, ...], generator_key: str) -> InterviewSessionSummary:
            return InterviewSessionSummary(
                id=interview_session_summary_id(session.id), session_id=session.id,
                user_id=session.user_id, readiness=readiness, headline=headline,
                strengths=strengths, focus_areas=focus_areas,
                questions_asked=len(questions),
                answers_evaluated=readiness.evaluated_answers,
                generator_key=generator_key, created_at=now)

        context = await self._context_for(session)
        try:
            proposed = await self._llm.generate_summary(
                context=context, mode=session.mode,
                transcript=_render_transcript(questions, answers))
        except LLMError:
            proposed = None
        if proposed is not None:
            candidate = build(
                headline=proposed.headline, strengths=proposed.strengths,
                focus_areas=proposed.focus_areas, generator_key=self._llm.key)
            report = self._guard.review_summary(
                candidate, profile=context.profile, opportunity=context.opportunity)
            if report.ok:
                return candidate
        return build(headline=_FALLBACK_SUMMARY_HEADLINE, strengths=(), focus_areas=(),
                     generator_key=_DETERMINISTIC_SUMMARY_KEY)

    async def _propose_plan(self, context: InterviewContext,
                            mode: InterviewMode) -> InterviewPlan:
        """Ask the provider for a coverage plan, mapping a failure to a typed error.

        A plan that does not parse into the domain, or a provider that fails, is
        `QUESTION_GENERATION_UNAVAILABLE`: without a plan there is no session to open, so this
        is a hard failure, not a degrade.
        """
        try:
            return await self._llm.propose_plan(context=context, mode=mode)
        except LLMError as exc:
            raise InterviewError(InterviewErrorCode.QUESTION_GENERATION_UNAVAILABLE,
                                 "the interview planner is unavailable") from exc

    async def _apply_difficulty(self, session: InterviewSession,
                                evaluation: InterviewAnswerEvaluation,
                                now: datetime) -> InterviewSession:
        """Adapt the session's difficulty from one grade, writing only when it changed (§32)."""
        adapted = self._engine.adapt_difficulty_after(
            current=session.difficulty, evaluation=evaluation)
        if adapted is session.difficulty:
            return session
        return await self._sessions.upsert(session.model_copy(update={
            "difficulty": adapted, "updated_at": now}))

    async def _advance_to_in_progress(self, session: InterviewSession,
                                      now: datetime) -> InterviewSession:
        """Move a `CREATED` session to `IN_PROGRESS` as its first question is asked (§6)."""
        return await self._sessions.upsert(session.model_copy(update={
            "status": InterviewSessionStatus.IN_PROGRESS, "updated_at": now}))

    async def _context_for(self, session: InterviewSession) -> InterviewContext:
        """Reload the session's grounding, treating a vanished one as `SESSION_NOT_FOUND`.

        The grounding was resolvable when the session opened; if the profile or posting is
        gone now, the session can no longer be driven, so it reads as absent rather than
        raising an untyped error mid-turn.
        """
        context = await self._context.build(
            user_id=session.user_id, candidate_profile_id=session.candidate_profile_id,
            opportunity_id=session.opportunity_id)
        if context is None:
            raise InterviewError(
                InterviewErrorCode.SESSION_NOT_FOUND,
                "the session's candidate profile or posting is no longer available")
        return context

    async def _require_session(self, user_id: UserId,
                               session_id: InterviewSessionId) -> InterviewSession:
        """Load a session scoped to its owner, or raise `SESSION_NOT_FOUND`."""
        session = await self._sessions.get(user_id, session_id)
        if session is None:
            raise InterviewError(InterviewErrorCode.SESSION_NOT_FOUND,
                                 "no such session for this account")
        return session

    async def _require_active(self, user_id: UserId,
                              session_id: InterviewSessionId) -> InterviewSession:
        """Load a session and refuse it if it has already reached a terminal state."""
        session = await self._require_session(user_id, session_id)
        if not session.is_active:
            raise InterviewError(InterviewErrorCode.SESSION_NOT_ACTIVE,
                                 f"a {session.status.value} session takes no more turns")
        return session

    async def _question_at(self, user_id: UserId, session_id: InterviewSessionId,
                           sequence: int) -> InterviewQuestion:
        """The question at one sequence in a session, or raise `QUESTION_NOT_FOUND`."""
        questions = await self._questions.list_for_session(user_id, session_id)
        for question in questions:
            if question.sequence == sequence:
                return question
        raise InterviewError(InterviewErrorCode.QUESTION_NOT_FOUND,
                             f"no question at sequence {sequence} in this session")


def _derive_title(title: str | None, context: InterviewContext) -> str:
    """A short, non-empty session caption: the caller's, or one from the posting.

    The caller may name a session; an absent or blank name falls back to the posting's title
    and company — a caption a list can show, never model-authored authority.
    """
    trimmed = (title or "").strip()
    if not trimmed:
        trimmed = f"{context.opportunity.title} — {context.opportunity.company_name}"
    if len(trimmed) > _MAX_TITLE_LENGTH:
        return trimmed[:_MAX_TITLE_LENGTH - 1] + "…"
    return trimmed


def _render_transcript(questions: Sequence[InterviewQuestion],
                       answers: Sequence[InterviewAnswer]) -> str:
    """A plain question-and-answer transcript for the summary prompt (§37-39).

    Sequence-ordered, each question with its type and the candidate's own words beneath it (or
    a plain marker when a question went unanswered). Deterministic, so two runs over one
    session render the same transcript.
    """
    by_question = {answer.question_id: answer for answer in answers}
    blocks: list[str] = []
    for question in questions:
        answer = by_question.get(question.id)
        spoken = answer.content if answer is not None else "(no answer given)"
        blocks.append(
            f"Q{question.sequence} [{question.question_type.value}]: {question.prompt}\n"
            f"A{question.sequence}: {spoken}")
    return "\n\n".join(blocks)





