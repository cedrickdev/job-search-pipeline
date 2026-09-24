"""The provider-neutral interview LLM adapter — routes a prompt, re-validates the answer (§45-48).

The mirror of `LLMDocumentGenerator` for the five interview tasks. It composes a versioned
prompt out of an `InterviewContext`, routes it through the same `LLMRouter` under the same
privacy policy, and — the load-bearing part — **re-validates every answer against the domain
before returning it**, so a provider's claim to have honoured a schema is never taken on
trust (§42). A payload that does not parse is a typed `STRUCTURED_OUTPUT_INVALID`, never a
half-built object.

Three boundaries make the phase's promises physical here:

- **A provider can never author readiness.** `evaluate_answer` builds a full
  `InterviewAnswerEvaluation`, whose schema has *no* readiness field and whose `extra="forbid"`
  config rejects one — so a provider that smuggles a `readiness`, a `probability` or a hiring
  `verdict` into its JSON fails to parse (§33, §93). Readiness is computed later by
  `aggregate_session_readiness`, from a whole session's evaluations, with no provider in the
  call. This adapter never computes it and never lets one be returned.
- **The engine owns identity; the model proposes text.** Question and follow-up generation
  return identity-free *proposals* (`ProposedQuestion`, `FollowUpDecision`): the model writes
  the prompt and its metadata, and the engine assigns the id, the sequence and the depth. A
  proposal is a frozen, closed value, so an invented field on it fails exactly as it would on
  a domain model.
- **Privacy is the caller's decision.** The prompt carries the candidate's evidence, so how
  far it may travel is a `RoutingPolicy` the wiring supplies — `LOCAL_ONLY` for anything
  bearing candidate data unless an operator opted a connection out. This adapter never picks a
  provider; it hands the request to the router, which enforces that policy first.

Every call routes *through the telemetry recorder* when one is supplied, so a generated
question or evaluation is traceable to the run, the model and the prompt version that produced
it (§56).
"""
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.app.domain.identifiers import interview_answer_evaluation_id
from backend.app.domain.interview import (
    InterviewAnswer,
    InterviewAnswerEvaluation,
    InterviewDifficulty,
    InterviewMode,
    InterviewPlan,
    InterviewQuestion,
    InterviewQuestionType,
)
from backend.app.interview.context import InterviewContext
from backend.app.interview.prompts import interview_prompt_registry
from backend.app.llm.contracts import LLMResponse
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.prompts import PromptName, PromptRegistry
from backend.app.llm.recorder import LLMTelemetryRecorder
from backend.app.llm.router import LLMRouter, RoutingPolicy

# The interview adapter's provenance stamp, recorded as a question's `generator_key` and an
# evaluation's `evaluator_key`. It names the *strategy* ("a model produced this"), not the
# model or the connection — those live on the `LLMRun` keyed to the same call — so the key
# stays stable across a re-route, exactly as the document generator's does.
LLM_INTERVIEW_KEY = "llm-backed/1"

_T = TypeVar("_T", bound=BaseModel)


