# tests/test_v2_interview_llm.py
"""L'adaptateur d'entretien, sur un vrai routeur et un provider factice (§45-48).

`InterviewLLM` est le miroir de `LLMDocumentGenerator` pour les cinq tâches d'entretien : il
compose un prompt versionné, le route sous une politique de confidentialité, puis — le point
porteur — *re-valide chaque réponse contre le domaine* avant de la rendre. On tient ici les
trois frontières qui rendent physiques les promesses de la phase :

- un provider ne peut jamais *écrire* la readiness : une évaluation qui glisse un `readiness`,
  une `probability` ou un `verdict` échoue à parser, car `InterviewAnswerEvaluation` n'a aucun
  de ces champs et interdit les extras (§33, §93) ;
- le moteur possède l'identité, le modèle propose le texte : question et relance reviennent en
  *propositions* sans id, sequence ni depth ;
- les champs que la plateforme possède (id déterministe, clés d'appartenance, horodatage,
  `evaluator_key`) sont fusionnés *après* ceux du modèle, si bien qu'un id ou une clé forgés
  n'y survivent pas.

Le provider est `tests/v2_llm.FakeProvider`, renvoyant du texte canné sur un vrai `LLMRouter` :
ce qu'on teste, c'est le parsing, le routage et la re-validation de l'adaptateur, pas un
transport.
"""
import json
from datetime import UTC, datetime

import pytest

from backend.app.domain.identifiers import interview_answer_evaluation_id
from backend.app.domain.interview import (
    InterviewAnswerEvaluation,
    InterviewDifficulty,
    InterviewMode,
    InterviewPlan,
    InterviewQuestionType,
)
from backend.app.interview.context import InterviewContext
from backend.app.interview.llm import (
    LLM_INTERVIEW_KEY,
    FollowUpDecision,
    InterviewLLM,
    InterviewLLMResult,
    ProposedQuestion,
    ProposedSummary,
)
from backend.app.llm.capabilities import BASELINE_CAPABILITY, Capability
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.recorder import LLMTelemetryRecorder
from backend.app.llm.registry import LLMProviderRegistry
from backend.app.llm.router import LLMRouter, PrivacyClass, RoutingPolicy
from tests.v2_builders import (
    USER,
    a_candidate_profile,
    an_answer_evaluation,
    an_evidence_record,
    an_interview_answer,
    an_interview_question,
    an_opportunity,
)
from tests.v2_fakes import FakeLLMRunRepository
from tests.v2_llm import FakeProvider

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 1, 8, 0, tzinfo=UTC)

# Every interview prompt attaches a system instruction and a structured-output spec, so a
# provider that is to serve one must claim both capabilities — the same shape the document
# generator tests require of a provider.
_INTERVIEW_CAPS = frozenset({BASELINE_CAPABILITY, Capability.STRUCTURED_OUTPUT,
                             Capability.SYSTEM_INSTRUCTIONS})
_LOCAL = RoutingPolicy(privacy=PrivacyClass.LOCAL_ONLY)

# APPEND-MARKER


def _local_router(text, *, capabilities=_INTERVIEW_CAPS, provider_key="local_llm"):
    """A real router over a single local `FakeProvider` returning `text` — the doc-gen pattern."""
    provider = FakeProvider(provider_key=provider_key, local=True, text=text,
                            capabilities=capabilities)
    registry = LLMProviderRegistry()
    registry.register(provider)
    return LLMRouter(registry), provider


def _context():
    """A grounding of one candidate (one evidence record) rehearsing the sample posting."""
    return InterviewContext(
        profile=a_candidate_profile(evidence=(an_evidence_record(),)),
        opportunity=an_opportunity())


def _llm(router, **kwargs):
    return InterviewLLM(router=router, policy=_LOCAL, **kwargs)


_QUESTION_JSON = json.dumps({
    "prompt": "Décrivez un désaccord technique que vous avez dénoué.",
    "question_type": "BEHAVIORAL",
    "difficulty": "INTERMEDIATE",
    "topic_label": "past teamwork",
})
_SUMMARY_JSON = json.dumps({
    "headline": "Motivation claire, exemples encore à chiffrer.",
    "strengths": ["articule clairement ses motivations"],
    "focus_areas": ["étayer chaque réussite d'un résultat chiffré"],
})


# --- each task parses a well-formed answer into its domain value ------------

