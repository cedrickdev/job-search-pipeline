# tests/test_v2_api_interview.py
"""`/api/v2/interview-sessions` over HTTP : le simulateur d'entretien vu par un navigateur.

Les tests de service (`test_v2_interview_service.py`) tranchent les décisions ; celui-ci tient
la couche route et son câblage — ce qui franchit réellement le fil. La règle de la phase se lit
dans chaque cas : le simulateur est un entraînement, jamais une prédiction, si bien qu'aucune
route ne renvoie de probabilité d'embauche, de verdict de recruteur ni de readiness écrite par
un provider. La readiness n'est publiée que sur son propre endpoint, calculée par la plateforme,
et l'évaluation d'une réponse ne porte que du coaching.

Le propriétaire n'est jamais dans le chemin ni dans le corps — c'est le compte résolu depuis la
session — donc une séance qui n'est pas celle du compte se lit *absente* (404), jamais
« interdite ». Le LLM et le transcripteur sont des doublures injectées à leur propre couture
(`interview_llm`, `interview_transcriber`) : ce qu'on teste ici est le comportement de l'API,
pas celui d'un provider.
"""
import pytest

from backend.app.api.dependencies import CSRF_HEADER
from backend.app.domain.identifiers import default_candidate_profile_id
from backend.app.domain.interview import InterviewErrorCode
from backend.app.interview.transcriber import (
    DeterministicTranscriber,
    InterviewAudio,
    TranscriptionError,
    TranscriptResult,
)
from backend.app.llm.failures import LLMError, LLMFailureCode
from tests.v2_api import api_harness
from tests.v2_builders import OPPORTUNITY, a_candidate_profile, an_opportunity
from tests.v2_interview import FakeInterviewLLM

pytestmark = pytest.mark.asyncio


def _invalid_output() -> LLMError:
    """The typed failure a provider raises when its structured output cannot be trusted."""
    return LLMError(LLMFailureCode.STRUCTURED_OUTPUT_INVALID, detail="unusable output")


class _FailingTranscriber:
    """A transcriber that always raises one typed failure — for the 413 and 503 mappings."""

    def __init__(self, error: TranscriptionError) -> None:
        self._error = error

    async def transcribe(self, audio: InterviewAudio) -> TranscriptResult:
        raise self._error


async def _ground(api):
    """Seed the account's own profile and a shared posting — the grounding a session needs.

    The service grounds a session by reading the profile owner-first and the posting by id, so
    a session opens over exactly the account's own facts, the way a real prompt would.
    """
    user_id = next(iter(api.users.users))
    profile_id = default_candidate_profile_id(user_id)
    await api.profiles.upsert(a_candidate_profile(id=profile_id, user_id=user_id))
    await api.postings.upsert(an_opportunity(id=OPPORTUNITY))
    return profile_id


async def _create(api, *, profile_id, opportunity_id=OPPORTUNITY, mode="BEHAVIORAL"):
    """POST a create-session body; the response is returned unasserted for a status check."""
    return await api.write("POST", "/interview-sessions", json={
        "candidate_profile_id": str(profile_id),
        "opportunity_id": str(opportunity_id), "mode": mode})


async def _open(api):
    """Sign in, ground, and open a CREATED session — the state most cases start from."""
    await api.sign_in()
    profile_id = await _ground(api)
    response = await _create(api, profile_id=profile_id)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _ask(api, session_id):
    """Advance the session to its first question, moving it CREATED → IN_PROGRESS."""
    response = await api.write("POST", f"/interview-sessions/{session_id}/next-question")
    assert response.status_code == 200, response.text
    return response.json()


async def _voice(api, session_id, *, content, content_type):
    """POST a spoken answer as multipart, carrying the CSRF header a write needs."""
    headers = {} if api.csrf_token is None else {CSRF_HEADER: api.csrf_token}
    return await api.client.request(
        "POST", api.url(f"/interview-sessions/{session_id}/voice-answers"),
        files={"audio": ("clip", content, content_type)}, headers=headers)


def _carries_no_forecast(payload, *fields):
    """No hiring forecast leaked through this payload — the phase's boundary, at the wire."""
    for field in fields:
        assert field not in payload, f"{field!r} leaked into {sorted(payload)}"


# --- opening a session: grounding, ownership, provider failure --------------

async def test_creating_a_session_returns_201_created_and_planned(tmp_path):
    """A create is a 201: the session opens `CREATED`, planned for its mode, owner-free."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        profile_id = await _ground(api)
        response = await _create(api, profile_id=profile_id)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "CREATED"
        assert body["is_active"] is True
        assert body["mode"] == "BEHAVIORAL"
        assert body["plan"]["mode"] == "BEHAVIORAL"
        assert "user_id" not in body  # the owner is the caller, never surfaced


async def test_creating_a_session_without_grounding_is_404(tmp_path):
    """No profile and posting to ground on is a 404 that does not say which was missing."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = next(iter(api.users.users))
        response = await _create(api, profile_id=default_candidate_profile_id(user_id))
        assert response.status_code == 404
        assert response.json()["error"] == "interview_grounding_not_found"


