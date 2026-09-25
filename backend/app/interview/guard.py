"""The interview coaching truth gate — reusing the Phase 10 evidence guard (§40-44).

Every candidate-facing sentence the simulator produces is coaching *about* the candidate:
an evaluation's strengths, its improvements, a dimension's notes, a drafted better answer,
a session summary's headline and focus areas. None of it is a claim the candidate is making
on a résumé, so none of it carries evidence citations — but all of it could, if a provider
misbehaved, put a fact in the candidate's mouth that their evidence does not support. That
is the exact risk the Phase 10 `CandidateEvidenceGuard` was built to catch, so this module
reuses it rather than writing a second, weaker gate.

The reuse is deliberate and total: coaching prose runs through
`CandidateEvidenceGuard.review_supporting_prose`, which applies the two citation-free gates
(`INVENTED_NUMBER`, `INVENTED_TERM`) against the candidate's whole evidence corpus with the
very same tokenizer and term universe the résumé gates use. A number or a claimable skill
the candidate's evidence never mentions is caught here identically to how it would be caught
on a generated résumé (§40-44). The gate is pure and deterministic — no I/O, no model — so
the service runs it *before* persisting anything: an evaluation or a summary that fails is
rejected whole, never stored half-guarded.
"""
from backend.app.documents.guard import CandidateEvidenceGuard
from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.documents import DocumentGuardReport, DocumentGuardViolation
from backend.app.domain.interview import (
    InterviewAnswerEvaluation,
    InterviewSessionSummary,
)
from backend.app.domain.opportunity import Opportunity


class InterviewCoachingGuard:
    """Reviews an evaluation's or a summary's coaching prose against the candidate's evidence.

    Stateless and reusable: hold one and call `review_evaluation` / `review_summary` per
    artefact. Wraps a `CandidateEvidenceGuard` (the same one the document layer uses) so the
    truth rule is enforced by one implementation, not two that could drift.
    """

    def __init__(self, guard: CandidateEvidenceGuard | None = None) -> None:
        self._guard = guard if guard is not None else CandidateEvidenceGuard()

    def review_evaluation(self, evaluation: InterviewAnswerEvaluation, *,
                          profile: CandidateProfile,
                          opportunity: Opportunity) -> DocumentGuardReport:
        """The verdict on one answer evaluation's prose (§40-44).

        Gathers every candidate-facing string the evaluation carries — each dimension's
        coaching notes, the strengths, the improvements and the drafted `suggested_answer`,
        the single most dangerous field because it speaks *as* the candidate — and runs each
        through the shared guard. Collects all violations, so a rejected evaluation reports
        everything wrong at once, exactly as the document guard does.
        """
        return self._review_texts(self._evaluation_texts(evaluation),
                                   profile=profile, opportunity=opportunity)

    def review_summary(self, summary: InterviewSessionSummary, *,
                       profile: CandidateProfile,
                       opportunity: Opportunity) -> DocumentGuardReport:
        """The verdict on one session summary's prose — headline, strengths, focus areas."""
        return self._review_texts(self._summary_texts(summary),
                                  profile=profile, opportunity=opportunity)

    def review_question(self, prompt: str, *, profile: CandidateProfile,
                        opportunity: Opportunity) -> DocumentGuardReport:
        """The verdict on a generated question's text, *before* it is persisted (§10-17).

        A question is not coaching prose about the candidate — it is grounded in the posting,
        and may legitimately reference a skill or a number the posting states ("the posting
        mentions Kafka; how would you approach it?"). So the posting text rides in as
        `extra_corpus`, widening the allowed pool beyond the candidate's own evidence: a term
        or number grounded in *either* the candidate or the posting passes, and one grounded
        in neither — a fabrication invented from nowhere — is caught. The guard cannot tell
        "the posting mentions Kafka" from "you have Kafka experience"; that attribution is the
        question prompt's defence in depth, while this deterministic gate stays authoritative
        for the invented-from-nowhere case. Run before persistence, so a question that fails
        is regenerated or refused, never stored.
        """
        violations = self._guard.review_supporting_prose(
            prompt, profile=profile, opportunity=opportunity, label="question",
            extra_corpus=self._posting_corpus(opportunity))
        if violations:
            return DocumentGuardReport(ok=False, violations=tuple(violations))
        return DocumentGuardReport(ok=True)

    def _review_texts(self, texts: list[tuple[str, str]], *,
                      profile: CandidateProfile,
                      opportunity: Opportunity) -> DocumentGuardReport:
        violations: list[DocumentGuardViolation] = []
        for text, label in texts:
            violations += self._guard.review_supporting_prose(
                text, profile=profile, opportunity=opportunity, label=label)
        if violations:
            return DocumentGuardReport(ok=False, violations=tuple(violations))
        return DocumentGuardReport(ok=True)

    @staticmethod
    def _evaluation_texts(
            evaluation: InterviewAnswerEvaluation) -> list[tuple[str, str]]:
        """Every candidate-facing string in an evaluation, each with a human label."""
        texts: list[tuple[str, str]] = []
        for entry in evaluation.dimensions:
            for index, note in enumerate(entry.notes, start=1):
                texts.append((note, f"{entry.dimension.value.lower()} note {index}"))
        for index, strength in enumerate(evaluation.strengths, start=1):
            texts.append((strength, f"strength {index}"))
        for index, improvement in enumerate(evaluation.improvements, start=1):
            texts.append((improvement, f"improvement {index}"))
        if evaluation.suggested_answer is not None:
            texts.append((evaluation.suggested_answer, "suggested answer"))
        return texts

    @staticmethod
    def _summary_texts(summary: InterviewSessionSummary) -> list[tuple[str, str]]:
        """Every candidate-facing string in a session summary, each with a human label."""
        texts: list[tuple[str, str]] = [(summary.headline, "headline")]
        for index, strength in enumerate(summary.strengths, start=1):
            texts.append((strength, f"strength {index}"))
        for index, focus in enumerate(summary.focus_areas, start=1):
            texts.append((focus, f"focus area {index}"))
        return texts

    @staticmethod
    def _posting_corpus(opportunity: Opportunity) -> str:
        """The posting's own words — the legitimately citable ground a question may stand on.

        Company, title, each sought skill and the description, mirroring what
        `InterviewContext.render_role` shows the model, so the corpus the guard widens by
        matches the reference the question was generated against.
        """
        parts: list[str] = [opportunity.company_name, opportunity.title]
        parts += [req.skill for req in opportunity.skill_requirements]
        if opportunity.description:
            parts.append(opportunity.description)
        return "\n".join(parts)
