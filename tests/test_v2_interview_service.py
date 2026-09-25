# tests/test_v2_interview_service.py
"""Le service de session, sur des dépôts en mémoire et un LLM factice (§1-6, §54-59).

`InterviewService` est le seul service applicatif de la phase : il orchestre les pièces pures
(moteur, garde, agrégation de readiness) et le LLM re-validant, sous la règle unique — « on
coache, on ne prédit pas ». On le teste ici de bout en bout sur le harnais `build_interview_service`
(dépôts factices, transcripteur déterministe, `FakeInterviewLLM`), en tenant les trois
ordonnancements porteurs :

- **générer, garder, *puis* persister** : une évaluation dont le coaching invente un fait n'est
  jamais stockée — la réponse subsiste, non notée — et un résumé fabriqué retombe sur une prose
  déterministe sans fait ; la readiness, elle, est calculée par la plateforme dans tous les cas ;
- **le moteur possède l'identité** : type, difficulté, séquence et profondeur viennent du moteur,
  jamais du modèle ; la difficulté monte sur une réponse forte, descend sur une faible, et *tient*
  quand rien n'a pu être évalué ;
- **le propriétaire vient de la session, jamais du corps** : la session d'un autre compte se lit
  *absente* (`SESSION_NOT_FOUND`), sans fuir son existence.
"""
from datetime import UTC, datetime

import pytest

from backend.app.domain.application import (
    Application,
    ApplicationState,
    build_idempotency_key,
)
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.common import SkillRequirement
from backend.app.domain.identifiers import (
    ApplicationId,
    OpportunityId,
    UserId,
    application_id,
    new_application_decision_id,
    new_llm_run_id,
)
from backend.app.domain.interview import (
    InterviewAnswerFormat,
    InterviewDifficulty,
    InterviewErrorCode,
    InterviewMode,
    InterviewQuestionType,
    InterviewSessionStatus,
)
from backend.app.interview.llm import FollowUpDecision, ProposedSummary
from backend.app.interview.service import (
    InterviewError,
    InterviewGroundingNotFound,
    TranscriptReviewRequired,
)
from backend.app.interview.transcriber import DeterministicTranscriber, InterviewAudio
from backend.app.llm.failures import LLMError, LLMFailureCode
from tests.v2_builders import (
    OPPORTUNITY,
    OTHER_OPPORTUNITY,
    OTHER_USER,
    PROFILE,
    USER,
    a_candidate_profile,
    an_opportunity,
)
from tests.v2_interview import FakeInterviewLLM, build_interview_service

pytestmark = pytest.mark.asyncio

# A monotonic clock for a flow that spans several turns, so a session's `updated_at`/`ended_at`
# advance the way a real run's would, rather than every row sharing one instant.
T0 = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 4, 1, 9, 5, tzinfo=UTC)
T2 = datetime(2026, 4, 1, 9, 10, tzinfo=UTC)
T3 = datetime(2026, 4, 1, 9, 15, tzinfo=UTC)


def _invalid_output() -> LLMError:
    """The typed failure a provider raises when its output cannot be trusted."""
    return LLMError(LLMFailureCode.STRUCTURED_OUTPUT_INVALID, detail="unusable output")


async def _create(harness, *, mode=InterviewMode.BEHAVIORAL, now=T0):
    """Open a session over the harness's seeded grounding — the start of every flow."""
    return await harness.service.create_session(
        USER, candidate_profile_id=PROFILE, opportunity_id=OPPORTUNITY, mode=mode, now=now)


def _an_application(*, user_id: UserId = USER,
                    opportunity_id: OpportunityId | None = OPPORTUNITY,
                    state: ApplicationState = ApplicationState.PLANNED) -> Application:
    """A Phase 12 application for one posting, keyed the way the real engine keys it.

    The id derives from the idempotency key, exactly as production does, so the row a test
    seeds is the one `_verify_application` would resolve. `opportunity_id` is what the
    coherence check compares against the session's posting.
    """
    key = build_idempotency_key(
        candidate_profile_id=PROFILE, channel=ApplicationChannel.BROWSER,
        opportunity_id=opportunity_id, company_id=None)
    return Application(
        id=application_id(key), user_id=user_id, candidate_profile_id=PROFILE,
        decision_id=new_application_decision_id(), channel=ApplicationChannel.BROWSER,
        state=state, idempotency_key=key, opportunity_id=opportunity_id,
        created_at=T0, updated_at=T0)


