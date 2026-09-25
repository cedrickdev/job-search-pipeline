# Interview Simulator — practice with a memory, never a prediction

> Phase 14. The interview simulator turns V1's static prep sheet into a persistent,
> adaptive practice surface: a signed-in candidate opens a session against a posting, is
> asked questions that adapt to how they answer, earns coaching on each answer, and watches
> a readiness signal accumulate across a session and across sessions. It is the mirror of
> Phase 13's "prose has zero authority", and its one rule makes that word *practice* safe:
> **the simulator coaches, it does not predict.**

## The one rule: coaching, not prediction

Nothing in this phase — no field, no method, no band — is a probability that a real
recruiter will say yes. A model grades one answer on a few coaching axes; the *platform*,
never the model, decides what a session's worth of those grades means, and even then only
as a rehearsal signal. The rule is enforced by construction in four places that must agree:

- the evaluation schema has **no** readiness, probability or verdict field, and forbids
  extras, so a provider that invents one fails to parse (`InterviewAnswerEvaluation` in
  `backend/app/domain/interview.py`; the re-validation in `backend/app/interview/llm.py`);
- readiness is a **pure function** of the stored grades, computed with no provider in the
  call (`aggregate_session_readiness`);
- the API returns readiness only on its own endpoint, and an answer's evaluation carries
  coaching only (`backend/app/api/routes/interview.py`);
- the frontend renders dimensions, strengths and improvements, labels readiness "a coaching
  signal the platform computes — not a hiring forecast", and never shows a probability
  (`frontend/app/pages/interview.vue`, `frontend/app/stores/interview.ts`).

Everything below is an elaboration of that sentence.

## Readiness is aggregation, not authorship

`aggregate_session_readiness` (`backend/app/domain/interview.py`) is the load-bearing
function of the whole promise. Given a session's stored evaluations and its versioned
`ReadinessProfile`, it averages every *evaluated* grade per dimension, combines those means
by the profile's weights (renormalizing over the dimensions that actually have data), and
classifies the result into a coaching `ReadinessBand`. It is pure and reproducible: run it
twice on the same inputs and it returns the same number, and no provider is anywhere in the
call. A `SessionReadiness` carries the `profile_version` that produced it, so a number
computed under one recipe is never silently compared with a newer one — the same discipline
`MatchProfile.version` keeps for match scores.

Two invariants keep it honest:

- **`UNKNOWN` iff there is no overall.** When nothing in the session could be evaluated,
  `overall` is `None` and the band is `UNKNOWN` — the honest "we could not assess this
  practice", never a low score. The domain refuses to build a `SessionReadiness` that
  violates this (`_band_matches_overall`), exactly as `MatchClassification.UNKNOWN` ≠ `WEAK`.
