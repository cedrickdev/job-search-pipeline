"""The adaptive interview simulator: sessions, questions, answers, evaluations, readiness.

Phase 14 turns V1's static prep sheet into a *practice* surface with a memory. The one
idea the whole phase rests on is the mirror of Phase 13's "prose has zero authority":
**the simulator coaches, it does not predict**. Nothing here — no field, no method, no
band — is a probability that a real recruiter will say yes. Readiness is a coaching
signal about how a *practice* session went, computed deterministically by the platform
from structured evaluations, never authored by a provider (§33-36, §123). That is why
`InterviewAnswerEvaluation` has no readiness field at all: a model grades one answer on
a few axes, and the platform — not the model — decides what that means for the session.

Three boundaries shape every model below:

- **Closed vocabularies, `extra="forbid"`.** Mode, difficulty, question type, evaluation
  dimension and error code are `StrEnum`s, exactly as `ChatActionKind` and `MatchDimension`
  are: a provider that invents a dimension or a mode fails to parse into a domain object
  rather than smuggling a new concept past the type system.
- **The candidate's facts are authoritative and never invented.** A question is grounded
  in the posting and the profile but may not assert a candidate fact; coaching text that
  would put words in the candidate's mouth is caught by the Phase 10 evidence guard before
  it is ever persisted (§10, §40-44). The posting text is untrusted input, fenced before
  it reaches a provider, exactly as the document generator fences it.
- **Readiness is aggregation, not authorship.** `aggregate_session_readiness` is a pure
  function of the stored evaluations and the coverage plan; it lives in the domain so the
  number is reproducible and auditable, and so no surface can invent its own recipe.

These are pure domain values: `backend.app.domain` imports the standard library and
Pydantic and nothing else (docs/ARCHITECTURE.md §1). The service layer under
`backend.app.interview` is what drives a session, calls a provider, guards its output and
persists it; the voice adapter and the LLM tasks live behind their own protocols.
"""
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.base import (
    DomainModel,
    LanguageCode,
    NonEmptyStr,
    Score,
    UtcDatetime,
)
from backend.app.domain.identifiers import (
    ApplicationId,
    CandidateProfileId,
    InterviewAnswerEvaluationId,
    InterviewAnswerId,
    InterviewQuestionId,
    InterviewSessionId,
    InterviewSessionSummaryId,
    LLMRunId,
    OpportunityId,
    UserId,
)

# How many questions a single session may ever hold. A bound, not a guess: the engine
# asks one question at a time and stops at the plan's target, but an adaptive follow-up
# loop that never converged would otherwise ask forever. The ceiling is generous enough
# that no honest session reaches it and low enough that a runaway loop is caught (§64-72).
MAX_QUESTIONS_PER_SESSION: int = 40

# How many adaptive follow-ups may chain off a single primary question. A follow-up that
# begets a follow-up that begets a follow-up is a provider stuck on one topic, not a
# richer interview; the engine caps the depth so a session covers its plan (§26-29).
MAX_FOLLOW_UP_DEPTH: int = 2


class InterviewMode(StrEnum):
    """The kind of interview a session rehearses — the closed set of stages (§4).

    Each member is a real, recognizable round a candidate prepares for differently: an
    HR screen is not a technical loop, and a case study is not a hiring-manager chat. The
    mode decides which competencies the plan targets, how the question engine phrases
    prompts, and which readiness profile aggregates the answers, so it must be a value the
    domain understands rather than a free label a caller invents.
    """

    RECRUITER_HR = "RECRUITER_HR"
    BEHAVIORAL = "BEHAVIORAL"
    TECHNICAL = "TECHNICAL"
    HIRING_MANAGER = "HIRING_MANAGER"
    CASE_STUDY = "CASE_STUDY"
    FINAL_INTERVIEW = "FINAL_INTERVIEW"


class SessionStyle(StrEnum):
    """How the simulator conducts itself (§80).

    `COACHING` is the default and the point of the phase: the simulator explains, offers
    strengths and focus areas, and is generous with follow-ups that teach. `REALISTIC`
    dials the hand-holding down to something closer to a real room — terser, fewer
    scaffolding cues — without ever changing what is *true*: both styles run the same
    evidence guard and the same deterministic readiness, because coaching honesty is not
    a style setting (§123).
    """

    COACHING = "COACHING"
    REALISTIC = "REALISTIC"


class InterviewDifficulty(StrEnum):
    """How demanding a question is, on an ordered three-step scale (§5, §32).

    Ordered on purpose: the engine adapts difficulty *up* after a strong answer and *down*
    after a weak one (§32), so the domain has to know that `ADVANCED` is harder than
    `INTERMEDIATE`. The order lives in `_DIFFICULTY_ORDER` rather than in the member
    definitions, because `StrEnum` compares by string value and "ADVANCED" < "INTERMEDIATE"
    alphabetically would be exactly the wrong answer.
    """

    INTRODUCTORY = "INTRODUCTORY"
    INTERMEDIATE = "INTERMEDIATE"
    ADVANCED = "ADVANCED"