async def test_propose_plan_parses_a_plan_and_the_mode_is_the_platforms():
    """The model proposes topics; the *mode* is stamped by the platform, never read back."""
    router, provider = _local_router(json.dumps({
        "mode": "TECHNICAL",  # a model does not choose the mode — the caller's wins
        "topics": [
            {"label": "past teamwork", "question_type": "BEHAVIORAL", "target_questions": 2},
            {"label": "motivation", "question_type": "MOTIVATION", "target_questions": 1},
        ],
    }))
    result = await _llm(router).propose_plan(context=_context(), mode=InterviewMode.BEHAVIORAL)
    assert isinstance(result, InterviewLLMResult)
    plan = result.value
    assert isinstance(plan, InterviewPlan)
    assert plan.mode is InterviewMode.BEHAVIORAL
    assert provider.calls == 1  # it actually routed to the provider


async def test_generate_question_returns_an_identity_free_proposal():
    """A question comes back as a `ProposedQuestion`: text and type, but no id/sequence/depth."""
    router, _ = _local_router(_QUESTION_JSON)
    result = await _llm(router).generate_question(
        context=_context(), mode=InterviewMode.BEHAVIORAL,
        question_type=InterviewQuestionType.BEHAVIORAL,
        difficulty=InterviewDifficulty.INTERMEDIATE, topic_label="past teamwork")
    assert isinstance(result, InterviewLLMResult)
    proposal = result.value
    assert isinstance(proposal, ProposedQuestion)
    assert proposal.prompt
    assert proposal.question_type is InterviewQuestionType.BEHAVIORAL
    # The engine owns identity; a proposal carries none of it.
    assert not hasattr(proposal, "id")
    assert not hasattr(proposal, "sequence")
    assert not hasattr(proposal, "depth")


async def test_decide_follow_up_parses_a_warranted_decision():
    """A decision to ask carries its prompt and type — the invariant guarantees both."""
    router, _ = _local_router(json.dumps({
        "ask_follow_up": True,
        "prompt": "Qu'auriez-vous fait différemment ?",
        "question_type": "SITUATIONAL",
    }))
    decision = (await _llm(router).decide_follow_up(
        context=_context(), question=an_interview_question(), answer=an_interview_answer(),
        evaluation=an_answer_evaluation())).value
    assert isinstance(decision, FollowUpDecision)
    assert decision.ask_follow_up is True
    assert decision.prompt
    assert decision.question_type is InterviewQuestionType.SITUATIONAL


async def test_decide_follow_up_accepts_a_clean_no():
    """`ask_follow_up=false` is a first-class answer — no prompt, no type, and that is valid."""
    router, _ = _local_router(json.dumps({"ask_follow_up": False}))
    decision = (await _llm(router).decide_follow_up(
        context=_context(), question=an_interview_question(), answer=an_interview_answer(),
        evaluation=an_answer_evaluation())).value
    assert decision.ask_follow_up is False
    assert decision.prompt is None


async def test_generate_summary_parses_the_coaching_prose():
    """A summary is the prose only — headline, strengths, focus — and never a readiness."""
    router, _ = _local_router(_SUMMARY_JSON)
    summary = (await _llm(router).generate_summary(
        context=_context(), mode=InterviewMode.BEHAVIORAL, transcript="Q1 / R1")).value
    assert isinstance(summary, ProposedSummary)
    assert summary.headline
    assert not hasattr(summary, "readiness")


# --- the load-bearing boundary: a provider can never author readiness -------

async def test_evaluate_answer_parses_and_the_platform_owns_the_identity_fields():
    """The model grades; the platform merges in id, ownership, key and timestamp *after*.

    A forged `id` and `evaluator_key` in the payload are overridden by the owned fields, so
    a provider cannot forge its own provenance or backdate a run.
    """
    router, _ = _local_router(json.dumps({
        "dimensions": [{"dimension": "CLARITY", "status": "EVALUATED", "score": 0.8}],
        "id": "forged-evaluation-id",
        "evaluator_key": "forged/9",
    }))
    answer = an_interview_answer()
    evaluation = (await _llm(router).evaluate_answer(
        context=_context(), question=an_interview_question(), answer=answer,
        evaluated_at=NOW)).value
    assert isinstance(evaluation, InterviewAnswerEvaluation)
    assert evaluation.id == interview_answer_evaluation_id(answer.id)
    assert evaluation.evaluator_key == LLM_INTERVIEW_KEY
    assert evaluation.answer_id == answer.id
    assert evaluation.session_id == answer.session_id
    assert evaluation.user_id == answer.user_id
    assert evaluation.evaluated_at == NOW


