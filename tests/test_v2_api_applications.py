"""`/api/v2/applications` over HTTP: the lifecycle, the ownership, the error map.

The service's decisions are pinned by `test_v2_application_engine.py` over the fakes;
this covers what only the HTTP boundary shows — that the owner comes from the session
(never the path), that creation is idempotent and its failures map to the right
status, that a read cannot be probed across accounts, and that the audit trail is
served in order. The harness wires a *fallback-only* registry (the generic adapter),
so a prepared application routes to a human — the cautious API default — and the deep
auto-submit path stays the unit suite's job.
"""
from uuid import UUID
from datetime import date

import pytest

from backend.app.api.dependencies import application_queue_service
from backend.app.application_engine.browser.form_capabilities import (
    FormCapability, FormCapabilityCatalog,
)
from backend.app.domain.application import ApplicationState
from backend.app.domain.policy import AutomationMode
from backend.app.domain.identifiers import (
    OpportunityId,
    default_candidate_profile_id,
)
from backend.app.services.application_queue import ApplicationQueueService
from tests.v2_api import OTHER_EMAIL, api_harness
from tests.v2_builders import a_decision, a_policy, an_opportunity
from tests.v2_fakes import FakeTaskDispatcher

PLACEHOLDER = "00000000-0000-4000-8000-0000000000ff"
POSTING = OpportunityId(UUID("00000000-0000-4000-8000-0000000002ff"))
FORM_URL = "https://jobs.lever.co/fixture/role/apply"
FINGERPRINT = "a" * 64


def _fixture_catalog():
    return FormCapabilityCatalog(forms=(FormCapability(
        id="lever-fixture", ats="lever", employer="Fixture SA",
        target_url=FORM_URL, driver_key="lever/1", form_fingerprint=FINGERPRINT,
        allowed_required_fields=frozenset(), allowed_upload_slots=frozenset(),
        conditions_url="https://fixture.example/terms", review_id="fixture-review",
        reviewed_at=date(2026, 10, 13), access_decision="approved",
    ),))


async def _seed_pair(api):
    """Sign in, finish onboarding, and store a posting plus its AUTO_APPLY decision."""
    await api.sign_in()
    await api.finish_onboarding()
    user_id = next(iter(api.users.users))
    profile_id = default_candidate_profile_id(user_id)
    await api.postings.upsert(an_opportunity(id=POSTING))
    await api.application_decisions.upsert(a_decision(
        user_id=user_id, candidate_profile_id=profile_id, opportunity_id=POSTING))
    return user_id, POSTING


async def _allow_fixture_queue(api, *, live_enabled=True):
    owner = next(iter(api.users.users))
    await api.application_policies.upsert(a_policy(
        user_id=owner, mode=AutomationMode.SUPERVISED))
    dispatcher = FakeTaskDispatcher()
    api.app.dependency_overrides[application_queue_service] = lambda: ApplicationQueueService(
        api.applications, api.application_policies, api.postings, dispatcher,
        live_enabled=live_enabled, form_catalog=_fixture_catalog())
    return dispatcher


@pytest.mark.asyncio
async def test_create_requires_a_decision(tmp_path):
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await api.finish_onboarding()
        await api.postings.upsert(an_opportunity(id=POSTING))
        response = await api.write("POST", "/applications",
                                   json={"opportunity_id": str(POSTING)})
        assert response.status_code == 409
        assert response.json()["error"] == "application_decision_missing"


@pytest.mark.asyncio
async def test_create_is_idempotent(tmp_path):
    async with api_harness(tmp_path) as api:
        _, posting_id = await _seed_pair(api)
        first = await api.write("POST", "/applications",
                                json={"opportunity_id": str(posting_id)})
        assert first.status_code == 200, first.text
        assert first.json()["state"] == "PLANNED"
        second = await api.write("POST", "/applications",
                                 json={"opportunity_id": str(posting_id)})
        assert second.json()["id"] == first.json()["id"]
        assert len(api.applications.applications) == 1


@pytest.mark.asyncio
async def test_prepare_routes_to_a_human_on_the_fallback_channel(tmp_path):
    async with api_harness(tmp_path) as api:
        _, posting_id = await _seed_pair(api)
        created = await api.write("POST", "/applications",
                                  json={"opportunity_id": str(posting_id)})
        app_id = created.json()["id"]
        prepared = await api.write("POST", f"/applications/{app_id}/prepare")
        assert prepared.status_code == 200, prepared.text
        # The generic fallback prepares nothing it cannot verify and hands off (§13).
        assert prepared.json()["state"] == "REQUIRES_HUMAN"


@pytest.mark.asyncio
async def test_the_trail_is_served_in_order(tmp_path):
    async with api_harness(tmp_path) as api:
        _, posting_id = await _seed_pair(api)
        created = await api.write("POST", "/applications",
                                  json={"opportunity_id": str(posting_id)})
        app_id = created.json()["id"]
        await api.write("POST", f"/applications/{app_id}/prepare")
        events = await api.read(f"/applications/{app_id}/events")
        assert events.status_code == 200
        types = [e["event_type"] for e in events.json()["events"]]
        assert types[0] == "CREATED"
        assert "HUMAN_REQUIRED" in types


@pytest.mark.asyncio
async def test_submit_before_approval_is_a_conflict(tmp_path):
    async with api_harness(tmp_path) as api:
        _, posting_id = await _seed_pair(api)
        created = await api.write("POST", "/applications",
                                  json={"opportunity_id": str(posting_id)})
        app_id = created.json()["id"]
        response = await api.write("POST", f"/applications/{app_id}/submit")
        assert response.status_code == 409
        assert response.json()["error"] == "application_not_actionable"


