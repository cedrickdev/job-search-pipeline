"""Owner-scoped, authoritative browser context before any page navigation."""

import pytest

from backend.app.application_engine.browser.context_loader import ApplicationContextLoader
from backend.app.documents.artifacts import LocalDocumentArtifactStore
from backend.app.domain.application import (
    Application,
    ApplicationState,
    build_idempotency_key,
)
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.identifiers import application_id
from tests.v2_builders import (
    NOW,
    OPPORTUNITY,
    OTHER_PROFILE,
    OTHER_USER,
    PROFILE,
    USER,
    a_candidate_profile,
    a_decision,
    an_opportunity,
)
from tests.v2_fakes import (
    FakeApplicationRepository,
    FakeCandidateDocumentRepository,
    FakeCandidateProfileRepository,
    FakeOpportunityRepository,
)

CANONICAL_URL = "https://boards.greenhouse.io/fixture/jobs/123"


def _application(profile_id=PROFILE) -> Application:
    decision = a_decision(candidate_profile_id=profile_id)
    key = build_idempotency_key(
        candidate_profile_id=profile_id,
        channel=ApplicationChannel.BROWSER,
        opportunity_id=OPPORTUNITY)
    return Application(
        id=application_id(key), user_id=USER,
        candidate_profile_id=profile_id, decision_id=decision.id,
        channel=ApplicationChannel.BROWSER, state=ApplicationState.PLANNED,
        idempotency_key=key, opportunity_id=OPPORTUNITY,
        policy_id=decision.policy_id, created_at=NOW, updated_at=NOW)


def _loader(tmp_path, app, profiles):
    applications = FakeApplicationRepository()
    applications.applications[app.id] = app
    opportunities = FakeOpportunityRepository()
    opportunities.opportunities[OPPORTUNITY] = an_opportunity(application_url=CANONICAL_URL)
    loader = ApplicationContextLoader(
        applications=applications, opportunities=opportunities, profiles=profiles,
        documents=FakeCandidateDocumentRepository(),
        artifact_store=LocalDocumentArtifactStore(tmp_path / "artifacts"),
        staging_dir=tmp_path / "staging")
    return loader


@pytest.mark.asyncio
async def test_browser_loader_rejects_forged_target_url(tmp_path):
    app = _application()
    profiles = FakeCandidateProfileRepository()
    profiles.profiles[PROFILE] = a_candidate_profile()
    loader = _loader(tmp_path, app, profiles)

    loaded = await loader.load(
        USER, app.id,
        target_url="https://boards.greenhouse.io/attacker/jobs/999",
        correlation_id=None)

    assert loaded is None


@pytest.mark.asyncio
async def test_browser_loader_uses_pinned_application_profile(tmp_path):
    app = _application(profile_id=OTHER_PROFILE)
    profiles = FakeCandidateProfileRepository()
    profiles.profiles[PROFILE] = a_candidate_profile(display_name="Wrong default")
    profiles.profiles[OTHER_PROFILE] = a_candidate_profile(
        id=OTHER_PROFILE, user_id=USER, display_name="Pinned candidate")
    loader = _loader(tmp_path, app, profiles)

    loaded = await loader.load(
        USER, app.id, target_url=CANONICAL_URL, correlation_id=None)

    assert loaded is not None
    assert loaded.profile.id == OTHER_PROFILE
    assert loaded.profile.display_name == "Pinned candidate"


@pytest.mark.asyncio
async def test_browser_loader_cannot_read_another_accounts_application(tmp_path):
    app = _application()
    profiles = FakeCandidateProfileRepository()
    profiles.profiles[PROFILE] = a_candidate_profile()
    loader = _loader(tmp_path, app, profiles)

    assert await loader.load(
        OTHER_USER, app.id, target_url=CANONICAL_URL,
        correlation_id=None) is None


@pytest.mark.asyncio
async def test_browser_loader_rejects_missing_persisted_target(tmp_path):
    app = _application()
    profiles = FakeCandidateProfileRepository()
    profiles.profiles[PROFILE] = a_candidate_profile()
    loader = _loader(tmp_path, app, profiles)
    loader._opportunities.opportunities[OPPORTUNITY] = an_opportunity(application_url=None)

    assert await loader.load(USER, app.id, target_url=CANONICAL_URL,
                             correlation_id=None) is None


@pytest.mark.asyncio
async def test_browser_loader_rejects_foreign_pinned_profile(tmp_path):
    app = _application(profile_id=OTHER_PROFILE)
    profiles = FakeCandidateProfileRepository()
    profiles.profiles[OTHER_PROFILE] = a_candidate_profile(
        id=OTHER_PROFILE, user_id=OTHER_USER)
    loader = _loader(tmp_path, app, profiles)

    assert await loader.load(USER, app.id, target_url=CANONICAL_URL,
                             correlation_id=None) is None
