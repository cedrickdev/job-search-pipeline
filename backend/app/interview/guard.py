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
import re

from backend.app.documents.guard import (
    _NUMERIC_TOKEN_RE,
    CandidateEvidenceGuard,
    _term_pattern,
    numeric_tokens,
)
from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.documents import (
    DocumentGuardReport,
    DocumentGuardViolation,
    DocumentViolationCode,
)
from backend.app.domain.interview import (
    InterviewAnswerEvaluation,
    InterviewSessionSummary,
)
from backend.app.domain.opportunity import Opportunity

# --- attribution vocabulary (the posting-fact vs candidate-fact distinction) ------
#
# A question may stand on a fact the *posting* states, but it must not pin that fact on
# the candidate as their own experience unless the candidate's evidence backs it too. The
# tell is grammatical: a second-person *assertion of experience or possession*. These two
# small, deterministic vocabularies are what separate "the role uses Kafka; how would you
# approach it?" (a role reference — fine) from "tell me about your Kafka experience" or
# "you managed a team of 20" (attribution — a fabrication when only the posting says so).

# Second-person verbs that credit the candidate with having *done* or *held* the thing that
# follows. Kept to experience/possession/achievement verbs on purpose: a copula ("you are
# curious about Kafka") or a perception verb ("you know of Kafka") asserts no experience, so
# it must not trip the gate. Present and past forms both, because "you manage" and "you
# managed" attribute alike.
_ACHIEVEMENT_VERBS: frozenset[str] = frozenset({
    "have", "had", "own", "owned", "hold", "held", "possess", "possessed",
    "manage", "managed", "lead", "led", "build", "built", "create", "created",
    "design", "designed", "develop", "developed", "deliver", "delivered",
    "implement", "implemented", "architect", "architected", "maintain", "maintained",
    "run", "ran", "handle", "handled", "use", "used", "utilise", "utilised",
    "utilize", "utilized", "work", "worked", "scale", "scaled", "oversee", "oversaw",
    "mentor", "mentored", "launch", "launched", "ship", "shipped", "drive", "drove",
    "spearhead", "spearheaded", "establish", "established", "grow", "grew", "reduce",
    "reduced", "increase", "increased", "improve", "improved", "achieve", "achieved",
    "write", "wrote", "deploy", "deployed", "migrate", "migrated", "integrate",
    "integrated", "optimise", "optimised", "optimize", "optimized", "automate",
    "automated", "supervise", "supervised", "direct", "directed", "found", "founded",
    "produce", "produced", "coordinate", "coordinated", "operate", "operated",
})

# Tokens that, sitting immediately before "you", turn the clause hypothetical or
# interrogative — a question about approach or a conditional, not a claim of what the
# candidate did: "how *would* you use…", "*do* you use…", "*if* you led…". When one of
# these precedes "you", the second-person verb that follows is not an assertion.
_HYPOTHETICAL_BEFORE_YOU: frozenset[str] = frozenset({
    "would", "could", "might", "should", "may", "will", "shall", "do", "does", "did",
    "how", "if", "when", "whenever", "were", "to", "whether", "that", "suppose",
    "imagine", "hypothetically", "not",
})

# "you <achievement verb>" — a second-person assertion of experience.
_YOU_VERB_RE = re.compile(
    r"\byou\s+(?:" + "|".join(sorted(_ACHIEVEMENT_VERBS)) + r")\b")
