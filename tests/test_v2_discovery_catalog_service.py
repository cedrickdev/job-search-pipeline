"""Owner-scoped, durable Swiss discovery with synthetic source responses only."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.discovery.contracts import SourceHealthStatus
from backend.app.cli.run_discovery import enqueue_for_user
from backend.app.discovery.orchestrator import DiscoveryOrchestrator
from backend.app.discovery.registry import SourceRegistry
from backend.app.domain.discovery_run import DiscoveryRunStatus
from backend.app.domain.identifiers import DiscoveryRunId, OpportunityId
from backend.app.domain.task import TaskKind, TaskLane
from backend.app.infrastructure.database.engine import session_scope
from backend.app.repositories.sqlalchemy_repositories import SqlAlchemySearchProfileRepository
from backend.app.repositories.sqlalchemy_repositories import SqlAlchemyTaskRunRepository
from backend.app.services.discovery_catalog import (
    DiscoveryCatalogService,
    DiscoveryEnqueueService,
)
from backend.app.infrastructure.database.models import (
    DiscoveryMembershipRow,
    DiscoveryRunRow,
    OpportunityRow,
    SearchProfileRow,
    TaskRunRow,
    UserRow,
)
from country_packs.contracts import SourceBinding
from country_packs.registry import CountryPackRegistry
from tests.v2_builders import (
    OTHER_USER, SEARCH_PROFILE, USER, a_search_profile, a_source_record,
    an_opportunity,
)
from tests.v2_discovery import NOW, FakeSource, a_metadata, a_pack, a_result
from tests.v2_rows import a_user_row
from backend.app.tasks.dispatcher import PersistedTaskDispatcher

pytestmark = pytest.mark.asyncio


def posting(number: int, *, source_key: str, external_id: str, source_url: str,
            title: str):
    return an_opportunity(
        id=OpportunityId(UUID(f"00000000-0000-4000-8000-0000000003{number:02d}")),
        source=a_source_record(source_key=source_key, external_id=external_id,
                               source_url=source_url),
        title=title, dedup_fingerprint="same-title-hash",
    )


def orchestrator(*sources):
    registry = SourceRegistry()
    registry.register_all(sources)
    pack = a_pack("CH", *(SourceBinding(source_key=key) for key in registry.source_keys))
    return DiscoveryOrchestrator(
        registry=registry, packs=CountryPackRegistry((pack,)), clock=lambda: NOW)


@pytest_asyncio.fixture
async def catalog_database(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            session.add_all([a_user_row(id=USER), a_user_row(id=OTHER_USER)])
            await session.flush()
            await SqlAlchemySearchProfileRepository(session).upsert(a_search_profile())
    try:
        yield factory
    finally:
        async with factory() as session:
            async with session.begin():
                await session.execute(delete(UserRow).where(UserRow.id.in_((USER, OTHER_USER))))
                await session.execute(delete(OpportunityRow).where(OpportunityRow.id.in_(
                    UUID(f"00000000-0000-4000-8000-0000000003{number:02d}")
                    for number in range(21, 27)
                )))


async def test_owner_run_is_persisted_and_foreign_user_cannot_read_it(catalog_database):
    item = posting(21, source_key="fake_board", external_id="role-21",
                   source_url="https://example.ch/jobs/21", title="Engineer")
    source = FakeSource(a_metadata("fake_board"), result=a_result("fake_board", item))
    service = DiscoveryCatalogService(catalog_database, orchestrator(source), clock=lambda: NOW)

    run_id = await service.run_profile(USER, SEARCH_PROFILE, trigger="MANUAL")
    restarted = DiscoveryCatalogService(catalog_database, orchestrator(source), clock=lambda: NOW)
    run = await restarted.get_run(USER, run_id)
    assert run is not None and run.status is DiscoveryRunStatus.SUCCEEDED
    assert run.matched_count == 1
    assert await restarted.list_members(USER, SEARCH_PROFILE, limit=10, offset=0) == (item.id,)
    assert await restarted.get_run(OTHER_USER, run_id) is None
    assert await restarted.list_members(OTHER_USER, SEARCH_PROFILE, limit=10, offset=0) == ()
    assert (await restarted.get_latest_run(USER, SEARCH_PROFILE)).id == run_id
    assert await restarted.get_latest_run(OTHER_USER, SEARCH_PROFILE) is None


async def test_inactive_or_foreign_search_never_starts_a_sweep(catalog_database):
    source = FakeSource(a_metadata("fake_board"))
    service = DiscoveryCatalogService(catalog_database, orchestrator(source), clock=lambda: NOW)
    with pytest.raises(LookupError):
        await service.run_profile(OTHER_USER, SEARCH_PROFILE, trigger="MANUAL")
    async with catalog_database() as session:
        async with session.begin():
            row = (await session.execute(select(SearchProfileRow).where(
                SearchProfileRow.id == SEARCH_PROFILE))).scalar_one()
            row.is_active = False
    with pytest.raises(LookupError):
        await service.run_profile(USER, SEARCH_PROFILE, trigger="MORNING")
    assert source.requests == []


async def test_database_rejects_run_that_claims_another_users_search(catalog_database):
    with pytest.raises(IntegrityError):
        async with catalog_database() as session:
            async with session.begin():
                session.add(DiscoveryRunRow(
                    id=uuid4(), user_id=OTHER_USER, search_profile_id=SEARCH_PROFILE,
                    country="CH", trigger="MANUAL", status="RUNNING",
                    started_at=NOW, finished_at=None, seen_count=0,
                    matched_count=0, source_health=[], warning_codes=[],
                ))
                await session.flush()


async def test_partial_failure_preserves_previous_membership_and_last_seen(catalog_database):
    item = posting(22, source_key="fake_board", external_id="role-22",
                   source_url="https://example.ch/jobs/22", title="Engineer")
    first_source = FakeSource(a_metadata("fake_board"), result=a_result("fake_board", item))
    first = DiscoveryCatalogService(catalog_database, orchestrator(first_source),
                                    clock=lambda: NOW)
    await first.run_profile(USER, SEARCH_PROFILE, trigger="MANUAL")
    async with catalog_database() as session:
        before = (await session.execute(select(DiscoveryMembershipRow.last_seen_at))).scalar_one()

    bad = FakeSource(a_metadata("fake_board"), error=TimeoutError("synthetic timeout"))
    later = NOW + timedelta(days=1)
    second = DiscoveryCatalogService(catalog_database, orchestrator(bad),
                                     clock=lambda: later)
    run_id = await second.run_profile(USER, SEARCH_PROFILE, trigger="MORNING")
    run = await second.get_run(USER, run_id)
    assert run is not None and run.status is DiscoveryRunStatus.PARTIAL
    assert (await second.get_latest_run(USER, SEARCH_PROFILE)).id == run_id
    assert run.source_health[0].status == SourceHealthStatus.UNAVAILABLE
    assert await second.list_members(USER, SEARCH_PROFILE, limit=10, offset=0) == (item.id,)
    async with catalog_database() as session:
        after = (await session.execute(select(DiscoveryMembershipRow.last_seen_at))).scalar_one()
    assert after == before


async def test_repeated_run_refreshes_existing_membership_without_duplication(catalog_database):
    item = posting(23, source_key="fake_board", external_id="role-23",
                   source_url="https://example.ch/jobs/23", title="Engineer")
    source = FakeSource(a_metadata("fake_board"), result=a_result("fake_board", item))
    await DiscoveryCatalogService(catalog_database, orchestrator(source),
                                  clock=lambda: NOW).run_profile(
        USER, SEARCH_PROFILE, trigger="MANUAL")
    later = NOW + timedelta(days=1)
    await DiscoveryCatalogService(catalog_database, orchestrator(source),
                                  clock=lambda: later).run_profile(
        USER, SEARCH_PROFILE, trigger="MORNING")
    async with catalog_database() as session:
        memberships = (await session.execute(select(DiscoveryMembershipRow))).scalars().all()
    assert len(memberships) == 1
    assert memberships[0].first_seen_at == NOW
    assert memberships[0].last_seen_at == later
    assert memberships[0].last_changed_at is None

    changed = posting(23, source_key="fake_board", external_id="role-23",
                      source_url="https://example.ch/jobs/23", title="Senior Engineer")
    third_at = NOW + timedelta(days=2)
    changed_source = FakeSource(a_metadata("fake_board"),
                                result=a_result("fake_board", changed))
    await DiscoveryCatalogService(catalog_database, orchestrator(changed_source),
                                  clock=lambda: third_at).run_profile(
        USER, SEARCH_PROFILE, trigger="MORNING")
    async with catalog_database() as session:
        membership = (await session.execute(select(DiscoveryMembershipRow))).scalar_one()
    assert membership.first_seen_at == NOW
    assert membership.last_seen_at == third_at
    assert membership.last_changed_at == third_at


async def test_deleting_profile_cascades_runs_and_memberships(catalog_database):
    item = posting(24, source_key="fake_board", external_id="role-24",
                   source_url="https://example.ch/jobs/24", title="Engineer")
    source = FakeSource(a_metadata("fake_board"), result=a_result("fake_board", item))
    service = DiscoveryCatalogService(catalog_database, orchestrator(source), clock=lambda: NOW)
    run_id = await service.run_profile(USER, SEARCH_PROFILE, trigger="MANUAL")
    async with catalog_database() as session:
        async with session.begin():
            assert await SqlAlchemySearchProfileRepository(session).delete(
                USER, SEARCH_PROFILE)
    assert await service.get_run(USER, run_id) is None
    assert await service.list_members(USER, SEARCH_PROFILE, limit=10, offset=0) == ()


async def test_unexpected_sweep_failure_is_reported_without_partial_commit(catalog_database):
    class FailingOrchestrator:
        async def sweep(self, profile, *, country):
            raise RuntimeError("synthetic failure with private detail")

    service = DiscoveryCatalogService(catalog_database, FailingOrchestrator(),
                                      clock=lambda: NOW)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        await service.run_profile(USER, SEARCH_PROFILE, trigger="MANUAL")
    async with catalog_database() as session:
        rows = (await session.execute(select(DiscoveryRunRow))).scalars().all()
        members = (await session.execute(select(DiscoveryMembershipRow))).scalars().all()
    assert len(rows) == 1 and rows[0].status is DiscoveryRunStatus.FAILED
    assert rows[0].warning_codes == ["RUN_FAILED"]
    assert members == []


async def test_run_write_cap_reports_partial_coverage(catalog_database):
    first = posting(25, source_key="fake_board", external_id="role-25",
                    source_url="https://example.ch/jobs/25", title="Engineer")
    second = posting(26, source_key="fake_board", external_id="role-26",
                     source_url="https://example.ch/jobs/26", title="Designer")
    source = FakeSource(a_metadata("fake_board"),
                        result=a_result("fake_board", first, second))
    service = DiscoveryCatalogService(catalog_database, orchestrator(source),
                                      clock=lambda: NOW, max_observations=1)
    run_id = await service.run_profile(USER, SEARCH_PROFILE, trigger="MANUAL")
    run = await service.get_run(USER, run_id)
    assert run is not None and run.status is DiscoveryRunStatus.PARTIAL
    assert run.matched_count == 1
    assert "CATALOG_LIMIT_TRUNCATED" in run.warning_codes
    assert await service.list_members(USER, SEARCH_PROFILE, limit=10, offset=0) == (first.id,)


async def test_duplicate_manual_enqueues_share_one_general_lane_task(catalog_database):
    task_ids = []
    for _ in range(2):
        async with session_scope(catalog_database) as session:
            enqueue = DiscoveryEnqueueService(
                SqlAlchemySearchProfileRepository(session),
                PersistedTaskDispatcher(SqlAlchemyTaskRunRepository(session), clock=lambda: NOW),
                clock=lambda: NOW)
            task_ids.append(await enqueue.enqueue_discovery(
                USER, SEARCH_PROFILE, trigger="MANUAL"))
    assert task_ids[0] == task_ids[1]
    async with catalog_database() as session:
        rows = (await session.execute(select(TaskRunRow))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind is TaskKind.OPPORTUNITY_DISCOVERY
    assert rows[0].lane is TaskLane.GENERAL
    assert rows[0].payload == {"search_profile_id": str(SEARCH_PROFILE),
                               "trigger": "MANUAL"}


async def test_local_morning_dry_run_queues_nothing(catalog_database):
    count = await enqueue_for_user(catalog_database, USER, dry_run=True,
                                   trigger="MORNING", clock=lambda: NOW)
    assert count == 1
    async with catalog_database() as session:
        assert (await session.execute(select(TaskRunRow))).scalars().all() == []


async def test_redelivered_task_does_not_repeat_a_finished_sweep(catalog_database):
    source = FakeSource(a_metadata("fake_board"), result=a_result("fake_board"))
    service = DiscoveryCatalogService(catalog_database, orchestrator(source), clock=lambda: NOW)
    task_run_id = DiscoveryRunId(UUID("99999999-9999-4999-8999-999999999999"))

    first = await service.run_profile(USER, SEARCH_PROFILE, trigger="MANUAL",
                                      run_id=task_run_id)
    second = await service.run_profile(USER, SEARCH_PROFILE, trigger="MANUAL",
                                       run_id=task_run_id)

    assert first == second == task_run_id
    assert len(source.requests) == 1
    async with catalog_database() as session:
        assert len((await session.execute(select(DiscoveryRunRow))).scalars().all()) == 1