@pytest.mark.parametrize("field, value", [
    ("readiness", 0.9),
    ("probability", 0.8),
    ("verdict", "would hire"),
])
async def test_evaluate_answer_refuses_a_smuggled_readiness_field(field, value):
    """`InterviewAnswerEvaluation` has no such field and forbids extras, so it fails to parse."""
    router, _ = _local_router(json.dumps({
        "dimensions": [{"dimension": "CLARITY", "status": "EVALUATED", "score": 0.8}],
        field: value,
    }))
    with pytest.raises(LLMError) as caught:
        await _llm(router).evaluate_answer(
            context=_context(), question=an_interview_question(),
            answer=an_interview_answer(), evaluated_at=NOW)
    assert caught.value.code is LLMFailureCode.STRUCTURED_OUTPUT_INVALID


async def test_generate_summary_refuses_a_smuggled_readiness():
    """A summary is prose only: a `readiness` on it fails exactly as one on an evaluation would."""
    router, _ = _local_router(json.dumps({
        "headline": "Bonne clarté d'ensemble.",
        "readiness": 0.9,
    }))
    with pytest.raises(LLMError) as caught:
        await _llm(router).generate_summary(
            context=_context(), mode=InterviewMode.BEHAVIORAL, transcript="Q1 / R1")
    assert caught.value.code is LLMFailureCode.STRUCTURED_OUTPUT_INVALID


async def test_decide_follow_up_refuses_ask_with_no_question():
    """"Ask, but no question" is exactly what the proposal's invariant forbids."""
    router, _ = _local_router(json.dumps({"ask_follow_up": True}))
    with pytest.raises(LLMError) as caught:
        await _llm(router).decide_follow_up(
            context=_context(), question=an_interview_question(),
            answer=an_interview_answer(), evaluation=an_answer_evaluation())
    assert caught.value.code is LLMFailureCode.STRUCTURED_OUTPUT_INVALID


# --- the model's output is untrusted: it is re-validated (§42) --------------

async def test_a_non_json_answer_is_a_typed_structured_output_error():
    """Prose where JSON was asked for is a typed failure, never an uncaught decode error."""
    router, _ = _local_router("Je ne peux pas faire cela.")
    with pytest.raises(LLMError) as caught:
        await _llm(router).propose_plan(context=_context(), mode=InterviewMode.BEHAVIORAL)
    assert caught.value.code is LLMFailureCode.STRUCTURED_OUTPUT_INVALID


async def test_a_json_array_is_not_an_object_and_is_refused():
    """Every task's schema is an object; a payload that parses to a list is as invalid."""
    router, _ = _local_router(json.dumps([1, 2, 3]))
    with pytest.raises(LLMError) as caught:
        await _llm(router).propose_plan(context=_context(), mode=InterviewMode.BEHAVIORAL)
    assert caught.value.code is LLMFailureCode.STRUCTURED_OUTPUT_INVALID


async def test_the_typed_error_does_not_echo_the_offending_payload():
    """A ValidationError can quote the bad input; the typed error must not forward it."""
    sensitive = "SSN-999-88-7777-should-not-appear"
    router, _ = _local_router(json.dumps({"headline": sensitive, "readiness": 0.9}))
    with pytest.raises(LLMError) as caught:
        await _llm(router).generate_summary(
            context=_context(), mode=InterviewMode.BEHAVIORAL, transcript="Q1 / R1")
    assert sensitive not in (caught.value.detail or "")


# --- the request carries provenance, the evidence, and the fenced posting ---

async def test_the_routed_request_stamps_the_prompt_and_carries_evidence_and_fence():
    """The routed request bears the prompt version, every held evidence id, and a fenced posting."""
    router, provider = _local_router(_QUESTION_JSON)
    context = _context()
    await _llm(router).generate_question(
        context=context, mode=InterviewMode.BEHAVIORAL,
        question_type=InterviewQuestionType.BEHAVIORAL,
        difficulty=InterviewDifficulty.INTERMEDIATE)
    request = provider.seen[0]
    assert request.prompt_name == "interview_question"
    assert request.prompt_version == "1.1"
    assert request.structured_output is not None
    payload = request.messages[0].content
    for record in context.profile.evidence:
        assert str(record.id) in payload  # the only ids the model may cite
    assert "untrusted" in payload.lower()  # the posting is fenced as untrusted