# The difficulty ladder, low to high. `adapt_difficulty` steps along it; nothing else
# should hard-code the order, so a fourth rung is added here and works everywhere.
_DIFFICULTY_ORDER: tuple[InterviewDifficulty, ...] = (
    InterviewDifficulty.INTRODUCTORY,
    InterviewDifficulty.INTERMEDIATE,
    InterviewDifficulty.ADVANCED,
)


def adapt_difficulty(current: InterviewDifficulty, *, steps: int) -> InterviewDifficulty:
    """The difficulty `steps` rungs from `current`, clamped to the ends of the ladder.

    Positive `steps` climbs (a strong answer earns a harder question), negative descends
    (a weak one earns an easier one). Clamped rather than wrapped: the hardest question
    followed by another strong answer stays hardest — there is no rung above `ADVANCED`
    and pretending otherwise would ask for a difficulty the engine cannot render.
    """
    index = _DIFFICULTY_ORDER.index(current) + steps
    index = max(0, min(index, len(_DIFFICULTY_ORDER) - 1))
    return _DIFFICULTY_ORDER[index]


class InterviewSessionStatus(StrEnum):
    """Where a session is in its life, as a closed state machine (§6).

    A session is born `CREATED` — planned but not yet started. The first question moves it
    to `IN_PROGRESS`, where it stays through every question, answer and evaluation. It
    leaves that state exactly once, into one of two terminal states: `COMPLETED` when the
    candidate finishes and readiness is aggregated, or `ABANDONED` when they walk away. The
    terminal states are final — a completed session's history is immutable (§37-39), and an
    abandoned one is not resumed but restarted. `_ALLOWED_STATUS_TRANSITIONS` is the whole
    machine; `can_transition_to` is the only thing that reads it.
    """

    CREATED = "CREATED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


# The only status moves the machine allows. A move not listed here is a bug in a caller,
# refused by `can_transition_to` rather than quietly performed — the same shape the Phase
# 12 application lifecycle uses. `COMPLETED` and `ABANDONED` map to the empty set: nothing
# follows a terminal state.
_ALLOWED_STATUS_TRANSITIONS: dict[InterviewSessionStatus, frozenset[InterviewSessionStatus]] = {
    InterviewSessionStatus.CREATED: frozenset({
        InterviewSessionStatus.IN_PROGRESS,
        InterviewSessionStatus.ABANDONED,
    }),
    InterviewSessionStatus.IN_PROGRESS: frozenset({
        InterviewSessionStatus.COMPLETED,
        InterviewSessionStatus.ABANDONED,
    }),
    InterviewSessionStatus.COMPLETED: frozenset(),
    InterviewSessionStatus.ABANDONED: frozenset(),
}

# The terminal states, named once so the invariant on `InterviewSession.ended_at` and the
# service's "may this session still take an answer?" check read the same fact.
TERMINAL_SESSION_STATUSES: frozenset[InterviewSessionStatus] = frozenset({
    InterviewSessionStatus.COMPLETED,
    InterviewSessionStatus.ABANDONED,
})


def can_transition_session(current: InterviewSessionStatus,
                           target: InterviewSessionStatus) -> bool:
    """Whether the session lifecycle permits moving from `current` to `target`."""
    return target in _ALLOWED_STATUS_TRANSITIONS[current]


class InterviewQuestionType(StrEnum):
    """The topical kind of a single question — the closed question vocabulary (§8-9).

    Orthogonal to `InterviewMode`: a `TECHNICAL` mode is mostly `TECHNICAL` and `CASE`
    questions but opens with a `BACKGROUND` one and closes with `CANDIDATE_QUESTIONS`, and a
    `BEHAVIORAL` mode leans on `BEHAVIORAL` and `SITUATIONAL`. The type drives how an answer
    is evaluated (a `BACKGROUND` warm-up is not graded for technical depth) and which planned
    topic a question discharges, so it is typed rather than left to prose.
    """

    BACKGROUND = "BACKGROUND"
    MOTIVATION = "MOTIVATION"
    BEHAVIORAL = "BEHAVIORAL"
    SITUATIONAL = "SITUATIONAL"
    TECHNICAL = "TECHNICAL"
    CASE = "CASE"
    ROLE_KNOWLEDGE = "ROLE_KNOWLEDGE"
    CANDIDATE_QUESTIONS = "CANDIDATE_QUESTIONS"


class InterviewAnswerFormat(StrEnum):
    """How the candidate gave one answer (§13).

    `TEXT` is typed; `VOICE` is spoken, transcribed to text and then treated exactly like
    text — the transcript is what is stored and evaluated, and the raw audio is discarded
    (§17). The distinction survives only so the UI can show how an answer was given and so a
    voice answer can carry the transcriber's confidence, which a typed answer has no notion
    of.
    """

    TEXT = "TEXT"
    VOICE = "VOICE"


