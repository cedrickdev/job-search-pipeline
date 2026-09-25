"""`/api/v2/interview-sessions`: the adaptive interview simulator, over HTTP.

The phase's one rule shapes this surface: the simulator is practice and coaching, never
prediction. So the boundary the domain enforces by *absence* is enforced here too — no route
returns a hiring probability, a recruiter verdict or a provider-authored readiness. Readiness
is surfaced only on its own endpoint, computed by the platform from stored grades
(`GET /{id}/readiness`), and an answer's evaluation carries coaching, never a forecast.

The owner is never in the path or the body — it is the account resolved from the session, so
no request can open, drive, read or grade another user's practice (docs/ENGINEERING_STANDARDS.md
§Security). Reads answer 404 for "no such session" and "not yours" alike, so an id cannot be
probed. No request body carries a `user_id`, a `status`, a question's identity or an
evaluation's grade: those are the engine's and the platform's to assign.

The loop is deliberately small and idempotent: `POST /{id}/next-question` returns the current
question (polling never asks twice) or the signal to complete; the candidate answers by text
(`POST /{id}/answers`) or by voice (`POST /{id}/voice-answers`, whose audio is transcribed and
discarded — only the transcript is stored); a strict re-grade lives at
`POST /{id}/questions/{sequence}/evaluate`; and `complete` or `abandon` closes the session.
"""
from fastapi import APIRouter, File, UploadFile, status

from backend.app.api.dependencies import CurrentSession, Interviews, Now
from backend.app.api.schemas import (
    AnswerOutcomeResponse,
    CreateInterviewSessionRequest,
    InterviewAnswerEvaluationResponse,
    InterviewSessionDetailResponse,
    InterviewSessionListResponse,
    InterviewSessionResponse,
    InterviewSessionSummaryListResponse,
    InterviewSessionSummaryResponse,
    InterviewTurnResponse,
    SessionReadinessResponse,
    SubmitTextAnswerRequest,
)
from backend.app.domain.identifiers import InterviewSessionId
from backend.app.interview.transcriber import InterviewAudio

router = APIRouter(tags=["v2-interview"])


# --- opening and listing sessions -------------------------------------------

@router.post("/interview-sessions", response_model=InterviewSessionResponse,
             status_code=status.HTTP_201_CREATED)
async def create_session(body: CreateInterviewSessionRequest, current: CurrentSession,
                         service: Interviews, instant: Now) -> InterviewSessionResponse:
    """Plan and open a practice session against one posting, grounded in the account's profile.

    201, because it creates a resource. The body names the profile and posting to rehearse
    and the `mode`; the session opens `CREATED`, and the platform stamps its plan's mode so
    the session and plan can never disagree. A foreign or missing profile/posting is a 404
    (`interview_grounding_not_found`) that does not say which was missing. An `application_id`,
    when given, must be this account's and rehearse this very posting: a foreign or missing one
    is 404 (`application_not_found`), and one for a different opportunity is 409
    (`application_opportunity_mismatch`). No question is asked yet — the first `next-question`
    starts the exchange.
    """
    session = await service.create_session(
        current.user.id, candidate_profile_id=body.candidate_profile_id,
        opportunity_id=body.opportunity_id, mode=body.mode, now=instant,
        style=body.style, difficulty=body.difficulty, language=body.language,
        application_id=body.application_id, title=body.title)
    return InterviewSessionResponse.of(session)


@router.get("/interview-sessions", response_model=InterviewSessionListResponse)
async def list_sessions(current: CurrentSession,
                        service: Interviews) -> InterviewSessionListResponse:
    """This account's practice sessions, most recently updated first."""
    sessions = await service.list_sessions(current.user.id)
    return InterviewSessionListResponse.of(sessions)


@router.get("/interview-sessions/history",
            response_model=InterviewSessionSummaryListResponse)