# APPEND-MARKER


# --- opening a session: grounding, planning, ownership ----------------------

async def test_create_session_grounds_and_plans_a_created_session():
    """A new session is CREATED, planned for its mode, and persisted for its owner."""
    harness = build_interview_service()
    session = await _create(harness)
    assert session.status is InterviewSessionStatus.CREATED
    assert session.plan.mode is InterviewMode.BEHAVIORAL
    assert harness.llm.calls["propose_plan"] == 1
    assert await harness.sessions.get(USER, session.id) is not None


async def test_create_session_without_grounding_is_grounding_not_found():
    """With no profile or posting seeded, there is nothing to ground a session in."""
    harness = build_interview_service(seed_grounding=False)
    with pytest.raises(InterviewGroundingNotFound):
        await _create(harness)


async def test_create_session_for_a_foreign_account_cannot_reach_the_grounding():
    """The profile is read owner-first, so another account cannot open a session on it."""
    harness = build_interview_service()
    with pytest.raises(InterviewGroundingNotFound):
        await harness.service.create_session(
            OTHER_USER, candidate_profile_id=PROFILE, opportunity_id=OPPORTUNITY,
            mode=InterviewMode.BEHAVIORAL, now=T0)


async def test_a_failed_plan_generation_is_a_typed_question_generation_error():
    """No plan means no session: a provider failure planning is a hard, typed refusal."""
    harness = build_interview_service(llm=FakeInterviewLLM(plan_error=_invalid_output()))
    with pytest.raises(InterviewError) as caught:
        await _create(harness)
    assert caught.value.code is InterviewErrorCode.QUESTION_GENERATION_UNAVAILABLE


async def test_a_foreign_account_sees_a_session_as_absent():
    """A session owned by USER reads as SESSION_NOT_FOUND for another account (§90)."""
    harness = build_interview_service()
    session = await _create(harness)
    with pytest.raises(InterviewError) as caught:
        await harness.service.get_session(OTHER_USER, session.id)
    assert caught.value.code is InterviewErrorCode.SESSION_NOT_FOUND


# --- asking questions: the engine owns identity, polling is idempotent ------

async def test_next_question_asks_the_first_primary_and_starts_the_session():
    """The first question discharges the plan's first topic and moves CREATED → IN_PROGRESS."""
    harness = build_interview_service()
    session = await _create(harness)
    turn = await harness.service.next_question(USER, session.id, now=T1)
    assert turn.question is not None
    assert turn.question.sequence == 0
    assert turn.question.depth == 0 and turn.question.follows_sequence is None
    assert turn.question.question_type is InterviewQuestionType.BEHAVIORAL
    assert turn.question.generator_key == harness.llm.key  # engine stamps provenance
    assert turn.session.status is InterviewSessionStatus.IN_PROGRESS


async def test_next_question_is_idempotent_until_the_current_one_is_answered():
    """Polling before answering returns the same question and generates nothing new."""
    harness = build_interview_service()
    session = await _create(harness)
    first = await harness.service.next_question(USER, session.id, now=T1)
    again = await harness.service.next_question(USER, session.id, now=T2)
    assert again.question is not None and again.question.id == first.question.id
    assert harness.llm.calls["generate_question"] == 1


async def test_a_failed_question_generation_is_the_one_surfacing_failure():
    """With nothing to return, a provider failure generating a question is typed, not degraded."""
    harness = build_interview_service(
        llm=FakeInterviewLLM(question_error=_invalid_output()))
    session = await _create(harness)
    with pytest.raises(InterviewError) as caught:
        await harness.service.next_question(USER, session.id, now=T1)
    assert caught.value.code is InterviewErrorCode.QUESTION_GENERATION_UNAVAILABLE


# --- answering: best-effort grading, and the difficulty the engine adapts ----

