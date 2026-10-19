"""A morning preparation belongs to one owner/run/opportunity and only advances."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from backend.app.domain.identifiers import DiscoveryRunId, OpportunityId, UserId
from backend.app.domain.morning_preparation import Preparation, PreparationStage


NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)


def _new() -> Preparation:
    return Preparation(user_id=UserId(uuid4()), discovery_run_id=DiscoveryRunId(uuid4()),
                       opportunity_id=OpportunityId(uuid4()), stage=PreparationStage.DISCOVERED,
                       updated_at=NOW)


def test_a_cv_checkpoint_cannot_be_lost_on_a_letter_retry() -> None:
    preparation = _new().advance(PreparationStage.MATCHED, at=NOW)
    cv_version = uuid4()
    preparation = preparation.record_document("RESUME", cv_version, at=NOW)
    assert preparation.resume_version_id == cv_version
    assert preparation.stage is PreparationStage.CV_RENDERED
    assert preparation.record_document("RESUME", cv_version, at=NOW) == preparation
    with pytest.raises(ValueError):
        preparation.record_document("RESUME", uuid4(), at=NOW)


def test_documents_rendered_requires_both_exact_versions() -> None:
    preparation = _new().advance(PreparationStage.MATCHED, at=NOW)
    with pytest.raises(ValueError):
        preparation.advance(PreparationStage.DOCUMENTS_RENDERED, at=NOW)
    preparation = preparation.record_document("RESUME", uuid4(), at=NOW)
    preparation = preparation.record_document("COVER_LETTER", uuid4(), at=NOW)
    assert preparation.stage is PreparationStage.DOCUMENTS_RENDERED
    with pytest.raises(ValueError):
        preparation.advance(PreparationStage.MATCHED, at=NOW)


def test_stopped_preparation_cannot_restart_without_a_new_run() -> None:
    preparation = _new().advance(PreparationStage.REQUIRES_HUMAN, at=NOW,
                                 reason_code="ELIGIBILITY_INCOMPLETE")
    with pytest.raises(ValueError):
        preparation.advance(PreparationStage.MATCHED, at=NOW)
