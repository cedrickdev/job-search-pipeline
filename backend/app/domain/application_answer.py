"""Form questions, the model's proposed answers, and the answers a form is filled with.

The application engine's version of the same boundary the eligibility layer draws
between `LLM_EXTRACTION` and a decided verdict (`backend.app.domain.eligibility`):
a model may *read* a form and *propose* an answer, but a proposal is not an answer,
and the step from one to the other is deterministic, testable, and refuses to guess
(§28-29). `ApplicationAnswerProposal` is what a generator hands back;
`ApplicationAnswer` is what an adapter is allowed to type into a field; and
`resolve_answer` is the only bridge between them.

Two rules are structural rather than left to a service to remember (§25-26):

- a required question with no trustworthy answer never becomes `N/A`, `0`, `Yes` or
  `No` — it becomes `UNKNOWN_REQUIRED_FIELD` and the run stops for a human;
- a sensitive, demographic or legal question is never answered from a profile field
  or a model proposal — it is `SENSITIVE_QUESTION`, always, because a protected
  characteristic must not be inferred and a legal attestation is the candidate's to
  make.
"""
from enum import StrEnum
from typing import Annotated

from pydantic import Field

from backend.app.domain.application_channel import HumanRequiredReason
from backend.app.domain.base import DomainModel, NonEmptyStr, Score
from backend.app.domain.identifiers import EvidenceId


class QuestionSensitivity(StrEnum):
    """How freely a question may be answered without a human (§26).

    `NORMAL` is the only value an automated answer may fill. The other three each
    name a reason the platform refuses to answer on the candidate's behalf, and all
    three route to the same `SENSITIVE_QUESTION` hand-off — the distinction is kept
    so a reviewer sees *why* a field was held back, not merged away.

    - `NORMAL` — an ordinary field (years of experience, notice period);
    - `SENSITIVE` — private but not protected (salary expectation, health note);
    - `DEMOGRAPHIC` — a protected characteristic (§26: never inferred);
    - `LEGAL` — an attestation with legal weight (work-authorization declaration, a
      signed statement) that only the candidate may make.
    """

    NORMAL = "NORMAL"
    SENSITIVE = "SENSITIVE"
    DEMOGRAPHIC = "DEMOGRAPHIC"
    LEGAL = "LEGAL"

    @property
    def needs_human(self) -> bool:
        """Whether a field of this sensitivity must be left for a person."""
        return self is not QuestionSensitivity.NORMAL


class ApplicationQuestionKind(StrEnum):
    """The shape of the field a question is asking for.

    Named so an adapter knows how to fill it and a validator knows what a valid
    answer looks like — a `SINGLE_CHOICE` answer must be one of the options, a
    `FILE_UPLOAD` is satisfied by a pinned document rather than typed text. The set
    is deliberately small; a field a form presents that maps to none of these is a
    reason to stop for a human, not to invent a member.
    """

    SHORT_TEXT = "SHORT_TEXT"
    LONG_TEXT = "LONG_TEXT"
    BOOLEAN = "BOOLEAN"
    SINGLE_CHOICE = "SINGLE_CHOICE"
    MULTIPLE_CHOICE = "MULTIPLE_CHOICE"
    NUMBER = "NUMBER"
    DATE = "DATE"
    FILE_UPLOAD = "FILE_UPLOAD"


class AnswerProvenance(StrEnum):
    """Where the value in an answer came from, which is an audit fact (§29, §41).

    Ordered loosely by how much the platform trusts it to fill a field unattended.
    `LLM_PROPOSAL` is the weakest and, like `LLM_EXTRACTION` in eligibility, is never
    allowed to be the provenance of a *filled* answer on its own — a proposal is
    reviewed or backed by evidence before it becomes an `ApplicationAnswer`.

    - `CANDIDATE_PROFILE` — copied from a fact the profile already holds;
    - `CANDIDATE_DECLARATION` — a value the candidate stated for applications;
    - `HUMAN_SUPPLIED` — a person typed it during review;
    - `LLM_PROPOSAL` — a model suggested it; a suggestion, not yet an answer.
    """

    CANDIDATE_PROFILE = "CANDIDATE_PROFILE"
    CANDIDATE_DECLARATION = "CANDIDATE_DECLARATION"
    HUMAN_SUPPLIED = "HUMAN_SUPPLIED"
    LLM_PROPOSAL = "LLM_PROPOSAL"