# The last word of a fragment, so the token immediately before "you" can be inspected.
_LAST_WORD_RE = re.compile(r"([\w'’-]+)\s*$")
# "your <up to four words> " ending right where a fact begins — a possessive that binds
# the fact to the candidate ("your extensive Kafka experience", "your team of 20").
_POSSESSIVE_TAIL_RE = re.compile(r"\byour\b(?:\s+[\w'’-]+){0,4}\s+$")
# Sentence boundaries: terminal punctuation or a line break. Interview prompts are short,
# so a clause is the right unit — attribution binds within one, not across a paragraph.
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")


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
        mentions Kafka; how would you approach it?"). Two deterministic gates run, and both
        must pass:

        1. **Invented from nowhere.** The posting text rides in as `extra_corpus`, widening the
           allowed pool beyond the candidate's own evidence: a term or number grounded in
           *either* the candidate or the posting passes, and one grounded in neither is a
           fabrication and is caught (`INVENTED_NUMBER` / `INVENTED_TERM`).

        2. **Posting fact pinned on the candidate.** The first gate cannot tell "the posting
           mentions Kafka" from "you have Kafka experience" — both cite a term the posting
           carries. This gate can: it flags a *posting-only* fact (present in the posting,
           absent from the candidate's evidence) that the question attributes to the candidate
           through a possessive ("your Kafka experience") or a second-person assertion of
           experience ("you managed a team of 20"), while letting a role reference or a
           hypothetical ("the role runs a team of 20", "how would you approach it") pass
           (`MISATTRIBUTED_TO_CANDIDATE`). The distinction is enforced here, in code — not left
           to the prompt.

        Run before persistence, so a question that fails is regenerated or refused, never
        stored.
        """
        violations = list(self._guard.review_supporting_prose(
            prompt, profile=profile, opportunity=opportunity, label="question",
            extra_corpus=self._posting_corpus(opportunity)))
        violations += self._attribution_violations(
            prompt, profile=profile, opportunity=opportunity)
        if violations:
            return DocumentGuardReport(ok=False, violations=tuple(violations))
        return DocumentGuardReport(ok=True)

    def _attribution_violations(self, prompt: str, *, profile: CandidateProfile,
                                opportunity: Opportunity
                                ) -> list[DocumentGuardViolation]:
        """MISATTRIBUTED_TO_CANDIDATE: a posting-only fact pinned on the candidate.

        Works sentence by sentence (attribution binds within one clause, not across a
        paragraph). For each sentence it takes the posting-only facts that appear in it — the
        skill terms and numbers the posting states but the candidate's evidence does not — and
        flags any that the sentence attributes to the candidate. A fact grounded in the
        candidate's own evidence is never posting-only, so a genuine "you led a team of 12"
        that the evidence backs is left alone; a fact grounded in neither is the previous
        gate's business, not this one's.
        """
        candidate_corpus = self._guard.evidence_corpus(profile)
        posting_corpus = self._posting_corpus(opportunity).lower()
        posting_only_terms = self._posting_only_terms(
            profile, opportunity, candidate_corpus, posting_corpus)
        posting_only_numbers = (numeric_tokens(posting_corpus)
                                - numeric_tokens(candidate_corpus))
        if not posting_only_terms and not posting_only_numbers:
            return []

        violations: list[DocumentGuardViolation] = []
        for sentence in self._sentences(prompt):
            lowered = sentence.lower()
            for term in posting_only_terms:
                for match in _term_pattern(term).finditer(lowered):
                    if self._attributes_to_candidate(lowered, match.start()):
                        violations.append(self._misattribution(sentence, term))
                        break
            for match in _NUMERIC_TOKEN_RE.finditer(lowered):
                token = next(iter(numeric_tokens(match.group())), None)
                if token is not None and token in posting_only_numbers \
                        and self._attributes_to_candidate(lowered, match.start()):
                    violations.append(self._misattribution(sentence, match.group().strip()))
        return violations

    def _posting_only_terms(self, profile: CandidateProfile, opportunity: Opportunity,
                            candidate_corpus: str, posting_corpus: str) -> tuple[str, ...]:
        """Claimable terms the posting carries but the candidate's evidence does not.

        Drawn from the very term universe the `INVENTED_TERM` rule uses, so the two agree on
        what counts as a claimable qualification. A term found in the candidate's own corpus
        is theirs to claim and is excluded; one found only in the posting is the fact this
        gate guards against mis-attributing.
        """
        posting_only: list[str] = []
        for term in self._guard.term_universe(profile, opportunity):
            pattern = _term_pattern(term)
            if pattern.search(posting_corpus) and not pattern.search(candidate_corpus):
                posting_only.append(term)
        return tuple(posting_only)

    def _attributes_to_candidate(self, sentence_lower: str, fact_start: int) -> bool:
        """Does the clause pin the fact at `fact_start` on the candidate as their own?

        Two second-person patterns count as attribution, mirroring how a person reads the
        sentence:

        - **Possessive** — "your …" ending right at the fact ("your extensive Kafka
          experience", "your team of 20"), within a short window so an unrelated "your
          thoughts on the role's use of Kafka" does not bind.
        - **Assertion of experience** — "you <achievement verb>" somewhere in the clause,
          where "you" is not preceded by a hypothetical or interrogative marker. That marker
          check is what lets "how would you use Kafka" and "do you know Kafka" through while
          catching "you used Kafka" and "you have Kafka expertise".
        """
        before = sentence_lower[:fact_start]
        if _POSSESSIVE_TAIL_RE.search(before):
            return True
        for match in _YOU_VERB_RE.finditer(sentence_lower):
            preceding = _LAST_WORD_RE.search(sentence_lower[:match.start()])
            if preceding is None or preceding.group(1) not in _HYPOTHETICAL_BEFORE_YOU:
                return True
        return False

    @staticmethod
    def _misattribution(sentence: str, fact: str) -> DocumentGuardViolation:
        return DocumentGuardViolation(
            code=DocumentViolationCode.MISATTRIBUTED_TO_CANDIDATE,
            detail=f"question: attributes to the candidate a fact only the posting "
                   f"supports: {fact}",
            offending_text=sentence)

    @staticmethod
    def _sentences(prompt: str) -> list[str]:
        return [part.strip() for part in _SENTENCE_SPLIT_RE.split(prompt) if part.strip()]

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