async def _ask_first(harness, *, now=T1):
    """Open a session and ask its first question — the state a submit test starts from."""
    session = await _create(harness)
    turn = await harness.service.next_question(USER, session.id, now=now)
    return session, turn.question


async def test_submit_text_answer_records_grades_and_climbs_on_a_strong_answer():
    """A strong answer is stored, graded, and earns a harder next question (§32)."""
    harness = build_interview_service(llm=FakeInterviewLLM(evaluation_score=0.9))
    session, _ = await _ask_first(harness)
    outcome = await harness.service.submit_text_answer(
        USER, session.id, "Une réponse solide et structurée.", now=T2)
    assert outcome.answer.format is InterviewAnswerFormat.TEXT
    assert outcome.evaluation is not None
    assert outcome.session.difficulty is InterviewDifficulty.ADVANCED
    assert len(await harness.evaluations.list_for_session(USER, session.id)) == 1


async def test_submit_text_answer_descends_on_a_weak_answer():
    """A weak answer is graded and earns an easier next question."""
    harness = build_interview_service(llm=FakeInterviewLLM(evaluation_score=0.2))
    session, _ = await _ask_first(harness)
    outcome = await harness.service.submit_text_answer(
        USER, session.id, "Euh, je ne sais pas trop.", now=T2)
    assert outcome.session.difficulty is InterviewDifficulty.INTRODUCTORY


async def test_a_wholly_unassessed_answer_holds_difficulty_and_is_still_stored():
    """"Not assessed" is not "weak": difficulty holds, and the answer is still graded and kept."""
    harness = build_interview_service(llm=FakeInterviewLLM(evaluation_score=None))
    session, _ = await _ask_first(harness)
    outcome = await harness.service.submit_text_answer(
        USER, session.id, "Une réponse hors-sujet.", now=T2)
    assert outcome.evaluation is not None
    assert outcome.session.difficulty is InterviewDifficulty.INTERMEDIATE


async def test_a_second_answer_to_the_same_question_is_refused():
    """One answer per question in Phase 14: a resubmit is QUESTION_ALREADY_ANSWERED."""
    harness = build_interview_service()
    session, _ = await _ask_first(harness)
    await harness.service.submit_text_answer(USER, session.id, "Première réponse.", now=T2)
    with pytest.raises(InterviewError) as caught:
        await harness.service.submit_text_answer(USER, session.id, "Une autre.", now=T3)
    assert caught.value.code is InterviewErrorCode.QUESTION_ALREADY_ANSWERED


async def test_submitting_with_no_question_asked_is_no_current_question():
    """A submit before any question was asked has nothing to answer."""
    harness = build_interview_service()
    session = await _create(harness)
    with pytest.raises(InterviewError) as caught:
        await harness.service.submit_text_answer(USER, session.id, "Trop tôt.", now=T1)
    assert caught.value.code is InterviewErrorCode.NO_CURRENT_QUESTION


# --- the truth boundary: an ungrounded coaching is never persisted ----------

async def test_an_answer_whose_coaching_invents_a_fact_is_stored_ungraded():
    """Grading is best-effort: a fabricated coaching is refused, but the answer is not lost."""
    # No evidence backs "55%", so the drafted answer invents a number the guard rejects.
    harness = build_interview_service(
        profile=a_candidate_profile(),  # sparse: no evidence corpus
        llm=FakeInterviewLLM(
            evaluation_suggested_answer="J'ai réduit la latence de 55% en un trimestre."))
    session, question = await _ask_first(harness)
    outcome = await harness.service.submit_text_answer(
        USER, session.id, "Ma réponse.", now=T2)
    assert outcome.answer is not None  # the answer stands
    assert outcome.evaluation is None  # its coaching did not survive the guard
    assert await harness.evaluations.get_for_answer(USER, outcome.answer.id) is None


async def test_evaluate_answer_surfaces_a_provider_failure_as_a_typed_error():
    """The strict re-grade path raises EVALUATION_UNAVAILABLE where a submit only degrades."""
    harness = build_interview_service(
        llm=FakeInterviewLLM(evaluation_error=_invalid_output()))
    session, question = await _ask_first(harness)
    outcome = await harness.service.submit_text_answer(USER, session.id, "Réponse.", now=T2)
    assert outcome.evaluation is None  # the submit degraded quietly
    with pytest.raises(InterviewError) as caught:
        await harness.service.evaluate_answer(
            USER, session.id, question_sequence=question.sequence, now=T3)
    assert caught.value.code is InterviewErrorCode.EVALUATION_UNAVAILABLE


