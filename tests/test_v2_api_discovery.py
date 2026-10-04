"""Authenticated discovery contract, with no live source or submission."""

from uuid import UUID

import pytest

from backend.app.api.dependencies import discovery_catalog_service, discovery_enqueue_service
from backend.app.api.dependencies import geo_search_service
from backend.app.domain.discovery_run import DiscoveryRun, DiscoveryRunStatus, DiscoveryTrigger
from backend.app.domain.identifiers import DiscoveryRunId, SearchProfileId
from backend.app.services.discovery_catalog import DiscoveryEnqueueService
from backend.app.domain.task import TaskKind, TaskSpec
from tests.v2_api import api_harness
from tests.v2_builders import OTHER_USER, a_search_profile


RUN_ID = DiscoveryRunId(UUID("00000000-0000-4000-8000-000000000d51"))
MINE = SearchProfileId(UUID("00000000-0000-4000-8000-000000000d52"))
THEIRS = SearchProfileId(UUID("00000000-0000-4000-8000-000000000d53"))


class Dispatcher:
    def __init__(self):
        self.keys = set()

    async def enqueue(self, spec):
        self.keys.add(spec.idempotency_key)
        return RUN_ID


class Catalog:
    def __init__(self, run, task=None):
        self.run = run
        self.task = task

    async def get_run(self, user_id, run_id):
        return self.run if self.run and self.run.user_id == user_id and run_id == RUN_ID else None

    async def get_queued_task(self, user_id, run_id):
        return self.task if self.task and self.task.user_id == user_id and run_id == self.task.id else None


@pytest.mark.asyncio
async def test_manual_discovery_is_owner_scoped_and_idempotent(tmp_path):
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = next(iter(api.users.users))
        await api.searches.upsert(a_search_profile(id=MINE, user_id=user_id))
        await api.searches.upsert(a_search_profile(id=THEIRS, user_id=OTHER_USER))
        dispatcher = Dispatcher()
        api.app.dependency_overrides[discovery_enqueue_service] = lambda: DiscoveryEnqueueService(
            api.searches, dispatcher, clock=api.clock)

        first = await api.write("POST", f"/me/search-profiles/{MINE}/discovery-runs")
        again = await api.write("POST", f"/me/search-profiles/{MINE}/discovery-runs")
        foreign = await api.write("POST", f"/me/search-profiles/{THEIRS}/discovery-runs")

        assert first.status_code == 202, first.text
        assert first.json() == again.json() == {"run_id": str(RUN_ID), "status": "QUEUED"}
        assert len(dispatcher.keys) == 1
        assert foreign.status_code == 404
        assert "other" not in foreign.text.lower()


@pytest.mark.asyncio
async def test_inactive_search_cannot_enqueue(tmp_path):
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = next(iter(api.users.users))
        await api.searches.upsert(a_search_profile(id=MINE, user_id=user_id,
                                                   is_active=False))
        dispatcher = Dispatcher()
        api.app.dependency_overrides[discovery_enqueue_service] = lambda: DiscoveryEnqueueService(
            api.searches, dispatcher, clock=api.clock)
        response = await api.write("POST", f"/me/search-profiles/{MINE}/discovery-runs")
        assert response.status_code == 404
        assert dispatcher.keys == set()


@pytest.mark.asyncio
async def test_run_status_exposes_only_sanitized_owner_report(tmp_path):
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = next(iter(api.users.users))
        run = DiscoveryRun(
            id=RUN_ID, user_id=user_id, search_profile_id=MINE, country="CH",
            trigger=DiscoveryTrigger.MANUAL, status=DiscoveryRunStatus.PARTIAL,
            started_at=api.clock(), finished_at=api.clock(), seen_count=4,
            matched_count=2, warning_codes=("SOURCE_TIMEOUT",),
            source_health=(),
        )
        api.app.dependency_overrides[discovery_catalog_service] = lambda: Catalog(run)
        response = await api.read(f"/me/discovery-runs/{RUN_ID}")
        missing = await api.read("/me/discovery-runs/00000000-0000-4000-8000-000000000d54")

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "PARTIAL"
        assert response.json()["matched_count"] == 2
        assert response.json()["warning_codes"] == ["SOURCE_TIMEOUT"]
        assert "user_id" not in response.json()
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_queued_run_is_pollable_but_not_by_a_different_account(tmp_path):
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        owner = next(iter(api.users.users))
        task = TaskSpec(
            kind=TaskKind.OPPORTUNITY_DISCOVERY, user_id=owner,
            idempotency_key="discovery:CH:api-queue-example",
            payload={"search_profile_id": str(MINE), "trigger": "MANUAL"},
        ).to_queued_run(as_of=api.clock())
        api.app.dependency_overrides[discovery_catalog_service] = lambda: Catalog(None, task)
        pending = await api.read(f"/me/discovery-runs/{task.id}")
        await api.sign_in(email="other@example.com")
        foreign = await api.read(f"/me/discovery-runs/{task.id}")

        assert pending.status_code == 200, pending.text
        assert pending.json()["status"] == "QUEUED"
        assert pending.json()["source_health"] == []
        assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_catalog_query_is_explicit_only_for_saved_search(tmp_path):
    class Geo:
        def __init__(self):
            self.catalog = None

        async def opportunities_for_search_profile(self, user_id, search_id, *,
                                                   bounds, limit, offset, catalog):
            self.catalog = catalog
            return ()

    async with api_harness(tmp_path) as api:
        await api.sign_in()
        geo = Geo()
        api.app.dependency_overrides[geo_search_service] = lambda: geo
        response = await api.read(f"/me/search-profiles/{MINE}/opportunities?catalog=true")
        assert response.status_code == 200, response.text
        assert geo.catalog is True
        assert response.json()["opportunities"] == []