- **Coverage is reported beside readiness, never folded in.** `coverage` is how much of the
  plan the session exercised (gradable answers against the plan's intended length), on the
  unit interval and orthogonal to how *well*. "Narrow but strong" and "broad but early" stay
  distinguishable because collapsing them would hide the thing coaching needs to show.

## `NOT_EVALUATED` ≠ zero

The single most consequential distinction in the evaluation model, and the exact analogue
of `MatchClassification.UNKNOWN` ≠ `WEAK`. A `DimensionEvaluation` is either `EVALUATED`
with a score, or `NOT_EVALUATED` with none — the domain refuses any other shape
(`_score_matches_status`). Grading clarity at `0.0` says "the answer was unclear";
`NOT_EVALUATED` says "we did not assess clarity", and conflating the two is how a warm-up
question quietly tanks a readiness number. The aggregator *omits* a `NOT_EVALUATED` grade
rather than averaging in a zero, and the engine's difficulty adaptation does the same: an
answer nothing could be judged on leaves difficulty untouched, because "we could not assess"
is not "that was weak".

## The engine proposes, the service disposes

The adaptive behaviour lives in a pure engine (`backend/app/interview/engine.py`) that holds
no repository, no provider and no clock. Given the plan, the questions asked so far and the
last evaluation, it answers three deterministic questions — may the session ask again (bounds),
what should the next question be (coverage), how hard (difficulty) — and returns them as small
value objects the service realizes. Keeping it pure is what makes the adaptation testable
without a model.

Its three rules:

- **Bounds.** A session never exceeds `MAX_QUESTIONS_PER_SESSION` (40), and a follow-up chain
  never exceeds `MAX_FOLLOW_UP_DEPTH` (2). A provider stuck drilling one topic is caught here,
  not left to ask forever.
- **Coverage drives primaries.** Primary (depth-0) questions march through the plan's topics
  in order, one topic discharged per its `target_questions`; only primaries count toward
  coverage, so a provider cannot "cover" a plan by drilling one topic with follow-ups. When
  every topic has met its target the engine says the plan is done, and the session can close
  having actually exercised it.
- **Difficulty adapts to the evidence.** After an evaluated answer the engine steps difficulty
  up on a strong mean grade (≥ `STRONG_ANSWER_SCORE`) and down on a weak one
  (≤ `WEAK_ANSWER_SCORE`), along the ordered `_DIFFICULTY_ORDER` ladder and clamped at its
  ends — and holds when nothing could be evaluated.

The crucial division of labour: the engine decides whether a follow-up is *allowed* (depth and
bounds); whether one is *warranted* is the provider's `FollowUpDecision`. So a provider can want
to drill forever and the session still converges on its plan. Every question's identity — its
type, difficulty, sequence, depth and follow-up link — comes from the engine's `QuestionRequest`;
the model contributes only the prompt text.

An adaptive follow-up is *grounded in the grade that was actually stored* (§18-26, §53-54). Before
a follow-up is even considered, the service loads the last answer's persisted
`InterviewAnswerEvaluation` (`get_for_answer`, never recomputed) and hands its structured facts —
the per-axis grades and the improvements, never any hidden reasoning — to `decide_follow_up`, so
the probe drills into a dimension the grades left thin rather than re-deriving a judgement. When
the last answer carries **no** evaluation (grading failed or was skipped), no adaptive follow-up
is asked at all — the session falls through to its next planned topic rather than drilling blind.
The `INTERVIEW_FOLLOW_UP` prompt (version `1.1`) is told to key its decision to that evaluation.

## What the model may never author, and is never given

Safety here is a property of *wiring*, not of the model's good behaviour.

- **It is handed a bounded, user-isolated grounding and nothing else.** `InterviewContext`
  (`backend/app/interview/context.py`) is exactly two facts: the candidate's profile (read
  `user_id`-first, so another account's reads as absent) and the posting being rehearsed. The
  context builder holds only those two repositories, so "the prompt cannot leak a secret" is
  guaranteed by the *type* of what it can read — the same discipline as the chat context builder.
- **The posting is untrusted.** `render_role` fences the posting text and labels it plainly:
  an instruction hidden in a job description is data to be ignored, never a command. The
  evidence guard enforces that regardless of what any model was told.
- **The model never authors readiness.** `evaluate_answer` builds a full
  `InterviewAnswerEvaluation`, whose schema has no readiness field and whose `extra="forbid"`
  rejects one — so a payload carrying a `readiness`, a `probability` or a hiring `verdict`
  fails to parse. Platform-owned keys (the derived id, the answer/session/user, the evaluator
  key, the exact `llm_run_id`, the timestamp) are merged in *after* the model's, so a provider
  cannot forge its own id, backdate a run, or claim a different run than the recorder wrote.
- **Every answer is re-validated.** A provider's claim to have honoured a schema is never taken
  on trust: `InterviewLLM._validate` re-parses every payload against the domain model, and a
  `ValidationError` becomes a typed `STRUCTURED_OUTPUT_INVALID` whose message never quotes the
  offending (untrusted) payload.
- **Privacy is the caller's decision.** The prompt bears the candidate's evidence, so how far
  it may travel is a `RoutingPolicy` the wiring supplies; the adapter hands the request to the
  `LLMRouter`, which enforces that policy first. Audio never routes at all (see below).

## The truth gate: coaching is checked against the candidate's evidence

Every candidate-facing sentence the simulator produces — an evaluation's strengths,
improvements and dimension notes, a drafted `suggested_answer`, a summary's headline and focus
areas — is coaching *about* the candidate, and could, if a provider misbehaved, put a fact in
their mouth their evidence does not support. That is exactly the risk the Phase 10
`CandidateEvidenceGuard` was built to catch, so `InterviewCoachingGuard`
(`backend/app/interview/guard.py`) *reuses* it rather than writing a second, weaker gate:
coaching prose runs through `review_supporting_prose`, applying the citation-free
`INVENTED_NUMBER` and `INVENTED_TERM` gates against the candidate's whole evidence corpus with
the same tokenizer and term universe the résumé gates use. The guard is pure and deterministic,
so the service runs it *before* persisting anything.

A **generated question** is guarded too, but by a different standard, because a question is not
coaching *about* the candidate — it is grounded in the posting, and may legitimately name a
skill or number the posting states ("the posting mentions Kafka; how would you approach it?").
So `review_question` widens the allowed pool with the posting's own words (`extra_corpus` on
`review_supporting_prose`): a term or number grounded in *either* the candidate or the posting
passes, and one grounded in neither — invented from nowhere — is caught. The gate deliberately
does *not* distinguish "the posting mentions Kafka" from "you have Kafka experience"; that
attribution is the question prompt's defence in depth (the `INTERVIEW_QUESTION` prompt, at
version `1.1`, tells the model to reference the posting openly but never assert candidate
experience the context does not support), while this deterministic gate stays authoritative for
the invented-from-nowhere case. A question that fails the guard earns **one bounded repair** —
regenerate, telling the model not to repeat the rejected prompt — after which a still-ungrounded
prompt is refused `QUESTION_GROUNDING_FAILED` and never persisted (§10-17).