# --- voice: transcribe, then behave exactly like a text answer (§14-17) ------

async def test_submit_voice_answer_transcribes_records_and_grades():
    """A spoken answer becomes text, recorded as VOICE with its transcript confidence."""
    transcriber = DeterministicTranscriber(
        transcripts={b"a-clip": "Voici ma réponse orale."}, confidence=0.8)
    harness = build_interview_service(transcriber=transcriber)
    session, _ = await _ask_first(harness)
    audio = InterviewAudio(content=b"a-clip", content_type="audio/webm")
    outcome = await harness.service.submit_voice_answer(USER, session.id, audio, now=T2)
    assert outcome.answer.format is InterviewAnswerFormat.VOICE
    assert outcome.answer.content == "Voici ma réponse orale."
    assert outcome.answer.transcript_confidence == 0.8
    assert outcome.evaluation is not None


async def test_voice_answer_rejects_unusable_audio_and_records_nothing():
    """An unsupported content type is refused before any answer is recorded."""
    harness = build_interview_service()
    session, question = await _ask_first(harness)
    audio = InterviewAudio(content=b"anything", content_type="audio/flac")
    with pytest.raises(InterviewError) as caught:
        await harness.service.submit_voice_answer(USER, session.id, audio, now=T2)
    assert caught.value.code is InterviewErrorCode.UNSUPPORTED_AUDIO
    assert await harness.answers.get_for_question(USER, question.id) is None


# --- completing: platform readiness, guarded prose, safe fallback -----------

async def _answer_one(harness):
    """Open a session, ask a question, answer it — an IN_PROGRESS session with one grade."""
    session, _ = await _ask_first(harness)
    await harness.service.submit_text_answer(USER, session.id, "Une réponse.", now=T2)
    return session


async def test_complete_session_aggregates_readiness_and_writes_a_model_summary():
    """Completion pairs platform-computed readiness with the provider's cleared coaching."""
    harness = build_interview_service()
    session = await _answer_one(harness)
    summary = await harness.service.complete_session(USER, session.id, now=T3)
    assert summary.readiness.overall is not None  # computed by the platform, not the model
    assert summary.questions_asked == 1
    assert summary.answers_evaluated == summary.readiness.evaluated_answers == 1
    assert summary.generator_key == harness.llm.key  # the model's prose cleared the guard
    completed = await harness.sessions.get(USER, session.id)
    assert completed.status is InterviewSessionStatus.COMPLETED
    assert completed.ended_at == T3


async def test_complete_session_falls_back_to_safe_prose_when_coaching_is_rejected():
    """A fabricated summary is refused; the session still completes with fact-free prose."""
    fabricated = ProposedSummary(
        headline="Excellente maîtrise technique.",
        focus_areas=("approfondir Kafka pour ce poste",))  # Kafka is claimable, unbacked
    harness = build_interview_service(
        opportunity=an_opportunity(skill_requirements=(SkillRequirement(skill="Kafka"),)),
        llm=FakeInterviewLLM(summary=fabricated))
    session = await _answer_one(harness)
    summary = await harness.service.complete_session(USER, session.id, now=T3)
    assert summary.readiness.overall is not None  # readiness is untouched by the fallback
    assert summary.strengths == () and summary.focus_areas == ()
    assert "readiness" in summary.headline.lower()
    assert summary.generator_key != harness.llm.key  # the safe, deterministic key


async def test_complete_session_on_a_created_session_is_an_invalid_transition():
    """A session that never started cannot be completed."""
    harness = build_interview_service()
    session = await _create(harness)
    with pytest.raises(InterviewError) as caught:
        await harness.service.complete_session(USER, session.id, now=T3)
    assert caught.value.code is InterviewErrorCode.INVALID_STATUS_TRANSITION


