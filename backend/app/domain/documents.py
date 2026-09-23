"""Candidate-facing documents: the ATS résumé and the cover letter.

This is the domain Phase 10 exists to serve, and every model here is shaped by
one rule (docs/CANDIDATE_EVIDENCE.md, CLAUDE.md): *the system may rewrite,
reorder, shorten, emphasize or omit truthful candidate information, but it may
never invent new candidate facts.* The models make that rule structural rather
than aspirational —

- every sentence the platform would put on a document (`EvidenceBackedText`) and
  every experience or education block (`ResumeEntry`) is required to cite at
  least one `CandidateEvidence` id, so an unsupported line is *unconstructible*,
  exactly as `CandidateClaim` already made an unsupported claim unconstructible;
- the content types are the *structured* form of a document, never a blob of
  generated prose. A `DocumentGenerator` proposes this structure and the
  `CandidateEvidenceGuard` (`backend.app.documents.guard`) checks it against the
  candidate's evidence *before* it is ever rendered or stored. Persisting a blob
  would leave nothing to check.

A `CandidateDocument` is the durable thing a candidate keeps for one posting, of
one type; a `DocumentVersion` is one attempt at it, carrying the content, the
guard's verdict and — once it clears the guard and is rendered — the reference to
its PDF artifact. Regeneration appends a version rather than overwriting one, so
the audit trail §36 asks for is the aggregate's own history.

No PII (email, phone, postal address) and no file bytes appear here, for the same
reasons `CandidateProfile` and `OpportunitySourceRecord` keep them out: the
domain describes documents, it does not carry the candidate's contact card or the
rendered PDF's bytes. The artifact reference is a locator, resolved by a
`DocumentArtifactStore` adapter.
"""
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from backend.app.domain.base import (
    DomainModel,
    LanguageCode,
    NonEmptyStr,
    UtcDatetime,
)
from backend.app.domain.identifiers import (
    CandidateDocumentId,
    CandidateProfileId,
    DocumentVersionId,
    EvidenceId,
    OpportunityId,
    UserId,
)


class CandidateDocumentType(StrEnum):
    """The kinds of document Phase 10 produces.

    A résumé and a cover letter for the same posting are two documents, not two
    faces of one, because they have independent version histories: a candidate
    may regenerate the letter after an interview without touching the résumé. The
    value is part of a `candidate_document_id`, so the split is also what keeps
    their rows distinct.
    """

    RESUME = "RESUME"
    COVER_LETTER = "COVER_LETTER"


class DocumentStatus(StrEnum):
    """Where one `DocumentVersion` sits in its lifecycle (§7).

    The path is deliberately one-way through the guard: a version is `DRAFT` when
    a generator has proposed content, `VALIDATING` while the guard runs, and then
    either `VALIDATED` (cleared the guard) or `REJECTED` (a fabrication was
    caught). Only a `VALIDATED` version may be `RENDERED` to a PDF, and only a
    rendered or validated one may be `ARCHIVED` when a newer version supersedes
    it. A `REJECTED` version is kept, not discarded — the whole point of the guard
    is auditable, so the rejected attempt and *why* it was rejected are part of the
    record (§45).
    """

    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    RENDERED = "RENDERED"
    ARCHIVED = "ARCHIVED"

    @property
    def is_terminal_failure(self) -> bool:
        return self is DocumentStatus.REJECTED

    @property
    def is_usable(self) -> bool:
        """Whether a version in this state may be shown to the candidate as theirs.

        `VALIDATED` and `RENDERED` are the two states in which the content has
        passed the guard. A `DRAFT` has not been checked and a `REJECTED` one
        failed, so neither is offered as the document — a surface may show them
        for transparency, but never as "your résumé".
        """
        return self in (DocumentStatus.VALIDATED, DocumentStatus.RENDERED)


class DocumentViolationCode(StrEnum):
    """Why the guard refused a version (§12-16).

    These are the V2 form of V1's `truth_violations` categories
    (`pipeline/tailor_io.py`), named so a surface and an analytics query group on a
    code rather than parse prose. Every one describes a way generated content
    would state something the candidate's evidence does not support.

    There is deliberately no `MISSING_CITATION`: an uncited line is not something
    the guard has to *catch*, because `EvidenceBackedText` and `ResumeEntry` cannot
    be constructed without at least one evidence id (`Field(min_length=1)`). The
    guard's job is the harder question the type cannot answer — whether the cited
    evidence *exists* (`UNKNOWN_EVIDENCE`) and whether the *words* are supported.
    """

    UNKNOWN_EVIDENCE = "UNKNOWN_EVIDENCE"
    INVENTED_NUMBER = "INVENTED_NUMBER"
    UNSUPPORTED_SKILL = "UNSUPPORTED_SKILL"
    INVENTED_TERM = "INVENTED_TERM"
    ALTERED_IDENTITY = "ALTERED_IDENTITY"