class EvaluationDimension(StrEnum):
    """The axes one answer is graded on — the closed set of evaluation dimensions (§18).

    The first three are the minimum the spec mandates and mean what a coach means by them:
    `CLARITY` is whether the answer is understandable, `RELEVANCE` whether it addresses the
    question asked, `COMPLETENESS` whether it leaves out something the question needed.
    `STRUCTURE` (does the answer have a shape — situation, action, result — or wander) and
    `SPECIFICITY` (concrete detail versus generality) round out the coaching picture without
    ever straying into anything a real recruiter decides. All five are *compatibility with
    good answering*, never a hiring verdict — the same discipline that keeps eligibility out
    of `MatchDimension`.
    """

    CLARITY = "CLARITY"
    RELEVANCE = "RELEVANCE"
    COMPLETENESS = "COMPLETENESS"
    STRUCTURE = "STRUCTURE"
    SPECIFICITY = "SPECIFICITY"


class EvaluationStatus(StrEnum):
    """Whether one dimension of one answer was actually graded (§22-24).

    The load-bearing distinction of the whole evaluation model, and the exact analogue of
    `MatchClassification.UNKNOWN` ≠ `WEAK`: `NOT_EVALUATED` is *not* a score of zero. A
    provider that could not judge specificity — because the question did not call for it, or
    the answer was too short to tell — records `NOT_EVALUATED`, and the readiness aggregation
    then *omits* that dimension rather than averaging in a zero. Grading something at 0.0 says
    "this was bad"; `NOT_EVALUATED` says "we did not assess this", and conflating the two is
    how a warm-up question quietly tanks a readiness number.
    """

    EVALUATED = "EVALUATED"
    NOT_EVALUATED = "NOT_EVALUATED"


class ReadinessBand(StrEnum):
    """The coaching band a session's readiness falls in — named once, invented nowhere (§34).

    A practice trajectory, never a hiring probability (§123). The names describe how far
    *rehearsal* has come — `EARLY` through `POLISHED` — and deliberately avoid any word that
    reads as "will get the offer". `UNKNOWN` is the honest answer when no answer in the
    session could be evaluated at all; like its match-domain twin it must never be rendered as
    a low band, because "we could not assess this practice" and "this practice went badly" are
    different facts. The thresholds live on `ReadinessProfile`, so no surface hard-codes a
    `if score > 0.8`.
    """

    UNKNOWN = "UNKNOWN"
    EARLY = "EARLY"
    DEVELOPING = "DEVELOPING"
    PROGRESSING = "PROGRESSING"
    POLISHED = "POLISHED"


class InterviewErrorCode(StrEnum):
    """The stable vocabulary of ways an interview operation can be refused (§90).

    A closed set of machine codes the service raises and the API maps to status codes, so a
    surface never has to parse a prose message to know what went wrong. Ownership failures
    deliberately surface as `SESSION_NOT_FOUND` rather than a "forbidden" code: a session that
    belongs to another account must read as absent, never as "exists but denied", which would
    leak that it exists at all (the same discipline Phase 13's executor uses).
    """

    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_NOT_ACTIVE = "SESSION_NOT_ACTIVE"
    INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"
    QUESTION_NOT_FOUND = "QUESTION_NOT_FOUND"
    NO_CURRENT_QUESTION = "NO_CURRENT_QUESTION"
    QUESTION_ALREADY_ANSWERED = "QUESTION_ALREADY_ANSWERED"
    ANSWER_OUT_OF_ORDER = "ANSWER_OUT_OF_ORDER"
    # Two writers raced for the same session state — a second question at a sequence, a second
    # answer to a question — and the loser is refused rather than surfacing a raw IntegrityError
    # or overwriting the winner (§41-43). The natural key made the collision physical; this
    # names it so a caller retries against the state the winner committed.
    INTERVIEW_STATE_CONFLICT = "INTERVIEW_STATE_CONFLICT"
    SESSION_LIMIT_REACHED = "SESSION_LIMIT_REACHED"
    EVALUATION_UNAVAILABLE = "EVALUATION_UNAVAILABLE"
    QUESTION_GENERATION_UNAVAILABLE = "QUESTION_GENERATION_UNAVAILABLE"
    # A generated question failed the deterministic evidence guard even after one repair —
    # it is refused rather than persisted, so a session never records a question that
    # asserts a candidate fact no evidence supports (§10-17).
    QUESTION_GROUNDING_FAILED = "QUESTION_GROUNDING_FAILED"
    # The session named an `application_id` that does not resolve for this user — missing or
    # owned by someone else, deliberately one code so the two are indistinguishable (§2-9).
    APPLICATION_NOT_FOUND = "APPLICATION_NOT_FOUND"
    # The named application is this user's, but rehearses a different opportunity than the
    # session — refused so a session can never borrow another role's application (§62-63).
    APPLICATION_OPPORTUNITY_MISMATCH = "APPLICATION_OPPORTUNITY_MISMATCH"
    TRANSCRIPTION_UNAVAILABLE = "TRANSCRIPTION_UNAVAILABLE"
    AUDIO_TOO_LARGE = "AUDIO_TOO_LARGE"
    UNSUPPORTED_AUDIO = "UNSUPPORTED_AUDIO"
    # A voice answer transcribed with a confidence below the auto-evaluate threshold — held
    # for the candidate to confirm the transcript, never auto-graded, so speech-to-text
    # uncertainty never silently shapes readiness (§44-49).
    TRANSCRIPT_REVIEW_REQUIRED = "TRANSCRIPT_REVIEW_REQUIRED"