async def test_completed_sessions_feed_the_readiness_history():
    """A completed session's summary is what the readiness history is built from (§74)."""
    harness = build_interview_service()
    session = await _answer_one(harness)
    await harness.service.complete_session(USER, session.id, now=T3)
    history = await harness.service.readiness_history(USER)
    assert len(history) == 1
    assert history[0].session_id == session.id


# --- live readiness and abandonment -----------------------------------------

async def test_session_readiness_is_computed_live_without_ending_the_session():
    """Mid-practice readiness runs the same aggregation, with no provider and no completion."""
    harness = build_interview_service()
    session = await _answer_one(harness)
    readiness = await harness.service.session_readiness(USER, session.id, now=T3)
    assert readiness.overall is not None
    assert readiness.evaluated_answers == 1
    still_active = await harness.sessions.get(USER, session.id)
    assert still_active.status is InterviewSessionStatus.IN_PROGRESS


async def test_abandon_moves_to_terminal_and_refuses_a_second_time():
    """Walking away is terminal: a second abandon has nowhere to go."""
    harness = build_interview_service()
    session = await _create(harness)
    abandoned = await harness.service.abandon_session(USER, session.id, now=T2)
    assert abandoned.status is InterviewSessionStatus.ABANDONED
    assert abandoned.ended_at == T2
    with pytest.raises(InterviewError) as caught:
        await harness.service.abandon_session(USER, session.id, now=T3)
    assert caught.value.code is InterviewErrorCode.INVALID_STATUS_TRANSITION


# --- an application reference must be this account's and about this posting --

async def test_create_session_binds_a_matching_application():
    """A session may rehearse an application that is this account's and about this posting."""
    harness = build_interview_service()
    application = _an_application(opportunity_id=OPPORTUNITY)
    harness.applications.applications[application.id] = application
    session = await harness.service.create_session(
        USER, candidate_profile_id=PROFILE, opportunity_id=OPPORTUNITY,
        mode=InterviewMode.BEHAVIORAL, now=T0, application_id=application.id)
    assert session.application_id == application.id


async def test_create_session_with_a_missing_application_is_not_found():
    """An application id that resolves to nothing is refused before the session exists."""
    harness = build_interview_service()
    missing = _an_application().id  # computed, but never seeded
    with pytest.raises(InterviewError) as caught:
        await harness.service.create_session(
            USER, candidate_profile_id=PROFILE, opportunity_id=OPPORTUNITY,
            mode=InterviewMode.BEHAVIORAL, now=T0, application_id=missing)
    assert caught.value.code is InterviewErrorCode.APPLICATION_NOT_FOUND


async def test_create_session_with_a_foreign_application_is_indistinguishably_not_found():
    """Another account's application reads as absent — the read is owner-first, leaking nothing."""
    harness = build_interview_service()
    foreign = _an_application(user_id=OTHER_USER, opportunity_id=OPPORTUNITY)
    harness.applications.applications[foreign.id] = foreign
    with pytest.raises(InterviewError) as caught:
        await harness.service.create_session(
            USER, candidate_profile_id=PROFILE, opportunity_id=OPPORTUNITY,
            mode=InterviewMode.BEHAVIORAL, now=T0, application_id=foreign.id)
    assert caught.value.code is InterviewErrorCode.APPLICATION_NOT_FOUND


async def test_create_session_with_a_cross_opportunity_application_is_a_mismatch():
    """An application about another posting cannot be borrowed to coach this session."""
    harness = build_interview_service()
    other = _an_application(opportunity_id=OTHER_OPPORTUNITY)
    harness.applications.applications[other.id] = other
    with pytest.raises(InterviewError) as caught:
        await harness.service.create_session(
            USER, candidate_profile_id=PROFILE, opportunity_id=OPPORTUNITY,
            mode=InterviewMode.BEHAVIORAL, now=T0, application_id=other.id)
    assert caught.value.code is InterviewErrorCode.APPLICATION_OPPORTUNITY_MISMATCH


# --- a generated question is grounded before it is ever persisted (§10-17) ---