async def readiness_history(
        current: CurrentSession,
        service: Interviews) -> InterviewSessionSummaryListResponse:
    """This account's readiness history — the summaries of completed sessions, most recent first.

    Declared before `/{session_id}` so the literal `history` is never parsed as a session id.
    Each summary pairs the platform-computed readiness with the coaching prose that explains
    it, so a candidate can watch progress over repeated practice — never a hiring forecast.
    """
    summaries = await service.readiness_history(current.user.id)
    return InterviewSessionSummaryListResponse.of(summaries)


@router.get("/interview-sessions/{session_id}",
            response_model=InterviewSessionResponse)
async def get_session(session_id: InterviewSessionId, current: CurrentSession,
                      service: Interviews) -> InterviewSessionResponse:
    """One session's configuration and lifecycle, or 404 if it is not this account's."""
    session = await service.get_session(current.user.id, session_id)
    return InterviewSessionResponse.of(session)


@router.get("/interview-sessions/{session_id}/detail",
            response_model=InterviewSessionDetailResponse)
async def session_detail(session_id: InterviewSessionId, current: CurrentSession,
                         service: Interviews) -> InterviewSessionDetailResponse:
    """One session with its whole exchange — questions, answers, evaluations and summary.

    The transcript view: everything the session holds, loaded under one ownership gate. 404
    when the session is not this account's.
    """
    detail = await service.session_detail(current.user.id, session_id)
    return InterviewSessionDetailResponse.of(detail)


@router.get("/interview-sessions/{session_id}/readiness",
            response_model=SessionReadinessResponse)
async def session_readiness(session_id: InterviewSessionId, current: CurrentSession,
                            service: Interviews, instant: Now) -> SessionReadinessResponse:
    """The session's readiness as it stands now — a coaching signal, computed by the platform.

    The same deterministic aggregation `complete` uses, run without ending the session, so a
    UI can show progress mid-practice. No provider is in this call, and the result is never a
    hiring probability: `band` is a practice label, `UNKNOWN` when nothing was evaluable rather
    than a low score. 404 when the session is not this account's.
    """
    readiness = await service.session_readiness(current.user.id, session_id, now=instant)
    return SessionReadinessResponse.of(readiness)


# --- the practice loop: ask, answer, grade ----------------------------------

@router.post("/interview-sessions/{session_id}/next-question",
             response_model=InterviewTurnResponse)
async def next_question(session_id: InterviewSessionId, current: CurrentSession,
                        service: Interviews, instant: Now) -> InterviewTurnResponse:
    """The current question to answer, or the signal the plan is done.

    Idempotent: if the last question is still unanswered it is returned again, so polling never
    asks twice; otherwise the session advances (a `CREATED` session becomes `IN_PROGRESS` on
    its first question) and the engine picks a provider-warranted follow-up or the next
    uncovered topic. When neither yields a question, the turn carries `question=null` and the
    caller completes the session. 404 when the session is not this account's, 409 when it is no
    longer active.
    """
    turn = await service.next_question(current.user.id, session_id, now=instant)
    return InterviewTurnResponse.of(turn)


@router.post("/interview-sessions/{session_id}/answers",
             response_model=AnswerOutcomeResponse,
             status_code=status.HTTP_201_CREATED)
async def submit_text_answer(session_id: InterviewSessionId,
                             body: SubmitTextAnswerRequest, current: CurrentSession,
                             service: Interviews, instant: Now) -> AnswerOutcomeResponse:
    """Record a typed answer to the current question and grade it best-effort.

    201, because it records an answer. The answer is stored and the session advances even when
    coaching could not be produced (`evaluation=null`), so a lost evaluation never costs the
    candidate their turn. 404 when the session is not this account's; 409 for an inactive
    session or an out-of-order answer.
    """
    outcome = await service.submit_text_answer(
        current.user.id, session_id, body.content, now=instant)
    return AnswerOutcomeResponse.of(outcome)


@router.post("/interview-sessions/{session_id}/voice-answers",
             response_model=AnswerOutcomeResponse,
             status_code=status.HTTP_201_CREATED)