class DocumentGuardViolation(DomainModel):
    """One reason a version failed the `CandidateEvidenceGuard`.

    `evidence_ids` names the records the offending text *should* have rested on
    (empty when the problem is that it cited nothing), and `offending_text` quotes
    the fragment that tripped the rule, so a reviewer sees the sentence rather than
    a section number. Kept as a value: it is a finding, not an entity.
    """

    code: DocumentViolationCode
    detail: NonEmptyStr
    offending_text: NonEmptyStr | None = None
    evidence_ids: tuple[EvidenceId, ...] = ()


class DocumentGuardReport(DomainModel):
    """The guard's verdict on one version.

    `ok` is not free to disagree with `violations`: a report that claimed to pass
    while carrying a violation would be the exact silent-failure the guard exists
    to prevent, so the validator ties them together. An empty, `ok=True` report is
    a version that cleared every gate.
    """

    ok: bool
    violations: tuple[DocumentGuardViolation, ...] = ()

    @model_validator(mode="after")
    def _verdict_matches_findings(self) -> Self:
        if self.ok and self.violations:
            raise ValueError("a passing guard report must carry no violations")
        if not self.ok and not self.violations:
            raise ValueError("a failing guard report must name at least one violation")
        return self


class EvidenceBackedText(DomainModel):
    """A sentence the platform is willing to write, and what it rests on.

    This is the atom of the truth guarantee: any line that makes a claim about the
    candidate — a summary sentence, an experience bullet, a cover-letter paragraph
    — is one of these, and it cannot exist without at least one `EvidenceId`. The
    guard then checks that the *words* are supported too (no invented number, no
    skill absent from the cited evidence), but the citation itself is a
    type-level invariant, mirroring `CandidateClaim.evidence_ids`.
    """

    text: NonEmptyStr
    evidence_ids: Annotated[tuple[EvidenceId, ...], Field(min_length=1)]


class ResumeSkillGroup(DomainModel):
    """A labelled cluster of skills on the résumé.

    Each entry in `skills` must be backed by a candidate `SKILL` claim — the guard
    checks it against the normalized skill ontology, so "Node" and "Node.js" are
    one skill and an unclaimed one is a `UNSUPPORTED_SKILL` violation. The group
    `name` ("Languages", "Cloud") is presentation and carries no claim.
    """

    name: NonEmptyStr | None = None
    skills: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _skills_are_distinct(self) -> Self:
        folded = [skill.casefold() for skill in self.skills]
        if len(folded) != len(set(folded)):
            raise ValueError("a skill group must not repeat a skill")
        return self


class ResumeEntry(DomainModel):
    """One experience, education or project block.

    `heading` and `subheading` are the block's factual frame ("Software Engineer —
    Acme", "2021–2024, Lausanne"). Those are facts too, so the entry as a whole
    cites the employment or education evidence it summarizes; each bullet then
    cites its own evidence on top. A block with no bullets is allowed — an
    education line is often just its heading — but a block that cites no evidence
    is not, because then its heading would be an unsupported fact.
    """

    heading: NonEmptyStr
    subheading: NonEmptyStr | None = None
    evidence_ids: Annotated[tuple[EvidenceId, ...], Field(min_length=1)]
    bullets: tuple[EvidenceBackedText, ...] = ()


class ResumeDocument(DomainModel):
    """The structured, ATS-oriented résumé (§28-32).

    `full_name` is identity, not a claim — it is the candidate's own
    `CandidateProfile.display_name`, and rewriting it would be altering identity,
    which the guard forbids (`ALTERED_IDENTITY`). `headline` is likewise carried
    from the profile. Everything that asserts a *fact* — the summary, every
    experience bullet, every skill — is evidence-backed. `languages` renders the
    profile's `LanguageProficiency` lines, which are facts the profile already
    holds rather than anything the generator composed.

    The structure is intentionally flat and label-driven: an ATS parser
    (§28) reads plain sections, so the renderer emits headings and bullet lists,
    never multi-column tables or text boxes.
    """

    kind: Literal["RESUME"] = "RESUME"
    full_name: NonEmptyStr
    headline: NonEmptyStr | None = None
    summary: EvidenceBackedText | None = None
    experience: tuple[ResumeEntry, ...] = ()
    education: tuple[ResumeEntry, ...] = ()
    skill_groups: tuple[ResumeSkillGroup, ...] = ()
    languages: tuple[NonEmptyStr, ...] = ()


