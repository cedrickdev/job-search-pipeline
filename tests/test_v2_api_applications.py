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

import pytest

from backend.app.domain.identifiers import (
    OpportunityId,
    default_candidate_profile_id,
)
from tests.v2_api import OTHER_EMAIL, api_harness
from tests.v2_builders import a_decision, an_opportunity

PLACEHOLDER = "00000000-0000-4000-8000-0000000000ff"
POSTING = OpportunityId(UUID("00000000-0000-4000-8000-0000000002ff"))


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
