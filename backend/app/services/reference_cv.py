"""Secure local import of a candidate's reference CV."""
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree

from backend.app.domain.candidate import (
    CandidateEvidence,
    CandidateProfile,
    EvidenceKind,
    EvidenceProvenance,
    ReferenceCvPurpose,
)
from backend.app.domain.identifiers import UserId, new_evidence_id
from backend.app.repositories.contracts import CandidateProfileRepository
from backend.app.services.assessment import CandidateProfileNotFound

MAX_REFERENCE_CV_BYTES = 10 * 1024 * 1024
_EXTENSIONS = {".pdf", ".docx", ".txt"}


class ReferenceCvService:
    def __init__(self, profiles: CandidateProfileRepository, root: Path) -> None:
        self._profiles = profiles
        self._root = root.resolve()

    async def import_file(self, user_id: UserId, *, filename: str, content: bytes,
                          purpose: ReferenceCvPurpose, now: datetime) -> CandidateProfile:
        suffix = Path(filename).suffix.lower()
        if suffix not in _EXTENSIONS:
            raise ValueError("unsupported reference CV file type")
        if not content or len(content) > MAX_REFERENCE_CV_BYTES:
            raise ValueError("reference CV file is empty or too large")
        profile = await self._profiles.get_default(user_id)
        if profile is None:
            raise CandidateProfileNotFound(str(user_id))
        text = _extract_text(content, suffix)
        lines = tuple(line.strip() for line in text.splitlines() if len(line.strip()) >= 3)[:100]
        if not lines:
            raise ValueError("reference CV contains no readable text")

        key = f"reference-cvs/{user_id}/{uuid4()}{suffix}"
        destination = (self._root / key).resolve()
        if self._root not in destination.parents:
            raise ValueError("reference CV storage path escapes its root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(0o600)

        evidence = profile.evidence + tuple(
            CandidateEvidence(
                id=new_evidence_id(), user_id=user_id, kind=EvidenceKind.CV_BULLET,
                provenance=EvidenceProvenance.IMPORTED_CV, summary=line,
                source_document=key, recorded_at=now)
            for line in lines)
        updated = profile.model_copy(update={
            "reference_cv_purpose": purpose,
            "reference_cv_filename": Path(filename).name,
            "reference_cv_storage_key": key,
            "evidence": evidence,
            "updated_at": now,
        })
        return await self._profiles.upsert(updated)


def _extract_text(content: bytes, suffix: str) -> str:
    if suffix == ".txt":
        return content.decode("utf-8", errors="replace")
    if suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages)
    with zipfile.ZipFile(BytesIO(content)) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    return "\n".join(node.text or "" for node in root.iter()
                       if node.tag.endswith("}t"))