class _InterviewProposal(BaseModel):
    """Frozen, closed base for the identity-free values a model proposes.

    Not a domain model — a proposal carries no id, no session, no timestamp, because those
    are the engine's to assign. The config is the domain's in spirit: `frozen` so a proposal
    is a value, `extra="forbid"` so an invented field fails to parse exactly as it would on a
    domain model — the re-validation the whole adapter rests on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProposedQuestion(_InterviewProposal):
    """One question's text and metadata as the model proposes it (§8-9).

    The engine assigns the id, the sequence, the follow-up depth and the *authoritative*
    difficulty (which it adapts, §32); the model only writes the prompt and classifies it. A
    `difficulty` is carried so the model can echo the rung it wrote for, but the engine's
    decision is what the persisted question records.
    """

    prompt: Annotated[str, Field(min_length=1)]
    question_type: InterviewQuestionType
    difficulty: InterviewDifficulty
    topic_label: str | None = None


class FollowUpDecision(_InterviewProposal):
    """Whether one answer warrants drilling in, and the follow-up question when it does (§26-29).

    `ask_follow_up=false` is a first-class answer — a complete answer needs no follow-up, and
    the engine moves on. When true, the decision must carry the follow-up prompt and its type;
    the invariant enforces that, so the engine never has to cope with "yes, but no question".
    """

    ask_follow_up: bool
    prompt: str | None = None
    question_type: InterviewQuestionType | None = None
    topic_label: str | None = None
    rationale: str | None = None

    @model_validator(mode="after")
    def _question_present_iff_following_up(self) -> Self:
        if self.ask_follow_up and not (self.prompt and self.prompt.strip()):
            raise ValueError("a follow-up decision to ask must carry a prompt")
        if self.ask_follow_up and self.question_type is None:
            raise ValueError("a follow-up decision to ask must carry a question_type")
        return self


class ProposedSummary(_InterviewProposal):
    """The coaching prose of a closing summary — never its readiness (§37-39, §123).

    `headline`, `strengths` and `focus_areas` are what the model writes; the `readiness` a
    session summary carries is computed by the platform and paired with this prose by the
    service. There is no readiness field here for the same reason there is none on
    `InterviewAnswerEvaluation`: a provider does not get to author it.
    """

    headline: Annotated[str, Field(min_length=1)]
    strengths: tuple[str, ...] = ()
    focus_areas: tuple[str, ...] = ()


class InterviewLLM:
    """Generates interview artefacts by routing versioned prompts and re-validating answers.

    Holds the router it routes through, the privacy policy that says how far a prompt may
    travel, and the interview prompt registry. The optional recorder turns each call into an
    `LLMRun`; without one the adapter still works, it just leaves no telemetry — right for a
    unit test of composition. `user_id` attributes a recorded run to the account whose
    evidence the prompt bore.
    """

    def __init__(self, *, router: LLMRouter, policy: RoutingPolicy,
                 prompts: PromptRegistry | None = None,
                 recorder: LLMTelemetryRecorder | None = None,
                 user_id: Any = None) -> None:
        self._router = router
        self._policy = policy
        self._prompts = prompts if prompts is not None else interview_prompt_registry()
        self._recorder = recorder
        self._user_id = user_id

    @property
    def key(self) -> str:
        return LLM_INTERVIEW_KEY

    async def propose_plan(self, *, context: InterviewContext,
                           mode: InterviewMode) -> InterviewPlan:
        """The coverage plan for one session, re-validated against `InterviewPlan` (§20-25).

        The model proposes topics; the layer stamps the session's `mode` and re-validates the
        whole against the domain — so a topic with an unknown `question_type`, a repeated
        topic, or an empty list fails to parse into a plan, exactly as it would if a test
        built one by hand. The mode is the platform's, never the model's: it is merged in
        here, not read from the payload.
        """
        payload = "\n\n".join((
            context.render_candidate(),
            context.render_role(),
            f"=== TASK ===\nDesign the coverage plan for a {mode.value} mock interview.",
        ))
        response = await self._run(PromptName.INTERVIEW_PLAN, payload)
        data = self._json_object(response)
        return self._validate({**data, "mode": mode}, InterviewPlan)

    async def generate_question(self, *, context: InterviewContext, mode: InterviewMode,
                                question_type: InterviewQuestionType,
                                difficulty: InterviewDifficulty,
                                topic_label: str | None = None,
                                already_asked: Sequence[str] = ()) -> ProposedQuestion:
        """One proposed question at the engine's requested type and difficulty (§8-9, §64-72).

        The engine decides the type, the difficulty rung and the topic; the model writes the
        prompt. `already_asked` lets the model avoid repeating a question it has posed — a
        convenience for the model, not a guarantee the engine relies on. The return is an
        identity-free `ProposedQuestion`; the engine assigns the id, sequence and depth.
        """
        task = [
            "=== TASK ===",
            f"Ask ONE {question_type.value} question at {difficulty.value} difficulty "
            f"for this {mode.value} interview.",
        ]
        if topic_label:
            task.append(f"Focus on the topic: {topic_label}.")
        if already_asked:
            task.append("Do not repeat any of these already-asked questions:")
            task += [f"- {prompt}" for prompt in already_asked]
        payload = "\n\n".join((
            context.render_candidate(),
            context.render_role(),
            "\n".join(task),
        ))
        response = await self._run(PromptName.INTERVIEW_QUESTION, payload)
        return self._validate(self._json_object(response), ProposedQuestion)

    async def evaluate_answer(self, *, context: InterviewContext,
                              question: InterviewQuestion, answer: InterviewAnswer,
                              evaluated_at: datetime) -> InterviewAnswerEvaluation:
        """A full `InterviewAnswerEvaluation` for one answer — but never a readiness (§33, §93).

        The load-bearing method. The model returns per-axis grades and coaching prose; the
        layer merges in the fields it — not the model — owns (the deterministic id, the
        answer/session/user the evaluation belongs to, the evaluator key, the timestamp) and
        re-validates the whole against `InterviewAnswerEvaluation`. Because that schema has no
        readiness field and forbids extras, a payload carrying a `readiness`, a `probability`
        or a hiring `verdict` fails to parse here — the model cannot author readiness, the
        platform computes it later from a whole session. Platform-owned keys are applied
        *after* the model's, so a provider cannot forge its own id or backdate a run.
        """
        payload = "\n\n".join((
            context.render_candidate(),
            context.render_role(),
            "=== QUESTION ASKED ===",
            question.prompt,
            "=== CANDIDATE ANSWER ===",
            answer.content,
            "=== TASK ===\nGrade this answer on the dimensions you can judge.",
        ))
        response = await self._run(PromptName.INTERVIEW_EVALUATION, payload)
        data = self._json_object(response)
        owned = {
            "id": interview_answer_evaluation_id(answer.id),
            "answer_id": answer.id,
            "session_id": answer.session_id,
            "user_id": answer.user_id,
            "evaluator_key": self.key,
            "evaluated_at": evaluated_at,
        }
        return self._validate({**data, **owned}, InterviewAnswerEvaluation)

    async def decide_follow_up(self, *, context: InterviewContext,
                               question: InterviewQuestion,
                               answer: InterviewAnswer) -> FollowUpDecision:
        """Whether this answer warrants a follow-up, and the question if so (§26-29).

        The model sees the question and the candidate's own answer — the answer is the
        candidate's words, so a follow-up that quotes it invents nothing. The role grounds the
        probe; the decision is an identity-free `FollowUpDecision` whose invariant guarantees
        a prompt and a type whenever it says to ask, so the engine never faces "ask, but no
        question". The engine still owns whether depth allows the follow-up at all (§28).
        """
        payload = "\n\n".join((
            context.render_role(),
            "=== QUESTION ASKED ===",
            question.prompt,
            "=== CANDIDATE ANSWER ===",
            answer.content,
            "=== TASK ===\nDecide whether a single follow-up question is warranted.",
        ))
        response = await self._run(PromptName.INTERVIEW_FOLLOW_UP, payload)
        return self._validate(self._json_object(response), FollowUpDecision)

    async def generate_summary(self, *, context: InterviewContext, mode: InterviewMode,
                               transcript: str) -> ProposedSummary:
        """The closing coaching prose for a session — the readiness is paired in by the service.

        The model is given a rendered transcript of the practice (the questions and answers)
        and writes a headline, strengths and focus areas *about the practice*. It is never
        given or asked for a readiness: the platform has already computed one from the grades,
        and the service pairs it with this prose. Every returned string is run through the
        evidence guard before anything is persisted (§40-44).
        """
        payload = "\n\n".join((
            context.render_candidate(),
            context.render_role(),
            f"=== PRACTICE TRANSCRIPT ({mode.value}) ===",
            transcript,
            "=== TASK ===\nWrite the closing coaching summary for this practice session.",
        ))
        response = await self._run(PromptName.INTERVIEW_SUMMARY, payload)
        return self._validate(self._json_object(response), ProposedSummary)

    # --- routing and re-validation -----------------------------------------

    async def _run(self, name: PromptName, payload: str) -> LLMResponse:
        """Render the named prompt around the payload, route it, and return the answer.

        The recorder wraps the router (it does not replace it), so telemetry being on or off
        changes nothing about which provider serves or how a failure propagates — an
        `LLMError` from routing is re-raised unchanged for the service to map. Rendering here,
        from a `PromptName`, keeps the version the registry serves recorded on the run.
        """
        request = self._prompts.get(name).render(user_content=payload)
        if self._recorder is not None:
            outcome = await self._recorder.route(
                self._router, request, self._policy, user_id=self._user_id)
        else:
            outcome = await self._router.route(request, self._policy)
        return outcome.response

    def _validate(self, data: Any, model: type[_T]) -> _T:
        """Re-validate a payload against a domain model or a proposal (§42).

        The model class is the schema that matters: `extra="forbid"` rejects an invented
        field — most pointedly a `readiness` on an `InterviewAnswerEvaluation`, which has no
        such field — and every domain invariant runs here before the value is trusted. A
        payload that does not parse is a typed `STRUCTURED_OUTPUT_INVALID`; the
        `ValidationError` is *not* forwarded, because its message can quote the offending
        (untrusted) payload.
        """
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise LLMError(
                LLMFailureCode.STRUCTURED_OUTPUT_INVALID,
                detail=f"the model's output did not match the {model.__name__} schema "
                       f"({len(exc.errors())} field error(s))") from exc

    def _json_object(self, response: LLMResponse) -> dict[str, Any]:
        """The parsed JSON object to validate, or a typed failure when it is not an object.

        Every interview task's schema is a JSON object; a payload that parses to a list or a
        scalar is as invalid as one that does not parse at all, and both surface as the same
        typed `STRUCTURED_OUTPUT_INVALID` rather than an `AttributeError` deeper in.
        """
        data = self._json(response)
        if not isinstance(data, dict):
            raise LLMError(
                LLMFailureCode.STRUCTURED_OUTPUT_INVALID,
                detail="the model did not return a JSON object")
        return data

    @staticmethod
    def _json(response: LLMResponse) -> Any:
        """The structured payload: the parsed field, else the text parsed as JSON.

        A provider that populated `structured_output` (a native JSON mode) is preferred;
        otherwise the text is parsed as JSON, which is what a CLI or a plain completion
        returns. Text that is not JSON at all is a `STRUCTURED_OUTPUT_INVALID`, never an
        uncaught `JSONDecodeError` — the failure a caller sees is always typed.
        """
        if response.structured_output is not None:
            return dict(response.structured_output)
        try:
            return json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise LLMError(
                LLMFailureCode.STRUCTURED_OUTPUT_INVALID,
                detail="the model did not return parseable JSON") from exc