class CoverLetterDocument(DomainModel):
    """The structured cover letter (§33).

    The persuasive counterpart to the résumé, and the looser surface: its body is
    prose. So the same invariant is applied where it bites — every `body`
    paragraph is an `EvidenceBackedText` and must cite the candidate evidence it
    draws on, and the guard checks its numbers and skill-terms just as it does the
    résumé's. `recipient`, `greeting`, `closing` and `signature` are the letter's
    frame: the recipient and greeting name the *employer* (a fact about the
    opportunity, not the candidate), and the signature is the candidate's own name.
    """

    kind: Literal["COVER_LETTER"] = "COVER_LETTER"
    recipient: NonEmptyStr | None = None
    greeting: NonEmptyStr | None = None
    body: Annotated[tuple[EvidenceBackedText, ...], Field(min_length=1)]
    closing: NonEmptyStr | None = None
    signature: NonEmptyStr


# The content of a version is exactly one of the two document shapes, told apart
# by their literal `kind`. A discriminated union rather than two nullable fields,
# so an impossible "both set" or "neither set" state cannot be represented.
DocumentContent = Annotated[
    ResumeDocument | CoverLetterDocument, Field(discriminator="kind")]


class DocumentArtifactRef(DomainModel):
    """Where a rendered PDF lives, without carrying its bytes.

    A locator resolved by a `DocumentArtifactStore` (`backend.app.documents`),
    kept deliberately storage-neutral: `storage_key` is opaque to the domain (a
    path under a local root today, an object key on a bucket tomorrow), and
    `media_type` and `byte_size` are metadata a download response needs without
    reading the file. The domain never holds the PDF itself, for the same reason
    `CandidateEvidence.source_document` is a label and not a file.
    """

    storage_key: NonEmptyStr
    media_type: NonEmptyStr = "application/pdf"
    byte_size: Annotated[int, Field(ge=0)]
    page_count: Annotated[int, Field(ge=1)] | None = None
    rendered_at: UtcDatetime


class DocumentVersion(DomainModel):
    """One attempt at a document: content, verdict, and (if rendered) its artifact.

    `version` is the document's own monotonic counter, starting at 1; the
    aggregate assigns it and `document_version_id` turns `(document, version)`
    into a stable key. `status` and `guard_report` must agree — a `VALIDATED` or
    `RENDERED` version cannot carry a failing report, and a `REJECTED` one cannot
    carry a passing one — because the status is a summary of the verdict and a
    disagreement would let a rejected version be served as usable. `artifact`
    exists only once the version is `RENDERED`.

    `generator_key` is provenance, a plain string for the same reason
    `MatchEvaluation.evaluator_key` is: the domain stays provider-neutral
    (docs/LLM_PROVIDER_ARCHITECTURE.md §3), and it must still be possible to tell
    which generator — the deterministic reference or a model-assisted one —
    produced a version when auditing.
    """

    id: DocumentVersionId
    version: Annotated[int, Field(ge=1)]
    status: DocumentStatus
    language: LanguageCode
    content: DocumentContent
    guard_report: DocumentGuardReport | None = None
    artifact: DocumentArtifactRef | None = None
    generator_key: NonEmptyStr | None = None
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _status_agrees_with_verdict_and_artifact(self) -> Self:
        report = self.guard_report
        if self.status in (DocumentStatus.VALIDATED, DocumentStatus.RENDERED):
            if report is None or not report.ok:
                raise ValueError(
                    f"a {self.status} version must carry a passing guard report")
        if self.status is DocumentStatus.REJECTED and (report is None or report.ok):
            raise ValueError("a REJECTED version must carry a failing guard report")
        if self.status is DocumentStatus.RENDERED and self.artifact is None:
            raise ValueError("a RENDERED version must reference its artifact")
        if self.status is not DocumentStatus.RENDERED and self.artifact is not None:
            raise ValueError(
                f"a {self.status} version has no rendered artifact to reference")
        return self

    @model_validator(mode="after")
    def _content_kind_is_consistent(self) -> Self:
        # The literal on the content is what the discriminated union switches on;
        # this only guards against a hand-built version whose content was swapped.
        if self.content.kind not in ("RESUME", "COVER_LETTER"):
            raise ValueError("unknown document content kind")
        return self

    @property
    def document_type(self) -> CandidateDocumentType:
        """The type implied by the content, so the aggregate can check agreement."""
        return (CandidateDocumentType.RESUME if self.content.kind == "RESUME"
                else CandidateDocumentType.COVER_LETTER)


