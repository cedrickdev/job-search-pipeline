"""DocumentService: generate a document, guard it, render it, and keep the version.

This is the one application service Phase 10 adds, and it is where the four pieces
in `backend.app.documents` become a workflow with a database behind it. The engine
pieces hold every *decision* — the generator composes content, the guard judges it,
the renderer lays it out — and this service decides *what to feed them* and *what to
do with the verdict*, in the one order that keeps the truth guarantee physical:

**Generate, then guard, then — only if it passed — render and store, then persist.**
The guard runs before a single byte is written or stored, so a fabrication is caught
with nothing to roll back, exactly as V1's `truth_violations` runs before a CV is
ever rendered. A version that clears the guard is rendered, its PDF is stored, and
the whole document is upserted in one call; a version that fails is persisted too —
as REJECTED, carrying the guard's report and no artifact — because an auditable
refusal is the point (§45), not a dropped attempt.

**Regeneration appends a version; it never overwrites one.** The document for a
`(profile, opportunity, type)` triple is loaded first, and a new attempt becomes
`next_version_number()` under it. Nothing already stored is rewritten, so the
document's version list *is* its audit trail, and the ids are derived from the
triple and the number so a retried write lands on the same rows rather than
duplicating them.

**The owner comes from the session, never the body.** Every method takes `user_id`
and hands it to repositories whose signatures require it; a document, its versions
and its artifact are all reached through a `user_id`-scoped read, so another
account's document reads as absent and cannot be downloaded by guessing an id.
"""
from datetime import datetime

from backend.app.documents import (
    DocumentArtifactStore,
    DocumentGenerator,
    RenderedDocument,
    StoredArtifact,
    render_document,
)
from backend.app.documents.guard import CandidateEvidenceGuard
from backend.app.domain.base import LanguageCode
from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.documents import (
    CandidateDocument,
    CandidateDocumentType,
    DocumentArtifactRef,
    DocumentContent,
    DocumentGuardReport,
    DocumentStatus,
    DocumentVersion,
    GenerationContext,
)
from backend.app.domain.identifiers import (
    CandidateDocumentId,
    DocumentVersionId,
    OpportunityId,
    UserId,
    candidate_document_id,
    document_version_id,
)
from backend.app.domain.opportunity import Opportunity
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    CandidateDocumentRepository,
    CandidateProfileRepository,
    OpportunityRepository,
)
from backend.app.services.assessment import (
    CandidateProfileNotFound,
    OpportunityNotFound,
)

# The language a document is written in when neither the posting nor the candidate
# says otherwise. English is the safe default for an ATS, and it is only ever
# reached for a posting with no `posting_language` and a profile with no declared
# language — a rare, thin case where a plain-English document is the honest output.
_FALLBACK_LANGUAGE: LanguageCode = "en"


class DocumentNotFound(Exception):
    """No document is stored under that id for this account.

    User-owned, so "no such document" and "not yours" are one condition here for
    the reason `MatchEvaluationRepository.get` gives: a caller that could tell them
    apart could enumerate another account's documents by id. The API maps it to a
    404 that says neither which.
    """


class DocumentArtifactMissing(Exception):
    """The document exists but has no rendered artifact to download.

    Distinct from `DocumentNotFound`: the document and the version are this user's,
    but no version has cleared the guard and been rendered — every attempt is a
    draft or was rejected — so there is nothing to stream. The API maps it to a 409,
    not a 404, because the resource is real and the state is temporary: generate a
    version that passes and it will have one.
    """