# --- the coverage plan -----------------------------------------------------------------


class InterviewTopic(DomainModel):
    """One competency a session intends to probe, and how many questions it is worth (§30).

    The plan is what makes coverage a fact rather than a feeling: a session for a technical
    role that never asked a system-design question covered its plan poorly, and readiness
    must be able to say so (§30-31). `label` is a short human topic ("past teamwork",
    "SQL fundamentals"); `question_type` is which kind of question discharges it, so the
    engine knows what to ask and the aggregator knows what an answer counted toward;
    `target_questions` is how many questions the plan wants on this topic before it is
    considered covered.
    """

    label: NonEmptyStr
    question_type: InterviewQuestionType
    target_questions: Annotated[int, Field(ge=1, le=10)] = 1


class InterviewPlan(DomainModel):
    """The set of topics a session sets out to cover, for one mode (§30-31).

    Built once when the session opens — deterministically from the mode and the posting, or
    proposed by a provider and then re-validated here — and then fixed: it is the yardstick
    coverage is measured against, so a plan that changed mid-session would make "how much did
    we cover?" unanswerable. `target_questions` across all topics is the session's intended
    length; the engine may ask fewer (the candidate stops early) or a little more (adaptive
    follow-ups), but the plan is what a follow-up is *in service of*.
    """

    mode: InterviewMode
    topics: Annotated[tuple[InterviewTopic, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _topics_do_not_repeat(self) -> Self:
        labels = [topic.label.casefold() for topic in self.topics]
        if len(labels) != len(set(labels)):
            raise ValueError("an InterviewPlan must not list the same topic twice")
        return self

    @property
    def target_question_count(self) -> int:
        """How many questions the plan intends, summed across its topics."""
        return sum(topic.target_questions for topic in self.topics)

    def topic_types(self) -> frozenset[InterviewQuestionType]:
        """The question types the plan calls for — what "covered" is measured against."""
        return frozenset(topic.question_type for topic in self.topics)


# --- the exchange: questions, answers, evaluations -------------------------------------


class InterviewQuestion(DomainModel):
    """One question the engine asked, at one position in the session (§8-9).

    `sequence` is the question's position in the session, a monotonic counter the engine
    assigns; the id is derived from `(session_id, sequence)`, so re-finalizing a turn writes
    the same row rather than asking twice. `prompt` is the question itself — grounded in the
    posting and the profile but never asserting a candidate fact, which the service enforces
    with the evidence guard before this is persisted (§10). A follow-up carries
    `follows_sequence`, the sequence of the primary question it drills into, so the adaptive
    chain is reconstructable without a self-referential id; `depth` is how many follow-ups
    deep it sits, bounded by `MAX_FOLLOW_UP_DEPTH`. `generator_key` is provenance — which
    strategy produced the text — kept as a plain string for the same reason
    `MatchEvaluation.evaluator_key` is, so a deterministic seed question is told apart from a
    model-authored one when auditing. `llm_run_id` narrows that to the *exact* `LLMRun` that
    produced it (the model, the connection, the prompt version), nullable because a question
    with no model behind it has no run to point at (§27-40).
    """

    id: InterviewQuestionId
    session_id: InterviewSessionId
    user_id: UserId
    sequence: Annotated[int, Field(ge=0)]
    question_type: InterviewQuestionType
    difficulty: InterviewDifficulty
    prompt: NonEmptyStr
    topic_label: NonEmptyStr | None = None
    follows_sequence: Annotated[int, Field(ge=0)] | None = None
    depth: Annotated[int, Field(ge=0, le=MAX_FOLLOW_UP_DEPTH)] = 0
    generator_key: NonEmptyStr | None = None
    llm_run_id: LLMRunId | None = None
    asked_at: UtcDatetime

    @model_validator(mode="after")
    def _follow_up_shape_is_coherent(self) -> Self:
        """A follow-up points back and sits deeper; a primary does neither.

        `depth == 0` is a primary question and must not claim to follow anything; `depth > 0`
        is a follow-up and must name the question it drills into. Enforced here so the
        adaptive chain in the database is always reconstructable, never a dangling pointer.
        """
        if self.depth == 0 and self.follows_sequence is not None:
            raise ValueError("a primary question (depth 0) must not set follows_sequence")
        if self.depth > 0 and self.follows_sequence is None:
            raise ValueError("a follow-up (depth > 0) must name the sequence it follows")
        if self.follows_sequence is not None and self.follows_sequence >= self.sequence:
            raise ValueError("a follow-up must follow an earlier question")
        return self

    @property
    def is_follow_up(self) -> bool:
        """Whether this question drills into an earlier answer rather than opening a topic."""
        return self.depth > 0


class InterviewAnswer(DomainModel):
    """The candidate's one, immutable answer to one question (§13, §52-53).

    One answer per question in Phase 14 — retries are deferred — so the id is derived from
    the question alone and a resubmit lands on the same row. `content` is the answer as text:
    for a `VOICE` answer it is the transcript, because the raw audio is transcribed and then
    discarded (§17), and everything downstream — evaluation, coaching, readiness — reads this
    text and never the audio. `transcript_confidence` is how sure the transcriber was, and it
    exists only for a voice answer: a typed answer has no transcription to be unsure about,
    which the invariant enforces.
    """

    id: InterviewAnswerId
    question_id: InterviewQuestionId
    session_id: InterviewSessionId
    user_id: UserId
    format: InterviewAnswerFormat
    content: NonEmptyStr
    transcript_confidence: Score | None = None
    answered_at: UtcDatetime

    @model_validator(mode="after")
    def _transcript_confidence_only_for_voice(self) -> Self:
        if self.format is InterviewAnswerFormat.TEXT \
                and self.transcript_confidence is not None:
            raise ValueError("a TEXT answer has no transcript_confidence")
        return self


class DimensionEvaluation(DomainModel):
    """One axis of one answer's grade, or an honest "not assessed" (§18-24).

    The `NOT_EVALUATED` ≠ zero rule lives here as an invariant: an `EVALUATED` dimension
    carries a score and a `NOT_EVALUATED` one carries none, and neither can pretend to be the
    other. A provider that grades clarity at 0.0 is saying the answer was unclear; one that
    could not judge clarity records `NOT_EVALUATED` and the aggregator skips it. `notes` are
    the free-text coaching detail behind the grade — plain sentences rather than coded
    `Reason`s, because a coaching note ("led with the result before the situation") is prose a
    candidate reads, not a machine code a dashboard groups by.
    """

    dimension: EvaluationDimension
    status: EvaluationStatus
    score: Score | None = None
    notes: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _score_matches_status(self) -> Self:
        if self.status is EvaluationStatus.EVALUATED and self.score is None:
            raise ValueError("an EVALUATED dimension must carry a score")
        if self.status is EvaluationStatus.NOT_EVALUATED and self.score is not None:
            raise ValueError(
                "a NOT_EVALUATED dimension must not carry a score (it is not a zero)")
        return self


class InterviewAnswerEvaluation(DomainModel):
    """The structured grade of one answer — and, pointedly, nothing about hiring (§19-24, §33).

    This is the model a provider fills, and the one place the "coaching, not prediction" rule
    is enforced by *absence*: there is no readiness field, no probability, no verdict about
    what a recruiter would do, and `extra="forbid"` means a provider that invents one fails to
    parse (§33, §93). Readiness is computed later, by the platform, from a whole session's
    worth of these — never authored here.

    `confidence` is the evaluator's confidence in its *grading*, deliberately distinct from
    any dimension score (§21): a short answer can be graded confidently as thin, and a rich
    one graded uncertainly. `strengths` and `improvements` are coaching prose; a
    `suggested_answer`, when present, is a model-drafted better answer — and the single most
    dangerous field in the phase, because it is where a provider would put words in the
    candidate's mouth. The service runs it through the Phase 10 evidence guard before this is
    persisted, exactly as it guards a generated résumé (§40-44).
    """

    id: InterviewAnswerEvaluationId
    answer_id: InterviewAnswerId
    session_id: InterviewSessionId
    user_id: UserId
    dimensions: Annotated[tuple[DimensionEvaluation, ...], Field(min_length=1)]
    confidence: Score | None = None
    strengths: tuple[NonEmptyStr, ...] = ()
    improvements: tuple[NonEmptyStr, ...] = ()
    suggested_answer: NonEmptyStr | None = None
    evaluator_key: NonEmptyStr | None = None
    llm_run_id: LLMRunId | None = None
    evaluated_at: UtcDatetime

    @model_validator(mode="after")
    def _one_grade_per_dimension(self) -> Self:
        seen = [entry.dimension for entry in self.dimensions]
        if len(seen) != len(set(seen)):
            raise ValueError("dimensions must not repeat an EvaluationDimension")
        return self

    def score_for(self, dimension: EvaluationDimension) -> DimensionEvaluation | None:
        """This answer's grade on one axis, or `None` if the axis was not graded at all."""
        for entry in self.dimensions:
            if entry.dimension is dimension:
                return entry
        return None

    @property
    def has_any_evaluated_dimension(self) -> bool:
        """Whether at least one axis was actually assessed — the answer counts for readiness."""
        return any(entry.status is EvaluationStatus.EVALUATED for entry in self.dimensions)


# --- readiness: aggregated by the platform, never authored by a provider ----------------


class ReadinessDimensionWeight(DomainModel):
    """How much one evaluation axis counts toward readiness in a `ReadinessProfile`.

    Configuration, not a result — the exact analogue of `DimensionWeight` in the match
    domain. A `TECHNICAL` mode weighs `COMPLETENESS` and `STRUCTURE` heavily; a `RECRUITER_HR`
    mode weighs `CLARITY` and `RELEVANCE`. Keeping the weights on a versioned profile is what
    stops five surfaces inventing five different ideas of what "ready" means.
    """

    dimension: EvaluationDimension
    weight: Score


class ReadinessProfile(DomainModel):
    """The versioned, mode-aware recipe readiness is aggregated under (§33-36).

    The heart of "readiness is deterministic and app-derived": given a session's evaluations,
    this profile — not a provider — decides how the per-dimension scores combine and which
    band the result falls in. `version` is stamped onto every `SessionReadiness` so a number
    computed under one set of weights is never silently compared with a newer one, exactly as
    `MatchProfile.version` guards match scores. The bands are coaching labels with strictly
    ordered thresholds; a misconfiguration is a construction error, not a surface that calls
    every practice `EARLY`.
    """

    version: NonEmptyStr
    mode: InterviewMode
    weights: Annotated[tuple[ReadinessDimensionWeight, ...], Field(min_length=1)]
    polished_min: Score = 0.82
    progressing_min: Score = 0.65
    developing_min: Score = 0.45

    @model_validator(mode="after")
    def _weights_and_bands_are_coherent(self) -> Self:
        seen = [entry.dimension for entry in self.weights]
        if len(seen) != len(set(seen)):
            raise ValueError("a ReadinessProfile must not weigh a dimension twice")
        if not (self.polished_min > self.progressing_min > self.developing_min):
            raise ValueError(
                "readiness bands must be strictly ordered: "
                "polished_min > progressing_min > developing_min")
        return self

    def weight_for(self, dimension: EvaluationDimension) -> float:
        """The configured weight of an axis, or 0.0 if this profile ignores it."""
        for entry in self.weights:
            if entry.dimension is dimension:
                return entry.weight
        return 0.0

    def classify(self, overall: float | None) -> ReadinessBand:
        """Which coaching band a computed `overall` falls in.

        `None` — nothing in the session could be evaluated — is `UNKNOWN`, never a low band.
        """
        if overall is None:
            return ReadinessBand.UNKNOWN
        if overall >= self.polished_min:
            return ReadinessBand.POLISHED
        if overall >= self.progressing_min:
            return ReadinessBand.PROGRESSING
        if overall >= self.developing_min:
            return ReadinessBand.DEVELOPING
        return ReadinessBand.EARLY


class ReadinessDimensionSummary(DomainModel):
    """How one axis fared across a whole session — the aggregated view of one dimension.

    `mean_score` is the average of every *evaluated* grade on this axis, or `None` when the
    axis was never evaluated in the session; `evaluated_count` says how many answers backed
    that mean, so a UI can tell "0.9 from one answer" from "0.9 from eight". `weight` is the
    profile weight the axis carried, copied here so the summary records the recipe it was
    aggregated under rather than requiring the reader to re-look-up the profile.
    """

    dimension: EvaluationDimension
    mean_score: Score | None
    evaluated_count: Annotated[int, Field(ge=0)]
    weight: Score

    @model_validator(mode="after")
    def _mean_matches_count(self) -> Self:
        if self.evaluated_count == 0 and self.mean_score is not None:
            raise ValueError("a dimension with no evaluated answers has no mean_score")
        if self.evaluated_count > 0 and self.mean_score is None:
            raise ValueError("a dimension with evaluated answers must carry a mean_score")
        return self


class SessionReadiness(DomainModel):
    """A session's readiness — a coaching signal, computed by the platform (§33-36, §123).

    Not a hiring probability, and the model is built so it cannot be mistaken for one: it is
    produced only by `aggregate_session_readiness` from stored evaluations, it carries the
    `profile_version` that produced it, and its `band` is a practice label. `overall` is the
    weighted mean of the evaluated dimension means, or `None` when nothing in the session was
    evaluable — in which case `band` is `UNKNOWN`, never a low band.

    `coverage` is a second, orthogonal axis (§30-31, §21): how much of the plan the session
    actually exercised, on the unit interval, independent of how well. A session can be highly
    ready on narrow coverage (two questions, both strong) or broadly covered but early — the
    two numbers are reported side by side rather than folded together, because collapsing them
    would hide exactly the thing coaching needs to show.
    """

    overall: Score | None
    band: ReadinessBand
    dimensions: Annotated[tuple[ReadinessDimensionSummary, ...], Field(min_length=1)]
    coverage: Score
    answered_questions: Annotated[int, Field(ge=0)]
    evaluated_answers: Annotated[int, Field(ge=0)]
    profile_version: NonEmptyStr
    computed_at: UtcDatetime

    @model_validator(mode="after")
    def _band_matches_overall(self) -> Self:
        """`UNKNOWN` iff there is no overall — the coaching promise, enforced.

        A readiness with a number must not be `UNKNOWN`, and one with no number must be:
        this is what stops "we could not assess" ever being rendered as a low score, and
        vice versa (§34, mirroring `MatchClassification`).
        """
        if self.overall is None and self.band is not ReadinessBand.UNKNOWN:
            raise ValueError("readiness with no overall score must be UNKNOWN")
        if self.overall is not None and self.band is ReadinessBand.UNKNOWN:
            raise ValueError("readiness with an overall score must not be UNKNOWN")
        return self

    def overall_percent(self) -> int | None:
        """`overall` on the 0-100 presentation scale, or `None` when unknown."""
        if self.overall is None:
            return None
        return int(self.overall * 100 + 0.5)


def _readiness_profile(
        mode: InterviewMode,
        weights: dict[EvaluationDimension, float]) -> ReadinessProfile:
    """Assemble one mode's default profile from a dimension→weight mapping."""
    return ReadinessProfile(
        version="interview-readiness/1.0",
        mode=mode,
        weights=tuple(
            ReadinessDimensionWeight(dimension=dimension, weight=weight)
            for dimension, weight in weights.items()),
    )


# The default readiness recipe per mode. The weights express what "a good answer" means in
# each room — an HR screen rewards clarity and relevance, a behavioural round rewards the
# structure and specificity of a STAR story, a technical round rewards completeness — and
# bumping any of them is a new `version`, so past sessions stay comparable only with their
# own. They are *importance*, not availability: a dimension a session never evaluated drops
# out at aggregation, reported as lower coverage rather than silently reweighted.
DEFAULT_READINESS_PROFILES: dict[InterviewMode, ReadinessProfile] = {
    InterviewMode.RECRUITER_HR: _readiness_profile(InterviewMode.RECRUITER_HR, {
        EvaluationDimension.CLARITY: 0.30,
        EvaluationDimension.RELEVANCE: 0.30,
        EvaluationDimension.COMPLETENESS: 0.20,
        EvaluationDimension.STRUCTURE: 0.10,
        EvaluationDimension.SPECIFICITY: 0.10,
    }),
    InterviewMode.BEHAVIORAL: _readiness_profile(InterviewMode.BEHAVIORAL, {
        EvaluationDimension.CLARITY: 0.15,
        EvaluationDimension.RELEVANCE: 0.20,
        EvaluationDimension.COMPLETENESS: 0.20,
        EvaluationDimension.STRUCTURE: 0.25,
        EvaluationDimension.SPECIFICITY: 0.20,
    }),
    InterviewMode.TECHNICAL: _readiness_profile(InterviewMode.TECHNICAL, {
        EvaluationDimension.CLARITY: 0.15,
        EvaluationDimension.RELEVANCE: 0.20,
        EvaluationDimension.COMPLETENESS: 0.30,
        EvaluationDimension.STRUCTURE: 0.15,
        EvaluationDimension.SPECIFICITY: 0.20,
    }),
    InterviewMode.HIRING_MANAGER: _readiness_profile(InterviewMode.HIRING_MANAGER, {
        EvaluationDimension.CLARITY: 0.20,
        EvaluationDimension.RELEVANCE: 0.25,
        EvaluationDimension.COMPLETENESS: 0.25,
        EvaluationDimension.STRUCTURE: 0.15,
        EvaluationDimension.SPECIFICITY: 0.15,
    }),
    InterviewMode.CASE_STUDY: _readiness_profile(InterviewMode.CASE_STUDY, {
        EvaluationDimension.CLARITY: 0.15,
        EvaluationDimension.RELEVANCE: 0.20,
        EvaluationDimension.COMPLETENESS: 0.25,
        EvaluationDimension.STRUCTURE: 0.25,
        EvaluationDimension.SPECIFICITY: 0.15,
    }),
    InterviewMode.FINAL_INTERVIEW: _readiness_profile(InterviewMode.FINAL_INTERVIEW, {
        EvaluationDimension.CLARITY: 0.20,
        EvaluationDimension.RELEVANCE: 0.25,
        EvaluationDimension.COMPLETENESS: 0.20,
        EvaluationDimension.STRUCTURE: 0.20,
        EvaluationDimension.SPECIFICITY: 0.15,
    }),
}


def readiness_profile_for(mode: InterviewMode) -> ReadinessProfile:
    """The default readiness profile for a mode.

    Total over `DEFAULT_READINESS_PROFILES`: every `InterviewMode` has a profile, so this
    never has to guess, and a mode added to the enum without a profile fails the module's own
    coverage test rather than falling back to a silent default.
    """
    return DEFAULT_READINESS_PROFILES[mode]


def aggregate_session_readiness(
    *,
    evaluations: tuple[InterviewAnswerEvaluation, ...],
    plan: InterviewPlan,
    profile: ReadinessProfile,
    answered_questions: int,
    computed_at: UtcDatetime,
) -> SessionReadiness:
    """Compute a session's readiness deterministically from its evaluations (§33-36).

    The load-bearing function of the whole "coaching, not prediction" promise: readiness is a
    pure, reproducible function of the stored grades and the versioned profile — run it twice
    on the same inputs and it returns the same number, and no provider is anywhere in the
    call. For each dimension the profile weighs, it averages every *evaluated* grade on that
    axis across the session (a `NOT_EVALUATED` grade contributes nothing, never a zero), then
    combines those means by the profile's weights, renormalizing over the dimensions that
    actually have data. When nothing could be evaluated, `overall` is `None` and the band is
    `UNKNOWN` — the honest "we could not assess this practice", never a low score.

    `coverage` is quantity of practice against the plan — how many gradable answers the
    session produced versus the plan's intended length — reported beside readiness rather
    than folded into it (§21, §30-31), so "narrow but strong" and "broad but early" stay
    distinguishable.
    """
    summaries: list[ReadinessDimensionSummary] = []
    weighted_sum = 0.0
    total_weight = 0.0
    for weight_entry in profile.weights:
        dimension = weight_entry.dimension
        graded = [
            entry.score
            for evaluation in evaluations
            if (entry := evaluation.score_for(dimension)) is not None
            and entry.status is EvaluationStatus.EVALUATED
            and entry.score is not None
        ]
        mean = sum(graded) / len(graded) if graded else None
        summaries.append(ReadinessDimensionSummary(
            dimension=dimension,
            mean_score=mean,
            evaluated_count=len(graded),
            weight=weight_entry.weight,
        ))
        if mean is not None:
            weighted_sum += mean * weight_entry.weight
            total_weight += weight_entry.weight

    overall = weighted_sum / total_weight if total_weight > 0.0 else None
    evaluated_answers = sum(
        1 for evaluation in evaluations if evaluation.has_any_evaluated_dimension)
    target = plan.target_question_count
    coverage = min(1.0, evaluated_answers / target) if target > 0 else 0.0
    return SessionReadiness(
        overall=overall,
        band=profile.classify(overall),
        dimensions=tuple(summaries),
        coverage=coverage,
        answered_questions=answered_questions,
        evaluated_answers=evaluated_answers,
        profile_version=profile.version,
        computed_at=computed_at,
    )


# --- the session and its closing summary -----------------------------------------------


class InterviewSession(DomainModel):
    """One adaptive interview-practice session, owned by exactly one account (§1-6).

    User-owned like every Phase 4+ entity: a session is read `WHERE user_id = ?`, so one
    account can neither list nor drive another's. It is anchored to one `opportunity_id` — the
    role being rehearsed, which is what grounds every question — and optionally to the
    `application_id` the practice is *for*, when the candidate is preparing for a posting they
    have actually applied to. `mode` fixes the kind of interview and therefore the plan and
    the readiness profile; `plan.mode` must agree, so a `TECHNICAL` session can never carry a
    behavioural plan. `difficulty` is the session's *current* rung, which the engine adapts as
    it goes (§32); `style` is coaching-vs-realistic and never changes what is true (§80, §123).

    `status` is the lifecycle (§6). `ended_at` is set exactly when the session reaches a
    terminal state and never before — the invariant makes "is this session over?" a fact of
    the data rather than a convention a caller might forget. `plan_llm_run_id` is the exact
    `LLMRun` that produced the coverage plan, nullable for a plan with no model behind it.
    """

    id: InterviewSessionId
    user_id: UserId
    candidate_profile_id: CandidateProfileId
    opportunity_id: OpportunityId
    application_id: ApplicationId | None = None
    mode: InterviewMode
    style: SessionStyle = SessionStyle.COACHING
    difficulty: InterviewDifficulty = InterviewDifficulty.INTERMEDIATE
    status: InterviewSessionStatus = InterviewSessionStatus.CREATED
    language: LanguageCode | None = None
    plan: InterviewPlan
    plan_llm_run_id: LLMRunId | None = None
    title: NonEmptyStr
    created_at: UtcDatetime
    updated_at: UtcDatetime
    ended_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _plan_matches_mode(self) -> Self:
        if self.plan.mode is not self.mode:
            raise ValueError(
                f"session mode {self.mode} and plan mode {self.plan.mode} must agree")
        return self

    @model_validator(mode="after")
    def _ended_at_matches_terminal_status(self) -> Self:
        is_terminal = self.status in TERMINAL_SESSION_STATUSES
        if is_terminal and self.ended_at is None:
            raise ValueError(f"a {self.status.value} session must carry an ended_at")
        if not is_terminal and self.ended_at is not None:
            raise ValueError(
                f"a {self.status.value} session has not ended and must not carry an ended_at")
        return self

    @property
    def is_active(self) -> bool:
        """Whether the session may still ask questions and take answers."""
        return self.status not in TERMINAL_SESSION_STATUSES

    def readiness_profile(self) -> ReadinessProfile:
        """The default readiness recipe this session aggregates under."""
        return readiness_profile_for(self.mode)


class InterviewSessionSummary(DomainModel):
    """The coaching artefact produced when a session completes (§37-39, §73).

    The one summary per session — the id is derived from the session, so completing it twice
    reuses the row. It pairs the deterministic `readiness` with the coaching prose that
    explains it: a `headline` that describes the *practice* ("clear on motivation, thin on
    concrete examples"), never a hiring forecast (§123), plus `strengths` and `focus_areas`.
    All of the prose is run through the Phase 10 evidence guard before this is persisted, so a
    summary can never assert a candidate fact the profile does not support (§40-44).
    `questions_asked` and `answers_evaluated` are the session's shape at a glance, and feed the
    readiness history a candidate watches over repeated practice (§74). `llm_run_id` is the
    exact `LLMRun` behind the prose, null when a deterministic fallback wrote the summary and
    `generator_key` reads `deterministic-summary/1` (§27-40, §68).
    """

    id: InterviewSessionSummaryId
    session_id: InterviewSessionId
    user_id: UserId
    readiness: SessionReadiness
    headline: NonEmptyStr
    strengths: tuple[NonEmptyStr, ...] = ()
    focus_areas: tuple[NonEmptyStr, ...] = ()
    questions_asked: Annotated[int, Field(ge=0)]
    answers_evaluated: Annotated[int, Field(ge=0)]
    generator_key: NonEmptyStr | None = None
    llm_run_id: LLMRunId | None = None
    created_at: UtcDatetime