async def test_a_question_that_invents_a_candidate_fact_is_never_persisted():
    """A prompt asserting a candidate fact no evidence backs is refused, and nothing is stored.

    The number "250" is nowhere in the sparse profile or the posting, so the grounding guard
    rejects the prompt; the one bounded repair regenerates the same fabrication, and the turn
    fails `QUESTION_GROUNDING_FAILED` rather than persisting an ungrounded question.
    """
    harness = build_interview_service(
        profile=a_candidate_profile(),  # sparse: no evidence corpus
        llm=FakeInterviewLLM(
            question_prompt="Parlez-moi de la fois où vous avez dirigé 250 ingénieurs."))
    session = await _create(harness)
    with pytest.raises(InterviewError) as caught:
        await harness.service.next_question(USER, session.id, now=T1)
    assert caught.value.code is InterviewErrorCode.QUESTION_GROUNDING_FAILED
    assert harness.llm.calls["generate_question"] == 2  # one repair attempt, then it gives up
    assert await harness.questions.list_for_session(USER, session.id) == ()


async def test_a_question_may_stand_on_a_posting_requirement():
    """A question grounded in the posting's own requirement is allowed — that is not fabrication."""
    harness = build_interview_service(
        opportunity=an_opportunity(skill_requirements=(SkillRequirement(skill="Kafka"),)),
        llm=FakeInterviewLLM(
            question_prompt="Comment aborderiez-vous l'usage de Kafka pour ce poste ?"))
    session = await _create(harness)
    turn = await harness.service.next_question(USER, session.id, now=T1)
    assert turn.question is not None
    assert "Kafka" in turn.question.prompt
    assert harness.llm.calls["generate_question"] == 1  # cleared on the first try