class DocumentService:
    """Generate, guard, render, store and version a candidate's documents.

    Every collaborator is handed in rather than reached for: the three repositories
    (over PostgreSQL in production, over the fakes in a flow test), the
    provider-neutral `DocumentGenerator`, the pure `CandidateEvidenceGuard`, and the
    `DocumentArtifactStore` that holds the rendered PDFs. The service never learns
    which generator produced a version beyond stamping its `key`, which is what
    keeps generation provider-neutral (docs/LLM_PROVIDER_ARCHITECTURE.md §3).
    """

    def __init__(self, profiles: CandidateProfileRepository,
                 opportunities: OpportunityRepository,
                 documents: CandidateDocumentRepository,
                 generator: DocumentGenerator,
                 guard: CandidateEvidenceGuard,
                 artifacts: DocumentArtifactStore) -> None:
        self._profiles = profiles
        self._opportunities = opportunities
        self._documents = documents
        self._generator = generator
        self._guard = guard
        self._artifacts = artifacts

    async def generate(self, user_id: UserId, opportunity_id: OpportunityId,
                       document_type: CandidateDocumentType, *, now: datetime,
                       language: LanguageCode | None = None) -> CandidateDocument:
        """Produce one document version for a posting, and return the whole document.

        The workflow the module docstring lays out: compose content from the
        candidate's evidence (which raises `InsufficientEvidence` when there is too
        little to build a truthful document), guard it, and then either render and
        store a RENDERED version or keep a REJECTED one — appending it to the
        document for this triple as the next version.

        Idempotent per version number: the document id and the version id are
        derived from the triple and the count, so a retried call after a failed
        flush reuses the same rows rather than accreting a duplicate. A fresh call
        after a *successful* one, though, is a new attempt and a new version — that
        is regeneration, and it is meant to grow the history.
        """
        profile = await self._profiles.get_default(user_id)
        if profile is None:
            raise CandidateProfileNotFound(str(user_id))
        opportunity = await self._opportunities.get(opportunity_id)
        if opportunity is None:
            raise OpportunityNotFound(str(opportunity_id))

        target_language = language or opportunity.posting_language \
            or self._candidate_language(profile)
        context = GenerationContext(
            document_type=document_type, target_language=target_language,
            candidate_profile_id=profile.id, opportunity_id=opportunity_id)
        content = self._compose(document_type, profile, opportunity, context)
        report = self._guard.review(content, profile=profile, opportunity=opportunity)

        existing = await self._documents.get_for_pair(
            user_id, profile.id, opportunity_id, document_type)
        number = existing.next_version_number() if existing is not None else 1
        document_id = candidate_document_id(
            profile.id, opportunity_id, document_type.value)
        version = self._build_version(
            document_id=document_id, number=number, content=content, report=report,
            language=target_language, now=now)

        versions = (existing.versions if existing is not None else ()) + (version,)
        document = CandidateDocument(
            id=document_id, user_id=user_id, candidate_profile_id=profile.id,
            opportunity_id=opportunity_id, document_type=document_type,
            versions=versions,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now)
        return await self._documents.upsert(document)

    async def document(self, user_id: UserId,
                       document_id: CandidateDocumentId) -> CandidateDocument:
        """One document with its whole version history, or raise `DocumentNotFound`.

        A pure read, scoped by owner: another account's id reads as absent and
        raises the same error a never-created id does, so neither can be told from
        the other.
        """
        found = await self._documents.get(user_id, document_id)
        if found is None:
            raise DocumentNotFound(str(document_id))
        return found

    async def documents(self, user_id: UserId, *,
                        limit: int = DEFAULT_LIMIT) -> tuple[CandidateDocument, ...]:
        """This account's documents, most recently updated first."""
        return await self._documents.list_for_user(user_id, limit=limit)

    async def candidate_profile(self, user_id: UserId) -> CandidateProfile:
        """The account's profile, or raise `CandidateProfileNotFound`.

        The read the evidence-listing endpoint uses: a document is built from the
        profile's evidence and claims, so the surface that shows the attested record
        resolves it the same way generation does — through the account's default
        profile, never an id in the path.
        """
        profile = await self._profiles.get_default(user_id)
        if profile is None:
            raise CandidateProfileNotFound(str(user_id))
        return profile

    async def download(self, user_id: UserId,
                       document_id: CandidateDocumentId) -> StoredArtifact:
        """The bytes of the document's newest rendered version, for a download.

        Resolves the newest RENDERED version — the one a candidate would send — and
        reads its artifact from the store. `DocumentNotFound` when the document is
        not this account's; `DocumentArtifactMissing` when it exists but nothing has
        cleared the guard and been rendered yet. An `ArtifactNotFound` from the
        store (the row references a key the store no longer holds) is left to
        propagate: it is a storage fault, not a client error, and must not be
        disguised as "not rendered yet".
        """
        document = await self.document(user_id, document_id)
        version = _latest_rendered(document)
        if version is None or version.artifact is None:
            raise DocumentArtifactMissing(str(document_id))
        return self._artifacts.get(version.artifact.storage_key)

    def _compose(self, document_type: CandidateDocumentType,
                 profile: CandidateProfile, opportunity: Opportunity,
                 context: GenerationContext) -> DocumentContent:
        """Dispatch to the generator method for the type.

        Two methods rather than one taking a type, because the generator's contract
        splits them (§19): a résumé and a cover letter are different enough
        compositions that an adapter implements each on its own.
        """
        if document_type is CandidateDocumentType.RESUME:
            return self._generator.generate_resume(
                profile=profile, opportunity=opportunity, context=context)
        return self._generator.generate_cover_letter(
            profile=profile, opportunity=opportunity, context=context)

    def _build_version(self, *, document_id: CandidateDocumentId, number: int,
                       content: DocumentContent, report: DocumentGuardReport,
                       language: LanguageCode, now: datetime) -> DocumentVersion:
        """Turn a guard verdict into a stored version — rendering only if it passed.

        A passing report is rendered to a PDF, the PDF is stored, and the version
        references it as RENDERED. A failing report is kept as REJECTED with no
        artifact. The render and the store happen here, after the guard and before
        the caller persists the document, so a RENDERED version never exists without
        the bytes it points at.
        """
        version_id = document_version_id(document_id, number)
        if not report.ok:
            return DocumentVersion(
                id=version_id, version=number, status=DocumentStatus.REJECTED,
                language=language, content=content, guard_report=report,
                artifact=None, generator_key=self._generator.key, created_at=now)
        rendered = render_document(content, language=language)
        artifact = self._store_artifact(document_id, version_id, rendered, now=now)
        return DocumentVersion(
            id=version_id, version=number, status=DocumentStatus.RENDERED,
            language=language, content=content, guard_report=report,
            artifact=artifact, generator_key=self._generator.key, created_at=now)

    def _store_artifact(self, document_id: CandidateDocumentId,
                        version_id: DocumentVersionId, rendered: RenderedDocument, *,
                        now: datetime) -> DocumentArtifactRef:
        """Write the rendered PDF and describe where it landed.

        The key is derived from the two ids, so re-rendering a version overwrites
        its own artifact rather than leaking a file. `rendered_at` is this request's
        instant, the same clock every other timestamp on the version reads.
        """
        storage_key = self._artifacts.key_for(document_id, version_id)
        self._artifacts.put(storage_key, rendered.pdf_bytes,
                            media_type="application/pdf")
        return DocumentArtifactRef(
            storage_key=storage_key, media_type="application/pdf",
            byte_size=len(rendered.pdf_bytes), page_count=rendered.page_count,
            rendered_at=now)

    @staticmethod
    def _candidate_language(profile: CandidateProfile) -> LanguageCode:
        """The document language when the posting names none: the candidate's first.

        The first declared language proficiency, or English when the profile
        declares none. Never invents a language the candidate did not state —
        the fallback is a safe default, not a guess about what they speak.
        """
        if profile.languages:
            return profile.languages[0].language
        return _FALLBACK_LANGUAGE


def _latest_rendered(document: CandidateDocument) -> DocumentVersion | None:
    """The newest version that has been rendered to a PDF, or `None`.

    Newest-first so a later render wins over an earlier one. Stricter than
    `CandidateDocument.latest_usable()`, which also accepts a VALIDATED version that
    was never rendered: a download needs the bytes, so only RENDERED will do.
    """
    for version in reversed(document.versions):
        if version.status is DocumentStatus.RENDERED and version.artifact is not None:
            return version
    return None