async def test_a_foreign_session_reads_as_404_not_found(tmp_path):
    """A session opened by one account reads as absent for another — never « forbidden »."""
    async with api_harness(tmp_path) as api:
        session_id = await _open(api)
        await api.register(email="someone.else@example.com")  # switches to a second account
        response = await api.read(f"/interview-sessions/{session_id}")
        assert response.status_code == 404
        assert response.json()["error"] == "session_not_found"


async def test_the_list_returns_only_this_accounts_sessions(tmp_path):
    """One account's practice never enters another's list — ownership scopes the read."""
    async with api_harness(tmp_path) as api:
        await _open(api)
        await api.register(email="someone.else@example.com")
        response = await api.read("/interview-sessions")
        assert response.status_code == 200
        assert response.json()["sessions"] == []


async def test_a_failed_plan_generation_is_503(tmp_path):
    """A provider that cannot plan is a hard, typed 503 — no session is opened without a plan."""
    async with api_harness(
            tmp_path, interview_llm=FakeInterviewLLM(plan_error=_invalid_output())) as api:
        await api.sign_in()
        profile_id = await _ground(api)
        response = await _create(api, profile_id=profile_id)
        assert response.status_code == 503
        assert response.json()["error"] == "question_generation_unavailable"


# --- listing, history ordering, and the ask/answer loop ---------------------

async def test_history_resolves_before_the_session_id_route(tmp_path):
    """`/history` is a literal, declared before `/{session_id}`, never parsed as an id.

    Were the order wrong, `history` would reach the `{session_id}` slot and fail UUID parsing
    with a 422; a 200 carrying a `summaries` list is the proof the specific route wins.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        response = await api.read("/interview-sessions/history")
        assert response.status_code == 200, response.text
        assert response.json()["summaries"] == []


async def test_next_question_asks_the_first_and_starts_the_session(tmp_path):
    """The first `next-question` discharges the plan's first topic and starts the session."""
    async with api_harness(tmp_path) as api:
        session_id = await _open(api)
        turn = await _ask(api, session_id)
        assert turn["question"] is not None
        assert turn["question"]["sequence"] == 0
        assert turn["question"]["depth"] == 0
        assert turn["question"]["question_type"] == "BEHAVIORAL"
        assert turn["session"]["status"] == "IN_PROGRESS"


async def test_next_question_is_idempotent_until_the_answer(tmp_path):
    """Polling before answering returns the same question — the loop never asks twice."""
    async with api_harness(tmp_path) as api:
        session_id = await _open(api)
        first = await _ask(api, session_id)
        again = await _ask(api, session_id)
        assert again["question"]["id"] == first["question"]["id"]


async def test_a_text_answer_is_graded_and_leaks_no_forecast(tmp_path):
    """A typed answer is recorded and graded, and its coaching carries no hiring forecast."""
    async with api_harness(tmp_path) as api:
        session_id = await _open(api)
        await _ask(api, session_id)
        response = await api.write(
            "POST", f"/interview-sessions/{session_id}/answers",
            json={"content": "Une réponse solide et structurée."})
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["answer"]["format"] == "TEXT"
        assert body["evaluation"] is not None
        _carries_no_forecast(body["evaluation"], "readiness", "probability", "verdict")


async def test_answering_with_no_question_asked_is_409(tmp_path):
    """A submit before any question was asked has nothing to answer — a 409."""
    async with api_harness(tmp_path) as api:
        session_id = await _open(api)
        response = await api.write(
            "POST", f"/interview-sessions/{session_id}/answers", json={"content": "Trop tôt."})
        assert response.status_code == 409
        assert response.json()["error"] == "no_current_question"


async def test_a_strict_regrade_failure_is_503(tmp_path):
    """A submit degrades quietly (evaluation null); the strict re-grade surfaces the 503."""
    async with api_harness(
            tmp_path,
            interview_llm=FakeInterviewLLM(evaluation_error=_invalid_output())) as api:
        session_id = await _open(api)
        await _ask(api, session_id)
        submit = await api.write(
            "POST", f"/interview-sessions/{session_id}/answers", json={"content": "Réponse."})
        assert submit.status_code == 201, submit.text
        assert submit.json()["evaluation"] is None  # the submit degraded, not failed
        regrade = await api.write(
            "POST", f"/interview-sessions/{session_id}/questions/0/evaluate")
        assert regrade.status_code == 503
        assert regrade.json()["error"] == "evaluation_unavailable"


# --- voice: transcribe over multipart, then map every refusal to its status ---