async def test_a_repaired_question_that_becomes_grounded_is_persisted():
    """The bounded repair is real: a second, grounded proposal is accepted and stored."""
    llm = FakeInterviewLLM()
    # First proposal fabricates a number; the fake's next proposal (variante B) is clean.
    llm._question_prompt = "Décrivez une situation professionnelle marquante."
    original_generate = llm.generate_question
    calls = {"n": 0}

    async def _fabricate_once(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            llm._question_prompt = "Parlez d'un projet de 250 personnes que vous avez mené."
        else:
            llm._question_prompt = "Décrivez une situation professionnelle marquante."
        return await original_generate(**kwargs)

    llm.generate_question = _fabricate_once  # type: ignore[method-assign]
    harness = build_interview_service(profile=a_candidate_profile(), llm=llm)
    session = await _create(harness)
    turn = await harness.service.next_question(USER, session.id, now=T1)
    assert turn.question is not None
    assert "250" not in turn.question.prompt
    assert calls["n"] == 2  # it took the one repair


# --- an adaptive follow-up consumes the persisted evaluation (§18-26) --------

async def test_an_adaptive_follow_up_consumes_the_persisted_evaluation():
    """The follow-up decision is handed the stored grade of the last answer, never a recomputation."""
    harness = build_interview_service(llm=FakeInterviewLLM(
        follow_up=FollowUpDecision(ask_follow_up=True, prompt="Et concrètement, comment ?",
                                   question_type=InterviewQuestionType.SITUATIONAL)))
    session, first = await _ask_first(harness)
    outcome = await harness.service.submit_text_answer(USER, session.id, "Une réponse.", now=T2)
    assert outcome.evaluation is not None
    turn = await harness.service.next_question(USER, session.id, now=T3)
    # The follow-up was decided from the evaluation the service persisted for the answer.
    assert harness.llm.calls["decide_follow_up"] == 1
    consumed = harness.llm.last_follow_up_evaluation
    assert consumed is not None
    assert consumed.id == outcome.evaluation.id
    assert consumed.answer_id == outcome.answer.id
    # And the engine shaped it as a follow-up drilling the first question one rung deeper.
    assert turn.question is not None
    assert turn.question.depth == 1
    assert turn.question.follows_sequence == first.sequence


async def test_without_an_evaluation_no_adaptive_follow_up_is_asked():
    """A lost evaluation means the session falls through to its plan, never drilling blind."""
    harness = build_interview_service(llm=FakeInterviewLLM(
        evaluation_error=_invalid_output(),
        follow_up=FollowUpDecision(ask_follow_up=True, prompt="Et ensuite ?",
                                   question_type=InterviewQuestionType.SITUATIONAL)))
    session, _ = await _ask_first(harness)
    outcome = await harness.service.submit_text_answer(USER, session.id, "Une réponse.", now=T2)
    assert outcome.evaluation is None  # grading failed, nothing persisted
    turn = await harness.service.next_question(USER, session.id, now=T3)
    assert harness.llm.calls["decide_follow_up"] == 0  # never even asked, with no grade to show
    assert turn.question is not None
    assert turn.question.depth == 0  # a fresh planned-topic primary, not a follow-up
    assert turn.question.follows_sequence is None


# --- provenance: every artifact records the run that produced it (§27-40) ----

async def test_each_artifact_records_the_run_that_produced_it():
    """Plan, question, evaluation and summary each carry the exact `llm_run_id` behind them."""
    run_id = new_llm_run_id()
    harness = build_interview_service(llm=FakeInterviewLLM(llm_run_id=run_id))
    session = await _create(harness)
    assert session.plan_llm_run_id == run_id
    turn = await harness.service.next_question(USER, session.id, now=T1)
    assert turn.question is not None and turn.question.llm_run_id == run_id
    outcome = await harness.service.submit_text_answer(USER, session.id, "Une réponse.", now=T2)
    assert outcome.evaluation is not None and outcome.evaluation.llm_run_id == run_id
    summary = await harness.service.complete_session(USER, session.id, now=T3)
    assert summary.llm_run_id == run_id


async def test_a_deterministic_fallback_summary_records_no_run():
    """When the coaching prose is rejected, the safe summary is honest about having no run."""
    fabricated = ProposedSummary(
        headline="Excellente maîtrise technique.",
        focus_areas=("approfondir Kafka pour ce poste",))  # Kafka is claimable, unbacked
    harness = build_interview_service(
        opportunity=an_opportunity(skill_requirements=(SkillRequirement(skill="Kafka"),)),
        llm=FakeInterviewLLM(llm_run_id=new_llm_run_id(), summary=fabricated))
    session, _ = await _ask_first(harness)
    await harness.service.submit_text_answer(USER, session.id, "Une réponse.", now=T2)
    summary = await harness.service.complete_session(USER, session.id, now=T3)
    assert summary.llm_run_id is None  # no model authored the persisted prose
    assert summary.generator_key != harness.llm.key


# --- a low-confidence voice transcript is held for review, never graded ------

async def test_a_low_confidence_transcript_requires_review_and_records_nothing():
    """A transcript below the auto-evaluate floor is held: no answer, no grade, no readiness move."""
    transcriber = DeterministicTranscriber(
        transcripts={b"muffled": "Peut-être que j'ai fait quelque chose comme ça."},
        confidence=0.2)
    harness = build_interview_service(transcriber=transcriber, min_transcript_confidence=0.5)
    session, question = await _ask_first(harness)
    audio = InterviewAudio(content=b"muffled", content_type="audio/webm")
    with pytest.raises(TranscriptReviewRequired) as caught:
        await harness.service.submit_voice_answer(USER, session.id, audio, now=T2)
    assert caught.value.confidence == 0.2
    assert caught.value.transcript  # the words ride back for the candidate to confirm
    assert await harness.answers.get_for_question(USER, question.id) is None
    readiness = await harness.service.session_readiness(USER, session.id, now=T3)
    assert readiness.answered_questions == 0  # STT uncertainty never touched readiness


async def test_a_transcript_with_no_confidence_is_graded_normally():
    """`None` confidence is "unknown", not "known-low": it takes the ordinary grading path."""
    transcriber = DeterministicTranscriber(
        transcripts={b"clip": "Voici ma réponse claire."}, confidence=None)
    harness = build_interview_service(transcriber=transcriber, min_transcript_confidence=0.5)
    session, _ = await _ask_first(harness)
    audio = InterviewAudio(content=b"clip", content_type="audio/webm")
    outcome = await harness.service.submit_voice_answer(USER, session.id, audio, now=T2)
    assert outcome.answer.format is InterviewAnswerFormat.VOICE
    assert outcome.answer.transcript_confidence is None
    assert outcome.evaluation is not None




