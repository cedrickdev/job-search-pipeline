"""Producing document content — the provider-neutral seam, and one reference.

docs/LLM_PROVIDER_ARCHITECTURE.md §3 and CLAUDE.md both forbid provider SDK calls
from leaking across the codebase: an LLM that helps draft a résumé must sit behind
an adapter. `DocumentGenerator` is that adapter's shape. It takes a
`GenerationContext`, the candidate's `CandidateProfile` and the `Opportunity`, and
returns structured `DocumentContent` — never a prompt, never a provider handle, so
the service above it and the guard beside it are indifferent to whether a human
rule, a local model or a hosted one produced the content.

`DeterministicDocumentGenerator` is the reference implementation, and it is
deliberately not an LLM. It selects, reorders and re-labels the candidate's own
evidence — the operations CLAUDE.md sanctions — and composes every line straight
from an evidence record's own text, so it passes the `CandidateEvidenceGuard` by
construction rather than by luck. It exists for three reasons: the default test
generator must not depend on a live LLM (CLAUDE.md §Testing); a deployment with no
model configured still produces a truthful, if plain, document; and it is the
worked example of the contract every model-backed adapter must meet — *cite
evidence that exists, and never state a number or a skill the cited evidence does
not carry.*
"""
import re
from typing import Protocol, runtime_checkable

from backend.app.domain.candidate import (
    CandidateClaim,
    CandidateEvidence,
    CandidateProfile,
    ClaimType,
    EvidenceKind,
)
from backend.app.domain.documents import (
    CoverLetterDocument,
    EvidenceBackedText,
    GenerationContext,
    ResumeDocument,
    ResumeEntry,
    ResumeSkillGroup,
)
from backend.app.domain.identifiers import EvidenceId
from backend.app.domain.opportunity import Opportunity

REFERENCE_GENERATOR_KEY = "deterministic-reference/1"

# The most body paragraphs / experience entries the reference generator will
# emit. A cap, not a rule of the domain: it keeps a one-page document plausible
# before the renderer's trimming ever has to intervene, and a model-backed
# generator is free to choose differently.
_MAX_COVER_LETTER_PARAGRAPHS = 4
_MAX_RESUME_HIGHLIGHTS = 8
_MIN_USABLE_RESUME_FACTS = 2
_MEANINGFUL_TOKEN = re.compile(r"[a-zA-ZÀ-ÿ][a-zA-ZÀ-ÿ0-9+#.]{2,}")
_NON_FACTUAL_LINES = {
    "languages", "langues", "skills", "compétences", "competences",
    "experience", "expérience", "education", "formation", "contact",
}