@pytest.mark.asyncio
async def test_duplicate_queue_returns_one_browser_task(tmp_path):
    async with api_harness(tmp_path) as api:
        _, posting_id = await _seed_pair(api)
        await api.postings.upsert(an_opportunity(id=POSTING, application_url=FORM_URL))
        created = await api.write("POST", "/applications",
                                  json={"opportunity_id": str(posting_id)})
        app_id = UUID(created.json()["id"])
        app = api.applications.applications[app_id]
        await api.applications.upsert(app.model_copy(update={
            "state": ApplicationState.APPROVED,
            "form_driver_key": "lever/1", "form_fingerprint": FINGERPRINT,
            "form_capability_id": "lever-fixture"}))
        dispatcher = await _allow_fixture_queue(api)

        first = await api.write("POST", f"/applications/{app_id}/queue")
        second = await api.write("POST", f"/applications/{app_id}/queue")

        assert first.status_code == second.status_code == 202
        assert first.json()["task_id"] == second.json()["task_id"]
        assert len(dispatcher.enqueued) == 1
        assert dispatcher.enqueued[0].max_attempts == 1


@pytest.mark.asyncio
async def test_queue_is_disabled_without_live_switch(tmp_path):
    async with api_harness(tmp_path) as api:
        _, posting_id = await _seed_pair(api)
        created = await api.write("POST", "/applications",
                                  json={"opportunity_id": str(posting_id)})
        app_id = UUID(created.json()["id"])
        app = api.applications.applications[app_id]
        await api.applications.upsert(app.model_copy(update={
            "state": ApplicationState.APPROVED}))
        dispatcher = await _allow_fixture_queue(api, live_enabled=False)

        response = await api.write("POST", f"/applications/{app_id}/queue")

        assert response.status_code == 409
        assert response.json()["error"] == "application_browser_disabled"
        assert dispatcher.enqueued == []


@pytest.mark.asyncio
async def test_queue_cannot_use_another_accounts_application(tmp_path):
    async with api_harness(tmp_path) as api:
        _, posting_id = await _seed_pair(api)
        created = await api.write("POST", "/applications",
                                  json={"opportunity_id": str(posting_id)})
        app_id = created.json()["id"]
        dispatcher = await _allow_fixture_queue(api)
        await api.register(email=OTHER_EMAIL, display_name="Intruder")
        await api.log_in(email=OTHER_EMAIL)

        response = await api.write("POST", f"/applications/{app_id}/queue")

        assert response.status_code == 404
        assert dispatcher.enqueued == []


@pytest.mark.asyncio
async def test_paused_policy_cannot_queue_live_submission(tmp_path):
    async with api_harness(tmp_path) as api:
        owner, posting_id = await _seed_pair(api)
        created = await api.write("POST", "/applications",
                                  json={"opportunity_id": str(posting_id)})
        app_id = UUID(created.json()["id"])
        app = api.applications.applications[app_id]
        await api.applications.upsert(app.model_copy(update={
            "state": ApplicationState.APPROVED}))
        dispatcher = await _allow_fixture_queue(api)
        policy = await api.application_policies.get_default(owner)
        await api.application_policies.upsert(policy.model_copy(update={
            "is_active": False}))

        response = await api.write("POST", f"/applications/{app_id}/queue")

        assert response.status_code == 409
        assert response.json()["error"] == "application_browser_not_permitted"
        assert dispatcher.enqueued == []


@pytest.mark.asyncio
async def test_approved_browser_application_cannot_submit_directly(tmp_path):
    async with api_harness(tmp_path) as api:
        _, posting_id = await _seed_pair(api)
        created = await api.write("POST", "/applications",
                                  json={"opportunity_id": str(posting_id)})
        app_id = UUID(created.json()["id"])
        app = api.applications.applications[app_id]
        await api.applications.upsert(app.model_copy(update={
            "state": ApplicationState.APPROVED}))

        response = await api.write("POST", f"/applications/{app_id}/submit")

        assert response.status_code == 409
        assert response.json()["error"] == "application_browser_not_permitted"


@pytest.mark.asyncio
async def test_cancel_closes_the_application(tmp_path):
    async with api_harness(tmp_path) as api:
        _, posting_id = await _seed_pair(api)
        created = await api.write("POST", "/applications",
                                  json={"opportunity_id": str(posting_id)})
        app_id = created.json()["id"]
        cancelled = await api.write("POST", f"/applications/{app_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "CANCELLED"


@pytest.mark.asyncio
async def test_an_unknown_application_is_not_found(tmp_path):
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        response = await api.read(f"/applications/{PLACEHOLDER}")
        assert response.status_code == 404
        assert response.json()["error"] == "application_not_found"


@pytest.mark.asyncio
async def test_another_account_cannot_read_the_application(tmp_path):
    async with api_harness(tmp_path) as api:
        _, posting_id = await _seed_pair(api)
        created = await api.write("POST", "/applications",
                                  json={"opportunity_id": str(posting_id)})
        app_id = created.json()["id"]
        # A second account signs in over the same client, replacing the session
        # cookie, so the read now carries the intruder's identity.
        await api.register(email=OTHER_EMAIL, display_name="Intruder")
        await api.log_in(email=OTHER_EMAIL)
        response = await api.read(f"/applications/{app_id}")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_the_endpoints_require_a_session(tmp_path):
    async with api_harness(tmp_path) as api:
        response = await api.client.get(api.url("/applications"))
        assert response.status_code == 401
        assert response.json()["error"] == "not_authenticated"