async def submit_voice_answer(session_id: InterviewSessionId, current: CurrentSession,
                              service: Interviews, instant: Now,
                              audio: UploadFile = File(...)) -> AnswerOutcomeResponse:
    """Transcribe a spoken answer, then record and grade it exactly like a typed one.

    201, like a text answer. The session is confirmed active before a byte is transcribed, so
    a dead session spends no transcription; the raw audio is transcribed and discarded, and
    only the transcript is stored and graded. An upload larger than the transcriber accepts is
    413, an unsupported media type is 415, and a transcriber that is unavailable or returns an
    empty transcript is 503 — the audio bytes are never echoed back. A transcript whose
    confidence falls below the auto-evaluate threshold is 422 (`transcript_review_required`),
    carrying the transcript for the candidate to review and resubmit as a text answer: nothing
    is recorded and readiness is untouched, so speech-to-text uncertainty never grades an
    answer. The upload's declared content type is passed through as-is for the transcriber to
    validate.
    """
    content = await audio.read()
    payload = InterviewAudio(content=content, content_type=audio.content_type or "")
    outcome = await service.submit_voice_answer(
        current.user.id, session_id, payload, now=instant)
    return AnswerOutcomeResponse.of(outcome)


@router.post("/interview-sessions/{session_id}/questions/{sequence}/evaluate",
             response_model=InterviewAnswerEvaluationResponse)
async def evaluate_answer(session_id: InterviewSessionId, sequence: int,
                          current: CurrentSession, service: Interviews,
                          instant: Now) -> InterviewAnswerEvaluationResponse:
    """(Re)grade one answered question, surfacing failure as an error rather than swallowing it.

    The strict counterpart to the best-effort grading a submit does: where a submit keeps an
    un-gradable answer and reports `evaluation=null`, this is the path to retry coaching for an
    answer that lacked it, and it answers 503 (`evaluation_unavailable`) when the provider
    fails or its coaching does not survive the evidence guard. It does not adapt difficulty, so
    a retry never double-counts. 404 when no question holds that sequence; 409 when the question
    has no answer to grade. The evaluation carries coaching only — never a readiness or verdict.
    """
    evaluation = await service.evaluate_answer(
        current.user.id, session_id, question_sequence=sequence, now=instant)
    return InterviewAnswerEvaluationResponse.of(evaluation)


# --- closing a session ------------------------------------------------------

@router.post("/interview-sessions/{session_id}/complete",
             response_model=InterviewSessionSummaryResponse)
async def complete_session(session_id: InterviewSessionId, current: CurrentSession,
                           service: Interviews,
                           instant: Now) -> InterviewSessionSummaryResponse:
    """Aggregate readiness, write the closing summary, and finalize the session.

    Readiness is computed by the platform from stored grades — never a provider — and paired
    with coaching prose the guard clears; if the prose fails, the session still completes with
    a safe, fact-free headline, and the readiness stands either way. `IN_PROGRESS → COMPLETED`;
    a session not in progress is a 409 (`invalid_status_transition`). 404 when it is not this
    account's.
    """
    summary = await service.complete_session(current.user.id, session_id, now=instant)
    return InterviewSessionSummaryResponse.of(summary)


@router.post("/interview-sessions/{session_id}/abandon",
             response_model=InterviewSessionResponse)
async def abandon_session(session_id: InterviewSessionId, current: CurrentSession,
                          service: Interviews, instant: Now) -> InterviewSessionResponse:
    """Walk away from a session, moving it to the terminal `ABANDONED` state.

    A `CREATED` or `IN_PROGRESS` session becomes `ABANDONED`; no summary is written, because an
    abandoned session has no closing coaching. A session already terminal is a 409
    (`invalid_status_transition`); 404 when it is not this account's.
    """
    session = await service.abandon_session(current.user.id, session_id, now=instant)
    return InterviewSessionResponse.of(session)
