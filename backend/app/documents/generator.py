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
from typing import Protocol, runtime_checkable

from backend.app.domain.candidate import (
    CandidateClaim,
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
        if not profile.evidence:
            raise InsufficientEvidence(
                "the candidate has no evidence on file, so no résumé can be built "
                "from it")
        return ResumeDocument(
            full_name=profile.display_name,
            headline=profile.headline,
            summary=self._summary(profile),
            experience=self._entries(profile, ClaimType.EXPERIENCE),
            education=self._entries(profile, ClaimType.EDUCATION),
            skill_groups=self._skill_groups(profile),
            languages=self._languages(profile),
        )

    def _summary(self, profile: CandidateProfile) -> EvidenceBackedText | None:
        """The résumé summary, taken verbatim from a summary evidence record.

        A `CV_SUMMARY` record if one exists; otherwise none — the generator does
        not manufacture a summary out of the headline, because a headline is a
        profile label, not an evidence-backed statement.
        """
        for item in profile.evidence:
            if item.kind is EvidenceKind.CV_SUMMARY:
                return EvidenceBackedText(text=item.summary, evidence_ids=(item.id,))
        return None

    def _entries(self, profile: CandidateProfile,
                 claim_type: ClaimType) -> tuple[ResumeEntry, ...]:
        """One entry per claim of a type, its bullets straight from its evidence.

        The heading is the claim's own label and the bullets are the summaries of
        the evidence backing it, each citing exactly the record it was taken from.
        Because a bullet's text *is* an evidence record's text, its numbers and
        terms are trivially supported — the generator selects, it does not compose.
        """
        entries: list[ResumeEntry] = []
        for claim in self._claims_of(profile, claim_type):
            evidence = profile.evidence_for(claim)
            bullets = tuple(
                EvidenceBackedText(text=item.summary, evidence_ids=(item.id,))
                for item in evidence)
            entries.append(ResumeEntry(
                heading=claim.label,
                subheading=claim.detail,
                evidence_ids=claim.evidence_ids,
                bullets=bullets))
        return tuple(entries)

    def _skill_groups(self, profile: CandidateProfile
                      ) -> tuple[ResumeSkillGroup, ...]:
        """A single "Skills" group listing every distinct SKILL claim label.

        One group, because the reference generator has no basis for clustering
        skills into "Languages" / "Cloud" without a taxonomy it would have to
        invent. Labels are de-duplicated case-insensitively to satisfy the group's
        own invariant, keeping the first spelling the candidate used.
        """
        seen: set[str] = set()
        skills: list[str] = []
        for claim in self._claims_of(profile, ClaimType.SKILL):
            folded = claim.label.casefold()
            if folded not in seen:
                seen.add(folded)
                skills.append(claim.label)
        if not skills:
            return ()
        return (ResumeSkillGroup(name="Skills", skills=tuple(skills)),)

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
        paragraphs = self._letter_paragraphs(profile)
        if not paragraphs:
            raise InsufficientEvidence(
                "the candidate has no evidence on file, so no cover letter can be "
                "built from it")
        # The employer and the role name the *letter's frame*, not the candidate,
        # so they live in `recipient`/`greeting` — fields the guard does not treat
        # as candidate claims — rather than in an evidence-backed paragraph.
        return CoverLetterDocument(
            recipient=self._recipient(opportunity),
            greeting="Dear Hiring Team,",
            body=paragraphs,
            closing="Thank you for considering my application. I would welcome the "
                    "opportunity to discuss how I can contribute.",
            signature=profile.display_name,
        )

    def _letter_paragraphs(self, profile: CandidateProfile
                           ) -> tuple[EvidenceBackedText, ...]:
        """Body paragraphs, each a candidate evidence record quoted verbatim.

        Opens with the summary record if there is one, then draws on the records
        behind the candidate's achievement and experience claims, capped so the
        letter stays a letter. Every paragraph cites the single record it quotes,
        so the guard's number and term rules pass by construction.
        """
        paragraphs: list[EvidenceBackedText] = []
        used: set[EvidenceId] = set()
        summary = self._summary(profile)
        if summary is not None:
            paragraphs.append(summary)
            used.update(summary.evidence_ids)
        for claim_type in (ClaimType.ACHIEVEMENT, ClaimType.EXPERIENCE):
            for claim in self._claims_of(profile, claim_type):
                for item in profile.evidence_for(claim):
                    if item.id in used:
                        continue
                    paragraphs.append(EvidenceBackedText(
                        text=item.summary, evidence_ids=(item.id,)))
                    used.add(item.id)
                    if len(paragraphs) >= _MAX_COVER_LETTER_PARAGRAPHS:
                        return tuple(paragraphs)
        if paragraphs:
            return tuple(paragraphs)
        # No summary and no achievement/experience claims: fall back to any single
        # evidence record so a candidate with only, say, a diploma still gets a
        # truthful one-line letter rather than an error.
        for item in profile.evidence:
            return (EvidenceBackedText(text=item.summary, evidence_ids=(item.id,)),)
        return ()

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
