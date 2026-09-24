"""The `CandidateEvidenceGuard` — the V2 truth gate over generated documents.

This is the module CLAUDE.md's central rule rests on, and it is a faithful port
of V1's `pipeline.tailor_io.truth_violations` from CV YAML onto the structured
`DocumentContent` domain. The rule is unchanged: *the platform may rewrite,
reorder, shorten, emphasize or omit truthful candidate information, but it may
never invent new candidate facts.* What changes is the substrate. V1 checked
tailored YAML against a base-CV dict; V2 checks a `ResumeDocument` /
`CoverLetterDocument` against the candidate's `CandidateEvidence` and
`CandidateClaim` records.

Four gates, each a way generated text could assert something the evidence does
not carry (`DocumentViolationCode`):

- **UNKNOWN_EVIDENCE** — a line cites an evidence id the profile does not hold.
  The type system guarantees a line cites *something* (`EvidenceBackedText`
  requires it); the guard guarantees the something is *real*. This is V1's
  "unknown bullet id" gate.
- **INVENTED_NUMBER** — a numeric token in a line is absent from the evidence that
  line cites. This is V1's per-language numeric-multiset rule, using the very same
  `numeric_tokens` tokenizer so French "2,5 M€" and "€2.5M" still compare equal.
- **INVENTED_TERM** — a claimable hard-skill term (from the skill ontology, the
  posting's requirements, or the candidate's own claims) appears in a line but is
  found nowhere in the candidate's evidence corpus. This is V1's base-library
  qualification rule.
- **UNSUPPORTED_SKILL** — a skill listed on the résumé does not match any of the
  candidate's `SKILL` claims, compared through the deterministic skill ontology so
  "Node.js" and "node" are one skill. This is V1's skill-library rule.
- **ALTERED_IDENTITY** — the name on the document is not the candidate's own. V1
  froze contact/identity verbatim; the structured form keeps the candidate's
  `display_name` and refuses a substitution.

The guard is pure and deterministic — no I/O, no LLM, no clock — so a service can
run it before touching a database, giving the same no-partial-state guarantee V1
has: nothing is stored or rendered until the content clears every gate.
"""
import re
from collections import Counter
from collections.abc import Iterable, Mapping

from backend.app.domain.candidate import CandidateProfile, ClaimType
from backend.app.domain.documents import (
    CoverLetterDocument,
    DocumentContent,
    DocumentGuardReport,
    DocumentGuardViolation,
    DocumentViolationCode,
    EvidenceBackedText,
    ResumeDocument,
    ResumeEntry,
    ResumeSkillGroup,
)
from backend.app.domain.identifiers import EvidenceId
from backend.app.domain.opportunity import Opportunity
from backend.app.matching.skill_ontology import SKILL_ONTOLOGY, SkillOntology
from backend.app.matching.skills import normalize_skill

# The very tokenizer V1 uses (`pipeline.tailor_io`), copied rather than imported so
# the backend domain carries no dependency on the V1 pipeline package. Digit runs
# with attached currency symbols, decimal separators, percent signs and magnitude
# suffixes: €2.5M, 2,5 M€, 80%, 4. Keeping it identical is what lets a fact that
# passed V1 pass here unchanged.
_NUMERIC_TOKEN_RE = re.compile(
    r"[€$£]?\d+(?:[.,]\d+)*"
    r"(?:\s?[KMBkmb](?![A-Za-z]))?"
    r"(?:\s?[€$£%])?")


def numeric_tokens(text: str) -> Counter[str]:
    """Multiset of numeric tokens in `text` (V1 spec §4).

    Narrow and non-breaking spaces are normalized first so French number
    formatting compares equal, then every token is upper-cased and stripped of
    inner spaces. A multiset, not a set: writing "3 to 5 years" when the evidence
    says "3 years" invents the 5, and only counting occurrences catches it.
    """
    text = text.replace(" ", " ").replace(" ", " ")
    return Counter(match.group().replace(" ", "").upper()
                   for match in _NUMERIC_TOKEN_RE.finditer(text))


def _term_pattern(term: str) -> re.Pattern[str]:
    """Case-insensitive word-boundary pattern for a hard-skill term.

    Tolerates a trailing plural 's' (texts are lower-cased before matching), and
    uses non-alphanumeric boundaries rather than `\\b` so "c++" and "c#" match
    without their punctuation being treated as a word edge. Identical to V1's
    `_term_pattern`.
    """
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"s?(?![A-Za-z0-9])")


