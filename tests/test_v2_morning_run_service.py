"""V2 batch enqueue is owner-scoped and reuses the durable discovery key."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from backend.app.domain.identifiers import OpportunityId, SearchProfileId, UserId
from backend.app.domain.discovery_run import (
    DiscoveryRun,
    DiscoveryRunStatus,
    DiscoveryTrigger,
    SourceHealthSummary,
)
from backend.app.domain.search import CountrySearchArea
from backend.app.domain.task import TaskFailureClass, TaskKind, TaskSpec, TaskStatus
from backend.app.infrastructure.database.models import TaskRunRow
from backend.app.repositories.discovery_catalog import DiscoveryRunStore
from backend.app.repositories.morning_run import MorningRunStatusRepository
from backend.app.repositories.morning_preparation import MorningPreparationRepository
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemySearchProfileRepository,
    SqlAlchemyTaskRunRepository,
    SqlAlchemyOpportunityRepository,
)
from backend.app.services.discovery_catalog import DiscoveryEnqueueService
from backend.app.services.morning_run import MorningRunService
from backend.app.tasks.dispatcher import PersistedTaskDispatcher
from tests.v2_builders import a_search_profile, an_opportunity
from tests.v2_rows import a_user_row


@pytest.mark.asyncio
async def test_empty_owner_gets_no_morning_tasks(db_session) -> None:
    owner = UserId(uuid4())
    db_session.add(a_user_row(id=owner))
    await db_session.flush()
    searches = SqlAlchemySearchProfileRepository(db_session)
    tasks = SqlAlchemyTaskRunRepository(db_session)
    def now() -> datetime:
        return datetime(2026, 10, 12, 6, tzinfo=UTC)
    service = MorningRunService(
        searches, DiscoveryEnqueueService(
            searches, PersistedTaskDispatcher(tasks, clock=now), clock=now), tasks)

    assert await service.enqueue_active_ch(owner, trigger="MORNING") == ()
    assert (await db_session.execute(select(func.count()).select_from(TaskRunRow))).scalar_one() == 0


@pytest.mark.asyncio
async def test_two_owned_ch_searches_coalesce_by_swiss_day(db_session) -> None:
    owner, other = UserId(uuid4()), UserId(uuid4())
    db_session.add_all((a_user_row(id=owner), a_user_row(id=other)))
    await db_session.flush()
    searches = SqlAlchemySearchProfileRepository(db_session)
    eligible = [SearchProfileId(uuid4()) for _ in range(2)]
    for profile_id in eligible:
        await searches.upsert(a_search_profile(id=profile_id, user_id=owner))
    await searches.upsert(a_search_profile(id=SearchProfileId(uuid4()), user_id=owner,
                                           is_active=False))
    await searches.upsert(a_search_profile(id=SearchProfileId(uuid4()), user_id=owner,
                                           areas=(CountrySearchArea(country="FR"),)))
    await searches.upsert(a_search_profile(id=SearchProfileId(uuid4()), user_id=other))
    tasks = SqlAlchemyTaskRunRepository(db_session)
    instant = [datetime(2026, 10, 25, 0, 30, tzinfo=UTC)]
    def now() -> datetime:
        return instant[0]
    service = MorningRunService(
        searches, DiscoveryEnqueueService(
            searches, PersistedTaskDispatcher(tasks, clock=now), clock=now), tasks)

    first = await service.enqueue_active_ch(owner, trigger="MORNING")
    instant[0] = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # repeated Swiss hour
    second = await service.enqueue_active_ch(owner, trigger="MORNING")
    assert len(first) == 2
    assert {item.search_profile_id for item in first} == set(eligible)
    assert [item.run_id for item in first] == [item.run_id for item in second]
    assert all(item.status == "QUEUED" for item in second)
    assert (await db_session.execute(select(func.count()).select_from(TaskRunRow))).scalar_one() == 2

    instant[0] = datetime(2026, 10, 26, 7, tzinfo=UTC)
    next_day = await service.enqueue_active_ch(owner, trigger="MORNING")
    assert len({*(item.run_id for item in first), *(item.run_id for item in next_day)}) == 4
    assert (await db_session.execute(select(func.count()).select_from(TaskRunRow))).scalar_one() == 4


@pytest.mark.asyncio
async def test_latest_status_keeps_queued_and_partial_distinct_and_private(db_session) -> None:
    owner, other = UserId(uuid4()), UserId(uuid4())
    db_session.add_all((a_user_row(id=owner), a_user_row(id=other)))
    await db_session.flush()
    searches = SqlAlchemySearchProfileRepository(db_session)
    profile_id = SearchProfileId(uuid4())
    await searches.upsert(a_search_profile(id=profile_id, user_id=owner))
    tasks = SqlAlchemyTaskRunRepository(db_session)
    def clock() -> datetime:
        return datetime(2026, 10, 12, 6, tzinfo=UTC)
    service = MorningRunService(
        searches, DiscoveryEnqueueService(
            searches, PersistedTaskDispatcher(tasks, clock=clock), clock=clock), tasks,
        MorningRunStatusRepository(db_session), DiscoveryRunStore(db_session),
        MorningPreparationRepository(db_session))

    assert (await service.latest_for_user(owner))[0].status == "NOT_STARTED"
    queued = await service.enqueue_active_ch(owner, trigger="MORNING")
    assert (await service.latest_for_user(owner))[0].status == "QUEUED"
    assert await service.latest_for_user(other) == ()

    run = DiscoveryRun(
        id=queued[0].run_id, user_id=owner, search_profile_id=profile_id,
        country="CH", trigger=DiscoveryTrigger.MORNING,
        status=DiscoveryRunStatus.RUNNING, started_at=clock(),
        seen_count=0, matched_count=0,
    )
    store = DiscoveryRunStore(db_session)
    await store.create_run(run)
    await store.finish_run(run.model_copy(update={
        "status": DiscoveryRunStatus.PARTIAL,
        "finished_at": clock(), "seen_count": 3, "matched_count": 2,
        "warning_codes": ("SOURCE_TIMEOUT",),
        "source_health": (SourceHealthSummary(
            source_key="fixture_board", status="UNAVAILABLE", reason="SOURCE_TIMEOUT"),),
    }))
    opportunity_id = OpportunityId(uuid4())
    await SqlAlchemyOpportunityRepository(db_session).upsert(an_opportunity(id=opportunity_id))
    await MorningPreparationRepository(db_session).start_or_get(
        owner, run.id, opportunity_id, now=clock())
    status = (await service.latest_for_user(owner))[0]
    assert status.status == "PARTIAL"
    assert status.matched_count == 2
    assert status.warning_codes == ("SOURCE_TIMEOUT",)
    assert status.source_health[0].source_key == "fixture_board"
    assert len(status.preparations) == 1
    assert status.preparations[0].opportunity_id == opportunity_id
    matching_id = await PersistedTaskDispatcher(tasks, clock=clock).enqueue(TaskSpec(
        kind=TaskKind.MATCHING, user_id=owner,
        idempotency_key=f"morning-match:{owner}:{run.id}:{opportunity_id}",
        payload={"discovery_run_id": str(run.id),
                 "opportunity_id": str(opportunity_id)},
    ))
    matching_task = await tasks.get_by_id(matching_id)
    assert matching_task is not None
    failed = matching_task.leased(
        worker="fixture", lease_expires_at=clock() + timedelta(minutes=1),
        as_of=clock()).dead_lettered(
            failure_class=TaskFailureClass.PERMANENT,
            reason="PROVIDER_UNAVAILABLE", as_of=clock())
    await tasks.save(failed)
    after_failure = (await service.latest_for_user(owner))[0]
    assert after_failure.preparation_tasks[opportunity_id] == (
        TaskStatus.DEAD_LETTERED, "PROVIDER_UNAVAILABLE")
    assert await service.latest_for_user(other) == ()