## Generate, then guard, then persist — and degrade, never strand

The service (`backend/app/interview/service.py`) makes three orderings physical:

- **Generate → guard → persist.** A generated *question*, an evaluation's coaching and a
  summary's prose are all guarded before they are stored. A rejected question is regenerated
  once and then refused `QUESTION_GROUNDING_FAILED` (never a fabricated prompt persisted); a
  rejected evaluation is not stored at all (the answer stands, ungraded); a rejected summary
  falls back to safe, deterministic, fact-free prose — never a fabricated strength persisted as
  coaching.
- **A provider hiccup never strands a session.** A failed follow-up *decision* falls through to
  the next planned question; a failed *evaluation* on submit leaves the answer stored but
  un-graded (`evaluation=None`), so a lost grade never costs the candidate their turn; a failed
  *summary* completes the session with deterministic prose. Only a failed *question generation*
  or *plan* — where there is nothing to return — surfaces as a typed `InterviewError`.
- **The owner comes from the session, never the body.** Every method takes `user_id` and hands
  it to owner-scoped repositories, so another account's session reads as absent
  (`SESSION_NOT_FOUND`) rather than "forbidden" — the same non-leaking discipline the chat
  executor uses.

The strict counterpart to best-effort grading is `evaluate_answer` (the
`.../questions/{sequence}/evaluate` route): it *raises* `EVALUATION_UNAVAILABLE` when the
provider fails or the guard rejects the coaching, and it does not adapt difficulty, so a retry
never double-counts.

## A session's application must be the account's, and about the posting

A session may name an `application_id` to rehearse — but only its own, and only for the very
posting it is about. `create_session` calls `_verify_application` (§2-9, §62-63): with no
`application_id` it short-circuits; with one, it resolves it *owner-first* through the Phase 12
`ApplicationRepository`, so a missing application and one belonging to another account are
indistinguishable — both `APPLICATION_NOT_FOUND` (a `404`), leaking nothing about whether the id
exists elsewhere. A resolved application whose `opportunity_id` differs from the session's is
`APPLICATION_OPPORTUNITY_MISMATCH` (a `409`), so an application for one role can never be
borrowed to coach a session about another. The check runs *before* the session exists, reusing
the Phase 12 ownership read rather than adding a second authorization path.

## Concurrency: derived ids make a race collide on the primary key

Two of a candidate's own clients can race the same turn, and the read-then-check both would pass
cannot close the window. The natural key does: an answer's id derives from its question and a
question's id from `(session_id, sequence)`, so the loser of a race inserts a row whose primary
key already exists and its flush raises an `IntegrityError` — caught and re-raised as a typed
refusal, never surfaced raw and never overwriting the winner (§41-43):

- **Two answers to the same current question** — the loser is `QUESTION_ALREADY_ANSWERED`, and
  the winner's immutable answer stands; exactly one answer row exists.
- **Two `next_question` calls advancing one session** — the loser is `INTERVIEW_STATE_CONFLICT`,
  and exactly one new question at the next sequence is recorded.

`tests/test_v2_persistence_interview_engine.py` proves both against a real PostgreSQL database
under real concurrency — the only place a cross-connection race is demonstrable — exactly as the
Phase 12 submission-budget race is proven.