class CandidateDocument(DomainModel):
    """A document a candidate keeps for one posting, across its versions.

    User-owned and scoped to a `(candidate_profile_id, opportunity_id,
    document_type)` triple — the same triple `candidate_document_id` derives from —
    so regenerating never leaves an orphan document behind, only a new version
    under this one. At least one version is required: a document exists because
    something was generated for it. Versions are held newest-last, their numbers
    strictly increasing, and every version's content must match this document's
    declared `document_type`, so a résumé cannot smuggle itself into a cover
    letter's history.
    """

    id: CandidateDocumentId
    user_id: UserId
    candidate_profile_id: CandidateProfileId
    opportunity_id: OpportunityId
    document_type: CandidateDocumentType
    versions: Annotated[tuple[DocumentVersion, ...], Field(min_length=1)]
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _versions_are_ordered_and_typed(self) -> Self:
        numbers = [version.version for version in self.versions]
        if numbers != sorted(numbers) or len(numbers) != len(set(numbers)):
            raise ValueError("versions must have strictly increasing version numbers")
        ids = [version.id for version in self.versions]
        if len(ids) != len(set(ids)):
            raise ValueError("versions must have unique ids")
        mismatched = [v.version for v in self.versions
                      if v.document_type is not self.document_type]
        if mismatched:
            raise ValueError(
                f"versions {mismatched} do not match document_type {self.document_type}")
        return self

    @property
    def latest(self) -> DocumentVersion:
        """The most recent version — never absent, since one is required."""
        return self.versions[-1]

    def version(self, number: int) -> DocumentVersion | None:
        """One version by its number, or `None` if this document has no such version."""
        for candidate in self.versions:
            if candidate.version == number:
                return candidate
        return None

    def latest_usable(self) -> DocumentVersion | None:
        """The newest version fit to be shown as the candidate's document.

        Newest-first so a later validated version wins over an earlier one; `None`
        when every version is a draft or was rejected, which a surface reports as
        "not generated yet" rather than showing an unchecked draft.
        """
        for candidate in reversed(self.versions):
            if candidate.status.is_usable:
                return candidate
        return None

    def next_version_number(self) -> int:
        """The number the next appended version should carry."""
        return self.versions[-1].version + 1


class GenerationContext(DomainModel):
    """Everything a `DocumentGenerator` is given, and nothing it must not have.

    The input bundle to generation, assembled by the `DocumentService` and never
    persisted. It is provider-neutral by construction: it carries domain objects,
    not a prompt or a provider handle, so a generator adapter is free to turn it
    into whatever its provider needs (docs/LLM_PROVIDER_ARCHITECTURE.md §3). A
    generator receives the candidate's evidence-bearing `CandidateProfile` and the
    `Opportunity`, so it can select and emphasize — but since it can only cite
    evidence ids that exist on the profile, and the guard checks every citation, it
    *cannot* invent. The optional `match_evaluation` lets a generator lead with the
    dimensions that already scored well, which is emphasis, not fabrication.

    Held as ids plus the objects the generator needs rather than the whole world:
    the profile and opportunity are the two aggregates a document is built from,
    and `target_language` is the language the posting is in (or the candidate's
    preference), decided by the service before generation.
    """

    document_type: CandidateDocumentType
    target_language: LanguageCode
    # The profile and opportunity are passed as their ids here; the concrete
    # `CandidateProfile` and `Opportunity` are handed to the generator's method
    # alongside this context (see `backend.app.documents.generator`). Keeping the
    # ids on the context makes a generated version's provenance explicit without
    # forcing the frozen aggregates into a field that would make this unhashable.
    candidate_profile_id: CandidateProfileId
    opportunity_id: OpportunityId