async def test_a_voice_answer_transcribes_records_and_grades(tmp_path):
    """A spoken answer becomes text, recorded as `VOICE` with its transcript confidence."""
    transcriber = DeterministicTranscriber(
        transcripts={b"a-clip": "Voici ma réponse orale."}, confidence=0.8)
    async with api_harness(tmp_path, interview_transcriber=transcriber) as api:
        session_id = await _open(api)
        await _ask(api, session_id)
        response = await _voice(
            api, session_id, content=b"a-clip", content_type="audio/webm")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["answer"]["format"] == "VOICE"
        assert body["answer"]["content"] == "Voici ma réponse orale."
        assert body["answer"]["transcript_confidence"] == 0.8
        assert body["evaluation"] is not None


async def test_a_voice_answer_of_an_unsupported_type_is_415_and_records_nothing(tmp_path):
    """An unsupported media type is refused before any answer is recorded — a 415."""
    async with api_harness(tmp_path) as api:
        session_id = await _open(api)
        response = await _voice(
            api, session_id, content=b"anything", content_type="audio/flac")
        assert response.status_code == 415
        assert response.json()["error"] == "unsupported_audio"
        assert api.interview_answers.answers == {}  # nothing was written


async def test_an_oversized_voice_answer_is_413(tmp_path):
    """A clip the transcriber rejects as too large maps to 413, not a generic error."""
    transcriber = _FailingTranscriber(TranscriptionError(
        InterviewErrorCode.AUDIO_TOO_LARGE, "audio exceeds the limit"))
    async with api_harness(tmp_path, interview_transcriber=transcriber) as api:
        session_id = await _open(api)
        response = await _voice(
            api, session_id, content=b"too-big", content_type="audio/webm")
        assert response.status_code == 413
        assert response.json()["error"] == "audio_too_large"


async def test_an_empty_transcript_is_503(tmp_path):
    """A transcriber that returns nothing usable is a 503, never a silently empty answer."""
    transcriber = DeterministicTranscriber(transcripts={b"silence": ""})
    async with api_harness(tmp_path, interview_transcriber=transcriber) as api:
        session_id = await _open(api)
        response = await _voice(
            api, session_id, content=b"silence", content_type="audio/webm")
        assert response.status_code == 503
        assert response.json()["error"] == "transcription_unavailable"


# --- readiness and closing: a coaching signal, computed by the platform -----

async def test_session_readiness_is_a_coaching_signal_never_a_forecast(tmp_path):
    """Readiness on a session with nothing evaluated is `UNKNOWN`/null — never a low forecast."""
    async with api_harness(tmp_path) as api:
        session_id = await _open(api)
        response = await api.read(f"/interview-sessions/{session_id}/readiness")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["band"] == "UNKNOWN"
        assert body["overall"] is None
        assert body["evaluated_answers"] == 0
        assert "overall" in body  # the platform's field, present even when null
        _carries_no_forecast(body, "probability", "verdict")


async def test_completing_an_answered_session_writes_a_summary(tmp_path):
    """Completion pairs platform-computed readiness with cleared coaching, and finalizes."""
    async with api_harness(tmp_path) as api:
        session_id = await _open(api)
        await _ask(api, session_id)
        await api.write("POST", f"/interview-sessions/{session_id}/answers",
                        json={"content": "Une réponse."})
        response = await api.write("POST", f"/interview-sessions/{session_id}/complete")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["readiness"]["overall"] is not None  # computed, not provider-authored
        assert body["questions_asked"] == 1
        assert body["answers_evaluated"] == 1
        assert body["generator_key"]
        _carries_no_forecast(body, "probability", "verdict")
        completed = await api.read(f"/interview-sessions/{session_id}")
        assert completed.json()["status"] == "COMPLETED"


async def test_completing_a_created_session_is_409(tmp_path):
    """A session that never started cannot be completed — an invalid-transition 409."""
    async with api_harness(tmp_path) as api:
        session_id = await _open(api)
        response = await api.write("POST", f"/interview-sessions/{session_id}/complete")
        assert response.status_code == 409
        assert response.json()["error"] == "invalid_status_transition"


async def test_abandoning_a_session_is_terminal_then_409(tmp_path):
    """Walking away moves the session to `ABANDONED`; a second abandon has nowhere to go."""
    async with api_harness(tmp_path) as api:
        session_id = await _open(api)
        first = await api.write("POST", f"/interview-sessions/{session_id}/abandon")
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "ABANDONED"
        again = await api.write("POST", f"/interview-sessions/{session_id}/abandon")
        assert again.status_code == 409
        assert again.json()["error"] == "invalid_status_transition"


async def test_a_completed_session_appears_in_the_readiness_history(tmp_path):
    """A completed session's summary is what the readiness history is built from."""
    async with api_harness(tmp_path) as api:
        session_id = await _open(api)
        await _ask(api, session_id)
        await api.write("POST", f"/interview-sessions/{session_id}/answers",
                        json={"content": "Une réponse."})
        await api.write("POST", f"/interview-sessions/{session_id}/complete")
        history = await api.read("/interview-sessions/history")
        assert history.status_code == 200, history.text
        summaries = history.json()["summaries"]
        assert len(summaries) == 1
        assert summaries[0]["session_id"] == session_id