class CandidateEvidenceGuard:
    """Reviews generated document content against a candidate's evidence.

    Stateless and reusable: hold one and call `review` per document. The skill
    ontology is injected so a test — or a future Country Pack vocabulary — can
    supply its own without this module reaching for a global.
    """

    def __init__(self, ontology: SkillOntology = SKILL_ONTOLOGY) -> None:
        self._ontology = ontology

    def review(self, content: DocumentContent, *, profile: CandidateProfile,
               opportunity: Opportunity) -> DocumentGuardReport:
        """The verdict on one document's content.

        Runs every gate and collects *all* violations rather than stopping at the
        first: a candidate fixing a rejected draft should see everything wrong at
        once, exactly as V1's `truth_violations` returns the full list.
        """
        violations: list[DocumentGuardViolation] = []
        held = {item.id: item for item in profile.evidence}
        corpus = self._evidence_corpus(profile)
        universe = self._term_universe(profile, opportunity)

        for text, label in self._checkable_texts(content):
            violations += self._citation_and_number_violations(text, held, label)
            violations += self._term_violations(text, corpus, universe, label)

        if isinstance(content, ResumeDocument):
            violations += self._skill_violations(content.skill_groups, profile)

        violations += self._identity_violations(content, profile)

        if violations:
            return DocumentGuardReport(ok=False, violations=tuple(violations))
        return DocumentGuardReport(ok=True)

    def review_supporting_prose(
            self, text: str, *, profile: CandidateProfile,
            opportunity: Opportunity, label: str = "text"
            ) -> tuple[DocumentGuardViolation, ...]:
        """Truth gate for un-cited coaching prose (Phase 14 §40-44, reusing this guard).

        A résumé line carries its own evidence ids; interview coaching text — a suggested
        answer, a strength, a focus area — does not, because it is advice *about* the
        candidate rather than a claim the candidate is making. So this applies the two gates
        that do not need a citation, against the candidate's whole corpus rather than a cited
        subset: a number in the prose must appear *somewhere* in the candidate's evidence
        (INVENTED_NUMBER), and a claimable hard-skill term must appear there too
        (INVENTED_TERM). The corpus, the term universe and the tokenizer are exactly the ones
        the document gates use, so a fact that would pass on a résumé passes here and a
        fabrication is caught identically — the point of reusing this guard rather than
        writing a second, weaker one.

        Returns every violation rather than the first, so a caller can reject a whole
        coaching payload and report all of its problems at once. Pure and deterministic, so
        the interview service runs it before persisting a thing.
        """
        corpus = self._evidence_corpus(profile)
        universe = self._term_universe(profile, opportunity)
        issues: list[DocumentGuardViolation] = []

        allowed = numeric_tokens(corpus)
        invented = numeric_tokens(text) - allowed
        if invented:
            issues.append(DocumentGuardViolation(
                code=DocumentViolationCode.INVENTED_NUMBER,
                detail=f"{label}: numbers not supported by the candidate's evidence: "
                       + ", ".join(sorted(invented)),
                offending_text=text))

        lowered = text.lower()
        for term in universe:
            pattern = _term_pattern(term)
            if pattern.search(lowered) and not pattern.search(corpus):
                issues.append(DocumentGuardViolation(
                    code=DocumentViolationCode.INVENTED_TERM,
                    detail=f"{label}: qualification not supported by the candidate's "
                           f"evidence: {term}",
                    offending_text=text))
        return tuple(issues)

    # --- gate: citations exist and numbers are supported -------------------

    def _citation_and_number_violations(
            self, text: EvidenceBackedText,
            held: Mapping[EvidenceId, object], label: str
            ) -> list[DocumentGuardViolation]:
        """UNKNOWN_EVIDENCE and INVENTED_NUMBER for one evidence-backed line.

        The numbers rule is per-line and against *the evidence that line cites*,
        not the whole profile: that is what makes it V1's per-bullet multiset rule
        rather than a weaker "appears somewhere" check. An unknown citation is
        reported, and its (missing) text simply contributes nothing to the allowed
        pool, so an invented number riding on a bogus citation is still caught.
        """
        issues: list[DocumentGuardViolation] = []
        unknown = [eid for eid in text.evidence_ids if eid not in held]
        if unknown:
            issues.append(DocumentGuardViolation(
                code=DocumentViolationCode.UNKNOWN_EVIDENCE,
                detail=f"{label}: cites evidence absent from the profile: "
                       + ", ".join(str(eid) for eid in unknown),
                offending_text=text.text,
                evidence_ids=tuple(unknown)))
        allowed: Counter[str] = Counter()
        for eid in text.evidence_ids:
            record = held.get(eid)
            if record is not None:
                allowed += numeric_tokens(self._evidence_text(record))
        invented = numeric_tokens(text.text) - allowed
        if invented:
            issues.append(DocumentGuardViolation(
                code=DocumentViolationCode.INVENTED_NUMBER,
                detail=f"{label}: numbers not supported by cited evidence: "
                       + ", ".join(sorted(invented)),
                offending_text=text.text,
                evidence_ids=text.evidence_ids))
        return issues

    # --- gate: no invented hard-skill terms --------------------------------

    def _term_violations(self, text: EvidenceBackedText, corpus: str,
                         universe: Iterable[str], label: str
                         ) -> list[DocumentGuardViolation]:
        """INVENTED_TERM: a claimable qualification the evidence never mentions.

        Mirrors V1's rule exactly — a term in the universe of claimable skills
        that appears in the generated line but nowhere in the candidate's evidence
        corpus is invented. Terms *not* in the universe (ordinary prose, the
        employer's name, the role) are left alone; the guard polices claimable
        qualifications, not vocabulary.
        """
        issues: list[DocumentGuardViolation] = []
        lowered = text.text.lower()
        for term in universe:
            pattern = _term_pattern(term)
            if pattern.search(lowered) and not pattern.search(corpus):
                issues.append(DocumentGuardViolation(
                    code=DocumentViolationCode.INVENTED_TERM,
                    detail=f"{label}: qualification not supported by the "
                           f"candidate's evidence: {term}",
                    offending_text=text.text))
        return issues

    # --- gate: résumé skills rest on SKILL claims --------------------------

    def _skill_violations(self, groups: tuple[ResumeSkillGroup, ...],
                          profile: CandidateProfile
                          ) -> list[DocumentGuardViolation]:
        """UNSUPPORTED_SKILL: a listed skill matches no candidate SKILL claim.

        Compared through `normalize_skill`, so the candidate's claim of "Node.js"
        supports a résumé listing "node", and an unclaimed skill is caught however
        it is spelled. This is stricter than the term rule and specific to the
        skills section, where a bare list has no sentence to hide an unclaimed tool
        inside.
        """
        claimed = self._claimed_skills(profile)
        issues: list[DocumentGuardViolation] = []
        for group in groups:
            for raw in group.skills:
                normalized = normalize_skill(raw, self._ontology)
                if normalized is None:
                    continue
                if normalized.canonical not in claimed:
                    issues.append(DocumentGuardViolation(
                        code=DocumentViolationCode.UNSUPPORTED_SKILL,
                        detail=f"skill '{raw}' is not backed by any of the "
                               f"candidate's SKILL claims",
                        offending_text=raw))
        return issues

    # --- gate: identity is not rewritten -----------------------------------

    def _identity_violations(self, content: DocumentContent,
                             profile: CandidateProfile
                             ) -> list[DocumentGuardViolation]:
        """ALTERED_IDENTITY: the name on the document is not the candidate's.

        The one field V1 froze that survives structurally into V2. A résumé's
        `full_name` and a cover letter's `signature` must be the candidate's own
        `display_name`; a generator that "improved" the name would be inventing a
        person. Compared case- and whitespace-insensitively, so presentation
        differences ("  Ada Lovelace ") are not flagged.
        """
        expected = _fold(profile.display_name)
        if isinstance(content, ResumeDocument):
            name = content.full_name
        else:
            name = content.signature
        if _fold(name) != expected:
            return [DocumentGuardViolation(
                code=DocumentViolationCode.ALTERED_IDENTITY,
                detail=f"the document names '{name}', not the candidate "
                       f"'{profile.display_name}'",
                offending_text=name)]
        return []

    # --- corpora --------------------------------------------------------

    def _checkable_texts(self, content: DocumentContent
                         ) -> list[tuple[EvidenceBackedText, str]]:
        """Every evidence-backed line in the document, with a human label.

        Headings and subheadings are not here: they are factual frames (a title, a
        date range) whose evidence the block cites as a whole, and scanning them
        for numbers would flag a legitimate "2021–2024" that the cited employment
        record carries anyway. The lines that make *claims* — the summary, the
        bullets, the letter's paragraphs — are what the numeric and term rules
        police, exactly as V1 policed bullet and summary text.
        """
        texts: list[tuple[EvidenceBackedText, str]] = []
        if isinstance(content, ResumeDocument):
            if content.summary is not None:
                texts.append((content.summary, "summary"))
            texts += self._entry_texts(content.experience, "experience")
            texts += self._entry_texts(content.education, "education")
        elif isinstance(content, CoverLetterDocument):
            for index, paragraph in enumerate(content.body, start=1):
                texts.append((paragraph, f"cover letter paragraph {index}"))
        return texts

    @staticmethod
    def _entry_texts(entries: tuple[ResumeEntry, ...], section: str
                     ) -> list[tuple[EvidenceBackedText, str]]:
        texts: list[tuple[EvidenceBackedText, str]] = []
        for entry_index, entry in enumerate(entries, start=1):
            for bullet_index, bullet in enumerate(entry.bullets, start=1):
                texts.append((
                    bullet,
                    f"{section} entry {entry_index} bullet {bullet_index}"))
        return texts

    def _evidence_corpus(self, profile: CandidateProfile) -> str:
        """All prose a term may legitimately come from (V1's `_base_tech_corpus`).

        Every evidence record's summary and detail, plus every claim's label and
        detail, lower-cased and joined. A term the candidate genuinely holds
        appears here because the record or the claim that backs it names it; an
        invented one does not.
        """
        parts: list[str] = []
        for item in profile.evidence:
            parts.append(item.summary)
            if item.detail:
                parts.append(item.detail)
            if item.reference_key:
                parts.append(item.reference_key)
        for claim in profile.claims:
            parts.append(claim.label)
            if claim.detail:
                parts.append(claim.detail)
        return " ".join(parts).lower()

    def _term_universe(self, profile: CandidateProfile,
                       opportunity: Opportunity) -> tuple[str, ...]:
        """The claimable hard-skill terms the term rule scans for.

        V1 read this list from `cv/glossary.yaml` + `cv/keywords.yaml`. V2 assembles
        it from three structured sources instead: the skill ontology (its canonical
        names and aliases), the posting's `skill_requirements`, and the candidate's
        own claim labels. The union is what makes the rule bite where it matters —
        a posting demanding "Kafka" puts Kafka in the universe, so a letter that
        claims Kafka with no evidence to back it is caught, even though "Kafka" is
        not in the seed ontology.
        """
        terms: set[str] = set()
        for entry in self._ontology.entries:
            terms.add(entry.canonical)
            terms.update(entry.aliases)
        for requirement in opportunity.skill_requirements:
            terms.add(requirement.skill)
        for claim in profile.claims:
            if claim.claim_type is ClaimType.SKILL:
                terms.add(claim.label)
        # Empty and whitespace-only guard: a term that is not a word cannot anchor a
        # boundary pattern, and would match everywhere.
        return tuple(sorted(term for term in terms if term.strip()))

    def _claimed_skills(self, profile: CandidateProfile) -> frozenset[str]:
        """The canonical spellings of every skill the candidate claims.

        A `SKILL` claim's `label` run through the same normalizer the résumé
        skills are, so the comparison is canonical-to-canonical and an alias on
        either side lines up.
        """
        canonical: set[str] = set()
        for claim in profile.claims:
            if claim.claim_type is not ClaimType.SKILL:
                continue
            normalized = normalize_skill(claim.label, self._ontology)
            if normalized is not None:
                canonical.add(normalized.canonical)
        return frozenset(canonical)

    @staticmethod
    def _evidence_text(record: object) -> str:
        """The prose of one evidence record, for the per-line number pool.

        Summary and detail concatenated. `getattr` rather than a type import keeps
        this helper indifferent to the record being anything but a
        `CandidateEvidence`; the caller only ever passes one.
        """
        summary = getattr(record, "summary", "") or ""
        detail = getattr(record, "detail", "") or ""
        reference = getattr(record, "reference_key", "") or ""
        return f"{summary} {detail} {reference}"


def _fold(value: str) -> str:
    """Case- and whitespace-normalized form, for identity comparison."""
    return " ".join(value.split()).casefold()