## Exact provenance: which run produced each artefact

`generator_key`/`evaluator_key` already recorded *which strategy* produced an artefact
(`llm-backed/1` vs a deterministic seed). The corrective narrows that to *which exact run* did
(§27-40): a nullable `llm_run_id` on the question, the evaluation and the summary, and a
`plan_llm_run_id` on the session, each pointing at `llm_runs.id` — the model, the connection,
the prompt version, keyed to the same call — so an audit traces a generated question back to the
routed request behind it. The id is not looked up after the fact (a "latest run for this user"
query another concurrent call could win); it rides back on the routing outcome. The recorder
mints the run id, writes the `LLMRun`, and sets it on the `RoutingOutcome`; the adapter returns
each artefact wrapped in an `InterviewLLMResult[T]` (value + `llm_run_id`); the service stamps it
onto what it persists. It is honestly `None` where no run stands behind the artefact: a
deterministic fallback summary records `llm_run_id=None` under the `deterministic-summary/1` key,
so an audit tells model prose from platform prose without guessing. Every FK is
`ON DELETE SET NULL`, never `CASCADE` — pruning telemetry must never delete a candidate's
practice history as a side effect.

## Voice answers: transcribe, then discard

A voice answer is not a new kind of answer — it is a text answer the candidate happened to
speak (`backend/app/interview/transcriber.py`). The `SpeechTranscriber` protocol has two
implementations mirroring the LLM seam: `DeterministicTranscriber` for tests (canned or
hash-derived transcripts, still running the size/type gates) and `WhisperCppTranscriber`, which
wraps V1's local whisper.cpp in a worker thread and maps V1's failures onto the domain's typed
codes. **Audio never leaves the machine** — transcription is a local subprocess, never a routed
LLM call — and **the raw audio is discarded** the moment the transcript is produced; only the
text is stored and graded. The session is confirmed active *before* a byte is transcribed, an
upload is bounded (`MAX_AUDIO_BYTES`, 25 MiB) and type-checked (`ALLOWED_AUDIO_TYPES`) before
any work, and an empty transcript is `TRANSCRIPTION_UNAVAILABLE`. whisper.cpp reports no
confidence, so a voice answer's `transcript_confidence` is nullable and recorded honestly.

