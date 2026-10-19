from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.app.domain.candidate import CandidateProfile, ReferenceCvPurpose
from backend.app.documents import CandidateEvidenceGuard, DeterministicDocumentGenerator
from backend.app.domain.documents import GenerationContext
from tests.v2_builders import an_opportunity
from backend.app.domain.identifiers import default_candidate_profile_id
from backend.app.services.reference_cv import ReferenceCvService
from tests.test_v2_onboarding import account, profile_draft
from tests.v2_fakes import FakeCandidateProfileRepository


NOW = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_import_reference_cv_stores_purpose_and_grounded_evidence(tmp_path: Path) -> None:
    profiles = FakeCandidateProfileRepository()
    service = ReferenceCvService(profiles, tmp_path)
    user = account()
    await profiles.upsert(CandidateProfile(
        id=default_candidate_profile_id(user.id), user_id=user.id,
        display_name="Cedrick Vanel", updated_at=NOW))

    saved = await service.import_file(
        user.id, filename="reference.txt", content=b"Python backend engineer\nSwiss internship 2026",
        purpose=ReferenceCvPurpose.INTERNSHIP, now=NOW,
    )

    assert saved.reference_cv_purpose is ReferenceCvPurpose.INTERNSHIP
    assert saved.reference_cv_filename == "reference.txt"
    assert len(saved.evidence) == 2
    assert all(item.source_document == saved.reference_cv_storage_key for item in saved.evidence)
    assert (tmp_path / saved.reference_cv_storage_key).read_bytes().startswith(b"Python")

    opportunity = an_opportunity()
    resume = await DeterministicDocumentGenerator().generate_resume(
        profile=saved, opportunity=opportunity,
        context=GenerationContext(document_type="RESUME", target_language="en",
                                  candidate_profile_id=saved.id, opportunity_id=opportunity.id))
    assert [item.text for item in resume.highlights]
    assert CandidateEvidenceGuard().review(resume, profile=saved, opportunity=opportunity).ok


@pytest.mark.asyncio
async def test_import_reference_cv_rejects_unsupported_files(tmp_path: Path) -> None:
    profiles = FakeCandidateProfileRepository()
    service = ReferenceCvService(profiles, tmp_path)
    with pytest.raises(ValueError, match="unsupported"):
        await service.import_file(
            account().id, filename="resume.exe", content=b"MZ", purpose=ReferenceCvPurpose.JOB,
            now=NOW,
        )