class ApplicationQuestion(DomainModel):
    """One field a form asks the candidate to fill (§24).

    `key` is the adapter's stable handle for the field (a form input name, an ATS
    question id); `label` is what the human reads. `sensitivity` is what decides
    whether the platform may answer at all, and `required` whether leaving it empty
    blocks submission. `options` is required to be non-empty for a choice question
    and empty otherwise, so a `SINGLE_CHOICE` with no options — which nothing could
    validly answer — cannot be constructed.
    """

    key: NonEmptyStr
    label: NonEmptyStr
    kind: ApplicationQuestionKind
    required: bool = True
    sensitivity: QuestionSensitivity = QuestionSensitivity.NORMAL
    options: tuple[NonEmptyStr, ...] = ()

    @property
    def is_choice(self) -> bool:
        return self.kind in (ApplicationQuestionKind.SINGLE_CHOICE,
                             ApplicationQuestionKind.MULTIPLE_CHOICE)


class ApplicationAnswerProposal(DomainModel):
    """A proposed answer to one question — a suggestion the engine must still weigh.

    This is the typed action a generator returns (§28): it names the question by
    `key`, offers a `value` (or `None` for "I have nothing trustworthy for this"),
    states where the value came from and, for a model proposal, how confident it is
    and which candidate evidence backs it. It is deliberately inert — nothing fills a
    field from a proposal; `resolve_answer` decides what, if anything, becomes an
    `ApplicationAnswer`.
    """

    question_key: NonEmptyStr
    value: NonEmptyStr | None = None
    provenance: AnswerProvenance
    confidence: Score | None = None
    evidence_ids: tuple[EvidenceId, ...] = ()


class ApplicationAnswer(DomainModel):
    """A value an adapter is cleared to type into a field.

    The existence of one of these *is* the statement that the field may be filled
    automatically: it always carries a real `value` (there is no empty answer — an
    absent answer is the absence of this object), and its `provenance` is never
    `LLM_PROPOSAL`, because a model suggestion only reaches a field after a human or
    evidence has backed it. `resolve_answer` is the only place these are minted, so
    those two invariants hold everywhere an answer is used.
    """

    question_key: NonEmptyStr
    value: NonEmptyStr
    provenance: AnswerProvenance
    evidence_ids: tuple[EvidenceId, ...] = ()


# The confidence a model proposal must reach before it may fill a field without a
# human. Below it, the run stops rather than typing a value it is not sure of — a
# deliberately conservative default (§28-29), stated once so no call site invents
# its own bar.
MIN_LLM_ANSWER_CONFIDENCE: Annotated[float, Field(ge=0.0, le=1.0)] = 0.8


def resolve_answer(
    question: ApplicationQuestion,
    proposal: ApplicationAnswerProposal | None,
) -> ApplicationAnswer | HumanRequiredReason | None:
    """Turn a question and its proposal into an answer, a hand-off, or nothing.

    The one deterministic bridge from a proposal to a fillable answer, and the
    place §25-29 are enforced. It returns exactly one of three things:

    - a `HumanRequiredReason`, when the field cannot be answered automatically —
      `SENSITIVE_QUESTION` for anything not `NORMAL` (checked first, so no sensitive
      field is ever filled from a profile value); `UNKNOWN_REQUIRED_FIELD` for a
      required field with no trustworthy answer (§25: never guessed as N/A/0/Yes/No);
    - an `ApplicationAnswer`, when there is a real value the platform may fill —
      a model proposal only qualifies when it clears `MIN_LLM_ANSWER_CONFIDENCE`,
      and it is recorded with its own provenance so the audit trail says a model
      supplied it;
    - `None`, when an *optional* field simply has no answer and may be left blank.

    A model proposal that is too unsure is treated as no answer at all: a required
    field then becomes `UNKNOWN_REQUIRED_FIELD`, an optional one is left blank.
    """
    if question.sensitivity.needs_human:
        return HumanRequiredReason.SENSITIVE_QUESTION

    usable = _usable_value(proposal)
    if usable is None:
        return (HumanRequiredReason.UNKNOWN_REQUIRED_FIELD
                if question.required else None)

    value, provenance, evidence_ids = usable
    return ApplicationAnswer(
        question_key=question.key,
        value=value,
        provenance=provenance,
        evidence_ids=evidence_ids,
    )


def _usable_value(
    proposal: ApplicationAnswerProposal | None,
) -> tuple[str, AnswerProvenance, tuple[EvidenceId, ...]] | None:
    """The value a proposal may fill a field with, or `None` if it may not.

    A missing proposal or a proposal with no value is nothing. A model proposal is
    nothing unless it clears the confidence bar; a proposal from any other source is
    trusted, because a profile fact, a declaration or a human's input is not a guess.
    """
    if proposal is None or proposal.value is None:
        return None
    if proposal.provenance is AnswerProvenance.LLM_PROPOSAL:
        if proposal.confidence is None \
                or proposal.confidence < MIN_LLM_ANSWER_CONFIDENCE:
            return None
    return proposal.value, proposal.provenance, proposal.evidence_ids