# --- privacy is enforced by the router before any provider is reached -------

async def test_a_local_only_policy_never_reaches_a_remote_provider():
    """The prompt bears candidate evidence: LOCAL_ONLY must refuse a remote-only fleet."""
    remote = FakeProvider(provider_key="remote_llm", local=False,
                          text=_QUESTION_JSON, capabilities=_INTERVIEW_CAPS)
    registry = LLMProviderRegistry()
    registry.register(remote)
    llm = InterviewLLM(router=LLMRouter(registry), policy=_LOCAL)
    with pytest.raises(LLMError) as caught:
        await llm.generate_question(
            context=_context(), mode=InterviewMode.BEHAVIORAL,
            question_type=InterviewQuestionType.BEHAVIORAL,
            difficulty=InterviewDifficulty.INTERMEDIATE)
    assert caught.value.code is LLMFailureCode.PROVIDER_MISCONFIGURED
    assert remote.calls == 0


# --- telemetry: a generation is recorded, keyed to the prompt version -------

async def test_generation_through_the_recorder_writes_a_run():
    """Routed through the recorder, one question generation writes one attributed `LLMRun`."""
    router, _ = _local_router(_QUESTION_JSON)
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs)
    llm = InterviewLLM(router=router, policy=_LOCAL, recorder=recorder, user_id=USER)
    await llm.generate_question(
        context=_context(), mode=InterviewMode.BEHAVIORAL,
        question_type=InterviewQuestionType.BEHAVIORAL,
        difficulty=InterviewDifficulty.INTERMEDIATE)
    assert len(runs.runs) == 1
    run = next(iter(runs.runs.values()))
    assert run.prompt_name == "interview_question"
    assert run.prompt_version == "1.1"
    assert run.user_id == USER
    assert run.provider_key == "local_llm"


# --- provenance: the result carries the exact run, and it cannot be forged --

async def test_the_result_carries_the_exact_run_that_produced_it():
    """`InterviewLLMResult.llm_run_id` is the very run the recorder wrote — not a re-query."""
    router, _ = _local_router(_QUESTION_JSON)
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs)
    llm = InterviewLLM(router=router, policy=_LOCAL, recorder=recorder, user_id=USER)
    result = await llm.generate_question(
        context=_context(), mode=InterviewMode.BEHAVIORAL,
        question_type=InterviewQuestionType.BEHAVIORAL,
        difficulty=InterviewDifficulty.INTERMEDIATE)
    assert len(runs.runs) == 1
    (written,) = runs.runs.values()
    assert result.llm_run_id == written.id


async def test_without_a_recorder_the_run_id_is_honestly_absent():
    """No recorder wrapping the route means no run to point at — `llm_run_id` is `None`, never faked."""
    router, _ = _local_router(_QUESTION_JSON)
    result = await _llm(router).generate_question(
        context=_context(), mode=InterviewMode.BEHAVIORAL,
        question_type=InterviewQuestionType.BEHAVIORAL,
        difficulty=InterviewDifficulty.INTERMEDIATE)
    assert result.llm_run_id is None


async def test_an_evaluation_cannot_forge_its_own_run_id():
    """A `llm_run_id` smuggled into the payload is overridden by the run the recorder wrote.

    The evaluation carries provenance itself; the platform-owned merge applies the recorder's
    real run id *after* the model's data, so a provider cannot claim a run it did not produce.
    """
    forged = "00000000-0000-0000-0000-000000000000"
    router, _ = _local_router(json.dumps({
        "dimensions": [{"dimension": "CLARITY", "status": "EVALUATED", "score": 0.8}],
        "llm_run_id": forged,
    }))
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(runs=runs)
    llm = InterviewLLM(router=router, policy=_LOCAL, recorder=recorder, user_id=USER)
    result = await llm.evaluate_answer(
        context=_context(), question=an_interview_question(),
        answer=an_interview_answer(), evaluated_at=NOW)
    (written,) = runs.runs.values()
    assert result.llm_run_id == written.id
    assert result.value.llm_run_id == written.id
    assert str(result.value.llm_run_id) != forged


async def test_the_interview_key_names_the_strategy_not_the_model():
    """The adapter's provenance stamp names the strategy, stable across a re-route."""
    router, _ = _local_router(_QUESTION_JSON)
    assert InterviewLLM(router=router, policy=_LOCAL).key == LLM_INTERVIEW_KEY