When a transcriber *does* report a confidence and it falls below
`MIN_AUTO_EVALUATE_TRANSCRIPT_CONFIDENCE` (`0.5`), the service refuses to auto-grade words it
is not sure the candidate said (§55-61): it raises `TranscriptReviewRequired`, which the API
returns as a `422` (`transcript_review_required`) carrying the low-confidence transcript back so
the candidate can correct it and resubmit as text — the answer is neither stored nor graded on a
transcript the machine doubts. A `None` confidence (whisper.cpp's usual case) does not trip the
hold; only a reported confidence below the floor does.

## The turn pipeline

A turn is two calls, each with a fixed order the service (`backend/app/interview/service.py`)
enforces:

- **Ask (`next_question`).** Confirm the session is active; if the last question is still
  unanswered, return it again (idempotent — polling never asks twice). Otherwise a `CREATED`
  session advances to `IN_PROGRESS`, the engine picks the next request (an allowed *and*
  provider-warranted follow-up on the last answer, else the next uncovered plan topic), and the
  model writes only that request's prompt text. When the engine returns nothing — the plan is
  covered or the bound is reached — the turn carries `question=None` and the caller completes.
- **Answer (`submit_text_answer` / `submit_voice_answer`).** Confirm active (for voice, *before*
  a byte is transcribed); store the answer *first*, so a lost grade never costs the candidate
  their turn; then grade best-effort — generate → guard → persist — and, only if a grade
  survived, adapt the session's difficulty for the next question. A voice answer transcribes and
  discards the audio before this same path runs.

Readiness is never part of an answer's response. The frontend refreshes it from its own endpoint
(`GET /{session_id}/readiness`) after each grade, so the number a candidate sees is always the
platform's live aggregation of the stored grades, not something a submit or summary carried.

## Session lifecycle and statuses

A session is a closed state machine (`InterviewSessionStatus`, `can_transition_session`):

- `CREATED` — planned but not started. The first `next_question` moves it to `IN_PROGRESS`.
- `IN_PROGRESS` — where every question, answer and evaluation happens.
- `COMPLETED` — `complete_session` aggregated readiness and wrote the closing summary.
- `ABANDONED` — the candidate walked away; no summary is written.

`_ALLOWED_STATUS_TRANSITIONS` is the whole machine: `CREATED → {IN_PROGRESS, ABANDONED}`,
`IN_PROGRESS → {COMPLETED, ABANDONED}`, and both terminal states map to the empty set — nothing
follows them, so a completed session's history is immutable and an abandoned one is restarted,
never resumed. A move not in the table raises `INVALID_STATUS_TRANSITION`. The domain refuses to
build a session whose `ended_at` disagrees with its status (`_ended_at_matches_terminal_status`):
a terminal session has an `ended_at`, a live one does not.

## Closed vocabularies

Every axis of the simulator is a closed enum in `backend/app/domain/interview.py`, never a free
label a caller invents — the same discipline that keeps eligibility out of `MatchDimension`.

**Interview modes** (`InterviewMode`) — the round a session rehearses, which decides the plan's
competencies, how the engine phrases prompts, and which readiness profile aggregates the answers:
`RECRUITER_HR`, `BEHAVIORAL`, `TECHNICAL`, `HIRING_MANAGER`, `CASE_STUDY`, `FINAL_INTERVIEW`.

**Question types** (`InterviewQuestionType`) — orthogonal to mode; a technical loop opens with a
`BACKGROUND` warm-up and closes with `CANDIDATE_QUESTIONS`: `BACKGROUND`, `MOTIVATION`,
`BEHAVIORAL`, `SITUATIONAL`, `TECHNICAL`, `CASE`, `ROLE_KNOWLEDGE`, `CANDIDATE_QUESTIONS`.

**Session style** (`SessionStyle`) — `COACHING` (default, generous with teaching follow-ups) and
`REALISTIC` (terser); both run the same evidence guard and the same deterministic readiness.

**Difficulty** (`InterviewDifficulty`, ordered by `_DIFFICULTY_ORDER`) — `INTRODUCTORY <
INTERMEDIATE < ADVANCED`; `adapt_difficulty` steps along the ladder and clamps at its ends.

**Evaluation dimensions** (`EvaluationDimension`) — `CLARITY`, `RELEVANCE`, `COMPLETENESS`,
`STRUCTURE`, `SPECIFICITY`. All five grade *compatibility with good answering*, never a verdict.

**Evaluation status** (`EvaluationStatus`) — `EVALUATED` (carries a score) or `NOT_EVALUATED`
(carries none); `NOT_EVALUATED` ≠ a score of zero.

**Readiness bands** (`ReadinessBand`) — `UNKNOWN`, `EARLY`, `DEVELOPING`, `PROGRESSING`,
`POLISHED`. `UNKNOWN` iff nothing could be evaluated; the thresholds live on `ReadinessProfile`
(`developing_min 0.45`, `progressing_min 0.65`, `polished_min 0.82`), so no surface hard-codes them.

**Error codes** (`InterviewErrorCode`) — the stable refusal vocabulary the service raises and the
API maps to status: `SESSION_NOT_FOUND`, `SESSION_NOT_ACTIVE`, `INVALID_STATUS_TRANSITION`,
`QUESTION_NOT_FOUND`, `NO_CURRENT_QUESTION`, `QUESTION_ALREADY_ANSWERED`, `ANSWER_OUT_OF_ORDER`,
`INTERVIEW_STATE_CONFLICT`, `SESSION_LIMIT_REACHED`, `EVALUATION_UNAVAILABLE`,
`QUESTION_GENERATION_UNAVAILABLE`, `QUESTION_GROUNDING_FAILED`, `APPLICATION_NOT_FOUND`,
`APPLICATION_OPPORTUNITY_MISMATCH`, `TRANSCRIPT_REVIEW_REQUIRED`, `TRANSCRIPTION_UNAVAILABLE`,
`AUDIO_TOO_LARGE`, `UNSUPPORTED_AUDIO`.

## Persisted entities

Migration `0012` (`backend/migrations/versions/rev_0012_phase_14_interview.py`, down-revision
`0011`) creates five tables, each owner-scoped and mirroring a domain model through the
SQLAlchemy mappers — the domain never imports the ORM:

- **`interview_sessions`** — one row per practice session: its mode, style, difficulty, plan
  (as JSON), status, `ended_at`, and the opportunity/application it rehearses.
- **`interview_questions`** — every question asked, keyed by `(session_id, sequence)`, carrying
  type, difficulty, depth, `follows_sequence`, prompt and `generator_key`.
- **`interview_answers`** — one answer per question (one answer per question in Phase 14): its
  format, content, and nullable `transcript_confidence`.
- **`interview_answer_evaluations`** — the coaching for one answer: per-dimension grades (as
  JSON), strengths, improvements, `suggested_answer`, `confidence` and `evaluator_key`.
- **`interview_session_summaries`** — the closing summary for a completed session: the aggregated
  readiness (as JSON), headline and focus areas, and the summary's `generator_key`.

CHECK constraints and indexes enforce at the row level what the domain enforces in memory (status
in its enum, one answer per question, the plan/mode agreement), so a bad write is refused by the
database even if it reached one.

Migration `0013` (`backend/migrations/versions/rev_0013_phase_14_llm_run_provenance.py`,
down-revision `0012`) adds the exact-provenance links: `interview_sessions.plan_llm_run_id`,
`interview_questions.llm_run_id`, `interview_answer_evaluations.llm_run_id` and
`interview_session_summaries.llm_run_id`, each a nullable FK to `llm_runs.id` with
`ON DELETE SET NULL`. Nullable because a deterministic artefact stands behind no run;
`SET NULL` because pruning telemetry must never cascade into a candidate's practice history.

## HTTP surface (`/api/v2/interview-sessions`)

Every route is owner-scoped: the owner comes from the authenticated session, never the path or
body, so another account's session reads as `404` rather than `403`. Routes are defined in
`backend/app/api/routes/interview.py`.

| Method & path | Purpose |
| --- | --- |
| `POST /interview-sessions` | Open a session against an opportunity (`201`) |
| `GET /interview-sessions` | List the account's sessions |
| `GET /interview-sessions/history` | The readiness trend across sessions |
| `GET /interview-sessions/{id}` | One session |
| `GET /interview-sessions/{id}/detail` | Session with its questions, answers, evaluations, summary |
| `GET /interview-sessions/{id}/readiness` | The live, platform-computed readiness |
| `POST /interview-sessions/{id}/next-question` | Advance to the current/next question |
| `POST /interview-sessions/{id}/answers` | Submit a typed answer (`201`) |
| `POST /interview-sessions/{id}/voice-answers` | Submit a spoken answer (`audio` upload, `201`) |
| `POST /interview-sessions/{id}/questions/{sequence}/evaluate` | Strictly (re)grade one answer |
| `POST /interview-sessions/{id}/complete` | Aggregate readiness and close the session |
| `POST /interview-sessions/{id}/abandon` | Walk away from the session |

`/history` is declared before `/{session_id}` so the literal path is not captured as an id.

## Error codes → status

`_INTERVIEW_STATUS` (`backend/app/api/errors.py`) maps each typed code to a status; anything
unlisted is a `409 Conflict`, the code carried in the `error` field for the client to switch on:

| Status | Codes |
| --- | --- |
| `404 Not Found` | `SESSION_NOT_FOUND`, `QUESTION_NOT_FOUND`, `APPLICATION_NOT_FOUND`, plus `interview_grounding_not_found` |
| `409 Conflict` | `SESSION_NOT_ACTIVE`, `INVALID_STATUS_TRANSITION`, `NO_CURRENT_QUESTION`, `QUESTION_ALREADY_ANSWERED`, `ANSWER_OUT_OF_ORDER`, `INTERVIEW_STATE_CONFLICT`, `APPLICATION_OPPORTUNITY_MISMATCH`, `SESSION_LIMIT_REACHED` |
| `413 Content Too Large` | `AUDIO_TOO_LARGE` |
| `415 Unsupported Media Type` | `UNSUPPORTED_AUDIO` |
| `422 Unprocessable Content` | `TRANSCRIPT_REVIEW_REQUIRED` (returned with the held transcript and its confidence) |
| `503 Service Unavailable` | `EVALUATION_UNAVAILABLE`, `QUESTION_GENERATION_UNAVAILABLE`, `QUESTION_GROUNDING_FAILED`, `TRANSCRIPTION_UNAVAILABLE` |

`TRANSCRIPT_REVIEW_REQUIRED` is raised as its own `TranscriptReviewRequired` exception, not an
`InterviewError`: unlike every other refusal it *returns* its detail — the candidate's held
transcript — because that transcript is the whole point of the response, not a secret to strip.

`interview_grounding_not_found` answers one sentence that does not say which of the profile or the
posting was missing, so a caller cannot probe for the existence of either.

## Frontend

The screen is a Pinia store and one page. `frontend/app/stores/interview.ts` holds the session
list, the open session's detail, its current question, the last answer/evaluation, the readiness
and the summary, and drives the loop (`create`, `select`, `nextQuestion`, `submitText`,
`submitVoice`, `evaluate`, `complete`, `abandon`). It refreshes readiness from its own endpoint
after every grade — it never reads a readiness off a submit or a summary the model wrote.
`frontend/app/pages/interview.vue` opens the most recent session on arrival, builds the
opportunity picker from the account's applications, renders dimensions, strengths and
improvements, and labels readiness "a coaching signal the platform computes — not a hiring
forecast". No probability, verdict or likelihood is ever rendered — the phase's rule, checked
where a person would see it broken.

## Testing

The suite proves the phase's rule from the pure core outward, on fixtures and fakes — no live
board, LLM or transcriber (CLAUDE.md §Testing):

- **`tests/test_v2_interview_engine.py`** — the pure engine: capacity, follow-up allowance,
  coverage marching the plan, difficulty stepping and clamping, holding on `NOT_EVALUATED`.
- **`tests/test_v2_interview_service.py`** — the service on in-memory repositories and a fake
  LLM: the turn orderings, best-effort grading, the degrade paths, owner isolation, and readiness
  aggregated by the domain rather than a provider.
- **`tests/test_v2_interview_context.py`** — the grounding is two owner-isolated facts and the
  posting is fenced as untrusted.
- **`tests/test_v2_interview_guard.py`** — coaching prose that invents a number or term is caught
  by the reused evidence guard.
- **`tests/test_v2_interview_llm.py`** — the adapter on a real router and a fake provider:
  structured output preferred, every payload re-validated, a bad one → `STRUCTURED_OUTPUT_INVALID`
  without quoting it, platform keys merged after the model's.
- **`tests/test_v2_interview_transcriber.py`** — the size/type gates, transcribe-then-discard, and
  V1's failures mapped to typed codes.
- **`tests/test_v2_api_interview.py`** — the HTTP surface: status mapping, owner-as-`404`, the
  `201`s, and that no response carries a readiness the model authored.
- **`tests/test_v2_persistence_interview_engine.py`** — the two turn races against a real
  PostgreSQL database: two answers to one question store one and refuse the other
  `QUESTION_ALREADY_ANSWERED`, two `next_question` calls ask one and conflict the other
  `INTERVIEW_STATE_CONFLICT` (a barrier inside the fake LLM forces the derived-id collision), and
  each leaves exactly one row — the property only a cross-connection race can show.
- **`frontend/tests/nuxt/pages/interview.spec.ts`**, **`.../stores/interview.spec.ts`** and
  **`frontend/tests/e2e/interview.spec.ts`** — the page opens the latest session, grades an answer
  into coaching in place, shows readiness as a signal, and renders no probability or verdict.

## Module map

- `backend/app/domain/interview.py` — the pure domain: enums, models, `aggregate_session_readiness`, `adapt_difficulty`, the readiness profiles.
- `backend/app/interview/engine.py` — the pure adaptive engine (bounds, coverage, difficulty).
- `backend/app/interview/context.py` — the bounded, owner-isolated grounding builder.
- `backend/app/interview/llm.py` — the provider-neutral adapter for the five interview tasks.
- `backend/app/interview/guard.py` — the coaching truth gate over the Phase 10 evidence guard.
- `backend/app/interview/transcriber.py` — voice → text, then discard; deterministic and whisper.cpp.
- `backend/app/interview/service.py` — the one application service that orchestrates the pure pieces.
- `backend/app/api/routes/interview.py` — the `/api/v2/interview-sessions` HTTP surface.
- `backend/migrations/versions/rev_0012_phase_14_interview.py` — the five tables.
- `backend/migrations/versions/rev_0013_phase_14_llm_run_provenance.py` — the four `SET NULL` `llm_run_id`/`plan_llm_run_id` provenance links.
- `frontend/app/stores/interview.ts`, `frontend/app/pages/interview.vue` — the practice screen.






