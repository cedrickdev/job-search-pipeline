"""Loading a submission's authoritative context from PostgreSQL, read-only (§6-7, §13).

The worker is handed only ids on a `BrowserTask` (§6) — never candidate content on a command line —
and rebuilds what it needs from the database here, scoped to the task's `user_id` so it can only
read that account's rows. It reads *domain state* and never mutates it (§7): the `Application`, its
resolved answers and pinned document versions, and the candidate profile. Transitioning the
application, appending an event or recording an attempt stays the `ApplicationService`'s job — the
worker's only output is a typed execution result.

Pinned documents become uploadable files here, and only the *exact* pinned versions (§13): each
`PinnedDocument` names a document id and a version number, and this resolves that version, checks it
is still a RENDERED, guard-cleared artifact, materializes its bytes to a private staging file, and
hands the driver that path — never "the latest" résumé, never a raw store key. A pin that no longer
resolves is simply left out, so a required upload with no file is caught downstream as
`UPLOAD_UNRESOLVED` rather than a wrong document being sent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from backend.app.application_engine.browser.contracts import DriverContext, ResolvedUpload
from backend.app.documents.artifacts import ArtifactNotFound, DocumentArtifactStore
from backend.app.domain.application import PinnedDocument
from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.documents import CandidateDocumentType, DocumentStatus
from backend.app.domain.identifiers import ApplicationId, UserId
from backend.app.repositories.contracts import (
    ApplicationRepository,
    CandidateDocumentRepository,
    CandidateProfileRepository,
    OpportunityRepository,
)

_FILENAME_SUFFIX: dict[CandidateDocumentType, str] = {
    CandidateDocumentType.RESUME: "CV",
    CandidateDocumentType.COVER_LETTER: "Cover_Letter",
}


@dataclass(frozen=True)
class LoadedApplication:
    """The read-only context a driver needs, plus the profile the worker proposes answers from."""

    profile: CandidateProfile
    context: DriverContext
    form_driver_key: str | None = None
    form_capability_id: str | None = None


class ApplicationContextLoader:
    """Rebuilds a submission's context from PostgreSQL, scoped to the task's account (§6-7)."""

    def __init__(self, *,
                 applications: ApplicationRepository,
                 opportunities: OpportunityRepository,
                 profiles: CandidateProfileRepository,
                 documents: CandidateDocumentRepository,
                 artifact_store: DocumentArtifactStore,
                 staging_dir: Path) -> None:
        self._applications = applications
        self._opportunities = opportunities
        self._profiles = profiles
        self._documents = documents
        self._artifacts = artifact_store
        self._staging = staging_dir

    async def load(self, user_id: UserId, application_id: ApplicationId, *,
                   target_url: str | None,
                   correlation_id: str | None) -> LoadedApplication | None:
        """The application's context, or `None` when it is absent or has no profile (§7)."""
        app = await self._applications.get(user_id, application_id)
        if app is None or app.opportunity_id is None:
            return None
        opportunity = await self._opportunities.get(app.opportunity_id)
        if opportunity is None or not _same_application_target(
                str(opportunity.application_url) if opportunity.application_url else None,
                target_url):
            return None
        profile = await self._profiles.get(user_id, app.candidate_profile_id)
        if profile is None:
            return None
        uploads = await self._resolve_uploads(user_id, app.pinned_documents, profile)
        context = DriverContext(
            target_url=str(opportunity.application_url),
            answers=app.answers,
            uploads=uploads,
            expected_form_fingerprint=app.form_fingerprint,
            correlation_id=correlation_id)
        return LoadedApplication(
            profile=profile, context=context,
            form_driver_key=app.form_driver_key,
            form_capability_id=app.form_capability_id,
        )


    async def _resolve_uploads(self, user_id: UserId,
                               pinned: tuple[PinnedDocument, ...],
                               profile: CandidateProfile) -> tuple[ResolvedUpload, ...]:
        """Materialize each still-valid pinned version to a staging file (§13)."""
        resolved: list[ResolvedUpload] = []
        for pin in pinned:
            document = await self._documents.get(user_id, pin.document_id)
            if document is None:
                continue
            version = document.version(pin.version)
            if version is None or version.status is not DocumentStatus.RENDERED \
                    or version.artifact is None:
                continue
            try:
                stored = self._artifacts.get(version.artifact.storage_key)
            except ArtifactNotFound:
                continue
            filename = self._filename(profile.display_name, pin.document_type)
            path = self._staging / f"{pin.document_id}_{pin.version}.pdf"
            path.write_bytes(stored.content)
            resolved.append(ResolvedUpload(
                document_type=pin.document_type, path=str(path), filename=filename))
        return tuple(resolved)

    def _filename(self, display_name: str, document_type: CandidateDocumentType) -> str:
        """A candidate-facing upload filename, e.g. ``Ada_Lovelace_CV.pdf`` (§13)."""
        stem = re.sub(r"[^A-Za-z0-9]+", "_", display_name).strip("_") or "Candidate"
        suffix = _FILENAME_SUFFIX.get(document_type, document_type.value)
        return f"{stem}_{suffix}.pdf"


def _same_application_target(persisted: str | None, requested: str | None) -> bool:
    """Only the stored HTTPS posting may be driven; never trust a queued URL alone."""
    if not persisted or not requested:
        return False
    try:
        left, right = urlsplit(persisted), urlsplit(requested)
        if left.scheme != "https" or right.scheme != "https":
            return False
        if any(part.username or part.password or part.fragment for part in (left, right)):
            return False
        return (left.hostname, left.port, left.path or "/", left.query) == (
            right.hostname, right.port, right.path or "/", right.query)
    except ValueError:
        return False