class InsufficientEvidence(Exception):
    """The profile carries too little evidence to build a truthful document.

    Not an error in the usual sense: a candidate who has recorded no evidence
    cannot have a résumé generated from evidence, and the honest response is to
    say so, not to invent content. The service maps this to a clear client error
    rather than a 500.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@runtime_checkable
class DocumentGenerator(Protocol):
    """Proposes structured document content from a candidate's evidence.

    Two methods rather than one dispatching on type (spec §19), because a résumé
    and a cover letter are different enough compositions that an adapter usually
    implements them separately. Both are pure with respect to the platform: they
    return content, and the service is what validates and persists it.

    Both are `async`, because the point of this seam is that a model-backed
    generator can sit behind it (Phase 11's `LLMDocumentGenerator`), and reaching a
    model is I/O — a subprocess or an HTTP call the `LLMRouter` awaits. The
    deterministic reference does no I/O and returns immediately, but it is `async`
    too so the one seam serves both without the service needing to know which it
    holds: a synchronous protocol would have forced every caller to learn that a
    model-backed generator cannot honour it.
    """

    @property
    def key(self) -> str:
        """Provenance stamp recorded on every version this generator produces."""
        ...

    async def generate_resume(self, *, profile: CandidateProfile,
                              opportunity: Opportunity,
                              context: GenerationContext) -> ResumeDocument:
        ...

    async def generate_cover_letter(self, *, profile: CandidateProfile,
                                    opportunity: Opportunity,
                                    context: GenerationContext) -> CoverLetterDocument:
        ...


class DeterministicDocumentGenerator:
    """Builds documents by selecting and reordering the candidate's own evidence.

    Every sentence it emits is composed from an evidence record's own text, cited
    to that record — so the content it returns is, by construction, content the
    `CandidateEvidenceGuard` accepts. It never translates (that would risk
    invention) and never fills a gap with prose; a thin profile yields a plain
    document, which is the honest outcome.
    """

    @property
    def key(self) -> str:
        return REFERENCE_GENERATOR_KEY

    # --- résumé ---------------------------------------------------------

    async def generate_resume(self, *, profile: CandidateProfile,
                              opportunity: Opportunity,
                              context: GenerationContext) -> ResumeDocument:
        self._require_usable_resume_evidence(profile)
        return ResumeDocument(
            full_name=profile.display_name,
            headline=profile.headline,
            summary=self._summary(profile, opportunity),
            highlights=self._highlights(profile, opportunity),
            experience=self._entries(profile, ClaimType.EXPERIENCE, opportunity),
            education=self._entries(profile, ClaimType.EDUCATION, opportunity),
            skill_groups=self._skill_groups(profile, opportunity, language=context.target_language),
            languages=self._languages(profile),
        )

    def _summary(self, profile: CandidateProfile,
                 opportunity: Opportunity | None = None) -> EvidenceBackedText | None:
        """The résumé summary, taken verbatim from a summary evidence record.

        A `CV_SUMMARY` record if one exists; otherwise none — the generator does
        not manufacture a summary out of the headline, because a headline is a
        profile label, not an evidence-backed statement.
        """
        for item in self._ranked_evidence(profile.evidence, opportunity):
            if item.kind is EvidenceKind.CV_SUMMARY:
                return EvidenceBackedText(text=item.summary, evidence_ids=(item.id,))
        return None

    def _entries(self, profile: CandidateProfile,
                 claim_type: ClaimType,
                 opportunity: Opportunity | None = None) -> tuple[ResumeEntry, ...]:
        """One entry per claim of a type, its bullets straight from its evidence.

        The heading is the claim's own label and the bullets are the summaries of
        the evidence backing it, each citing exactly the record it was taken from.
        Because a bullet's text *is* an evidence record's text, its numbers and
        terms are trivially supported — the generator selects, it does not compose.
        """
        entries: list[ResumeEntry] = []
        for claim in self._ranked_claims(profile, claim_type, opportunity):
            evidence = self._ranked_evidence(profile.evidence_for(claim), opportunity)
            bullets = tuple(
                EvidenceBackedText(text=item.summary, evidence_ids=(item.id,))
                for item in evidence)
            entries.append(ResumeEntry(
                heading=claim.label,
                subheading=claim.detail,
                evidence_ids=claim.evidence_ids,
                bullets=bullets))
        return tuple(entries)

    def _skill_groups(self, profile: CandidateProfile,
                      opportunity: Opportunity | None = None, *, language: str = "en"
                      ) -> tuple[ResumeSkillGroup, ...]:
        """A single "Skills" group listing every distinct SKILL claim label.

        One group, because the reference generator has no basis for clustering
        skills into "Languages" / "Cloud" without a taxonomy it would have to
        invent. Labels are de-duplicated case-insensitively to satisfy the group's
        own invariant, keeping the first spelling the candidate used.
        """
        seen: set[str] = set()
        skills: list[str] = []
        for claim in self._ranked_claims(profile, ClaimType.SKILL, opportunity):
            folded = claim.label.casefold()
            if folded not in seen:
                seen.add(folded)
                skills.append(claim.label)
        if not skills:
            return ()
        return (ResumeSkillGroup(name="Compétences" if language == "fr" else "Skills",
                                 skills=tuple(skills)),)

    def _highlights(self, profile: CandidateProfile,
                    opportunity: Opportunity) -> tuple[EvidenceBackedText, ...]:
        """Surface imported CV facts without pretending they are jobs or degrees.

        The importer intentionally records raw CV lines as evidence.  Until the
        candidate has reviewed/structured those lines into claims, this section
        keeps useful, truthful material in the CV while preserving that distinction.
        A posting only changes selection/order, never the words themselves.
        """
        structured = {item.id for claim in profile.claims
                      for item in profile.evidence_for(claim)}
        highlights: list[EvidenceBackedText] = []
        for item in self._ranked_evidence(profile.evidence, opportunity):
            if item.id in structured or item.kind is EvidenceKind.CV_SUMMARY:
                continue
            if not self._is_meaningful_fact(item.summary):
                continue
            highlights.append(EvidenceBackedText(
                text=item.summary, evidence_ids=(item.id,)))
            if len(highlights) == _MAX_RESUME_HIGHLIGHTS:
                break
        return tuple(highlights)

    @staticmethod
    def _is_meaningful_fact(text: str) -> bool:
        normalized = " ".join(text.casefold().split())
        return normalized not in _NON_FACTUAL_LINES and len(_MEANINGFUL_TOKEN.findall(text)) >= 2

    def _require_usable_resume_evidence(self, profile: CandidateProfile) -> None:
        structured = sum(
            1 for claim in profile.claims
            if claim.claim_type in (ClaimType.EXPERIENCE, ClaimType.EDUCATION, ClaimType.SKILL,
                                     ClaimType.ACHIEVEMENT))
        imported_facts = sum(
            1 for item in profile.evidence
            if item.kind is EvidenceKind.CV_SUMMARY or self._is_meaningful_fact(item.summary))
        if structured or imported_facts >= _MIN_USABLE_RESUME_FACTS:
            return
        raise InsufficientEvidence(
            "the profile does not yet contain enough usable CV evidence; import a complete "
            "reference CV or add at least two concrete profile facts before generating")

    @staticmethod
    def _opportunity_terms(opportunity: Opportunity | None) -> set[str]:
        if opportunity is None:
            return set()
        values = [opportunity.title, opportunity.company_name, opportunity.description or ""]
        values.extend(requirement.skill for requirement in opportunity.skill_requirements)
        return {term.casefold() for value in values for term in _MEANINGFUL_TOKEN.findall(value)}

    def _ranked_evidence(
        self, evidence: tuple[CandidateEvidence, ...], opportunity: Opportunity | None,
    ) -> tuple[CandidateEvidence, ...]:
        terms = self._opportunity_terms(opportunity)
        return tuple(sorted(
            evidence,
            key=lambda item: -len(terms.intersection(
                term.casefold() for term in _MEANINGFUL_TOKEN.findall(
                    f"{item.summary} {item.detail or ''}"))),
        ))

    def _ranked_claims(self, profile: CandidateProfile, claim_type: ClaimType,
                       opportunity: Opportunity | None) -> tuple[CandidateClaim, ...]:
        terms = self._opportunity_terms(opportunity)
        claims = self._claims_of(profile, claim_type)
        return tuple(sorted(
            claims,
            key=lambda claim: -len(terms.intersection(
                term.casefold() for term in _MEANINGFUL_TOKEN.findall(
                    f"{claim.label} {claim.detail or ''} " + " ".join(
                        item.summary for item in profile.evidence_for(claim))))),
        ))

    @staticmethod
    def _languages(profile: CandidateProfile) -> tuple[str, ...]:
        """The profile's declared languages, rendered as plain labels.

        Facts the profile already holds (`LanguageProficiency`), formatted for
        display. Not evidence-backed text and not guarded — a declared language is
        a profile fact, like the candidate's name.
        """
        return tuple(f"{proficiency.language.upper()} ({proficiency.level})"
                     for proficiency in profile.languages)

    # --- cover letter ---------------------------------------------------

    async def generate_cover_letter(self, *, profile: CandidateProfile,
                                    opportunity: Opportunity,
                                    context: GenerationContext) -> CoverLetterDocument:
        paragraphs = self._letter_paragraphs(profile, opportunity)
        if not paragraphs:
            raise InsufficientEvidence(
                "the candidate has no evidence on file, so no cover letter can be "
                "built from it")
        # The employer and the role name the *letter's frame*, not the candidate,
        # so they live in `recipient`/`greeting` — fields the guard does not treat
        # as candidate claims — rather than in an evidence-backed paragraph.
        return CoverLetterDocument(
            recipient=self._recipient(opportunity),
            greeting=("Madame, Monsieur," if context.target_language == "fr"
                      else "Dear Hiring Team,"),
            body=paragraphs,
            closing=("Je vous remercie de l’attention portée à ma candidature et me tiens "
                     "à votre disposition pour un entretien."
                     if context.target_language == "fr" else
                     "Thank you for considering my application. I would welcome the "
                     "opportunity to discuss how I can contribute."),
            signature=profile.display_name,
        )

    def _letter_paragraphs(self, profile: CandidateProfile, opportunity: Opportunity
                           ) -> tuple[EvidenceBackedText, ...]:
        """Body paragraphs, each a candidate evidence record quoted verbatim.

        Opens with the summary record if there is one, then draws on the records
        behind the candidate's achievement and experience claims, capped so the
        letter stays a letter. Every paragraph cites the single record it quotes,
        so the guard's number and term rules pass by construction.
        """
        paragraphs: list[EvidenceBackedText] = []
        used: set[EvidenceId] = set()
        summary = self._summary(profile, opportunity)
        if summary is not None:
            paragraphs.append(summary)
            used.update(summary.evidence_ids)
        for claim_type in (ClaimType.ACHIEVEMENT, ClaimType.EXPERIENCE):
            for claim in self._ranked_claims(profile, claim_type, opportunity):
                for item in self._ranked_evidence(profile.evidence_for(claim), opportunity):
                    if item.id in used:
                        continue
                    paragraphs.append(EvidenceBackedText(
                        text=item.summary, evidence_ids=(item.id,)))
                    used.add(item.id)
                    if len(paragraphs) >= _MAX_COVER_LETTER_PARAGRAPHS:
                        return tuple(paragraphs)
        if paragraphs:
            return tuple(paragraphs)
        # Imported facts have no structured claims yet. Select those most relevant
        # to this posting rather than always copying the first line of the CV.
        return tuple(EvidenceBackedText(text=item.summary, evidence_ids=(item.id,))
                     for item in self._ranked_evidence(profile.evidence, opportunity)
                     if self._is_meaningful_fact(item.summary))[:_MAX_COVER_LETTER_PARAGRAPHS]

    @staticmethod
    def _recipient(opportunity: Opportunity) -> str:
        """The letter's addressee line, built from opportunity facts.

        The company and the role are facts about the posting, not the candidate,
        so quoting them here invents nothing.
        """
        return f"{opportunity.company_name} — {opportunity.title}"

    # --- shared ---------------------------------------------------------

    @staticmethod
    def _claims_of(profile: CandidateProfile,
                   claim_type: ClaimType) -> tuple[CandidateClaim, ...]:
        return tuple(claim for claim in profile.claims
                     if claim.claim_type is claim_type)
