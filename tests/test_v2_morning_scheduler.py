"""Swiss scheduling survives repeats, replicas and the DST fallback hour."""

import asyncio
from datetime import UTC, datetime, time
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.cli.morning_scheduler import main, tick
from backend.app.domain.identifiers import SearchProfileId, UserId
from backend.app.domain.morning_schedule import MorningSchedule
from backend.app.infrastructure.database.models import TaskRunRow, UserRow
from backend.app.repositories.morning_schedule import SqlAlchemyMorningScheduleRepository
from backend.app.repositories.sqlalchemy_repositories import SqlAlchemySearchProfileRepository
from tests.v2_builders import a_search_profile
from tests.v2_rows import a_user_row


@pytest.fixture
def approved_synthetic_queue(monkeypatch):
    # These tests only inspect durable tasks; no source adapter is contacted.
    monkeypatch.setenv("JOBSEARCH_DISCOVERY_ALLOWED_SOURCES", "jooble")


async def seed(factory, *, count: int, at: time = time(8),
               cadence: str = "daily", enabled: bool = True) -> list[UserId]:
    owners = [UserId(uuid4()) for _ in range(count)]
    async with factory() as session, session.begin():
        searches = SqlAlchemySearchProfileRepository(session)
        schedules = SqlAlchemyMorningScheduleRepository(session)
        for owner in owners:
            session.add(a_user_row(id=owner))
            await session.flush()
            await searches.upsert(a_search_profile(
                id=SearchProfileId(uuid4()), user_id=owner))
            await schedules.upsert(MorningSchedule(
                user_id=owner, enabled=enabled, local_time=at,
                cadence=cadence, updated_at=datetime(2026, 10, 12, tzinfo=UTC)))
    return owners


async def task_count(factory, owners: list[UserId]) -> int:
    async with factory() as session:
        return (await session.execute(select(func.count()).select_from(TaskRunRow)
                .where(TaskRunRow.user_id.in_(owners)))).scalar_one()


async def cleanup(factory, owners: list[UserId]) -> None:
    async with factory() as session, session.begin():
        await session.execute(delete(UserRow).where(UserRow.id.in_(owners)))


@pytest.mark.asyncio
async def test_two_replicas_and_restarted_tick_do_not_duplicate_swiss_day(
    db_engine, approved_synthetic_queue,
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    owners = await seed(factory, count=1, at=time(2, 30))
    try:
        first_fold = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
        await asyncio.gather(tick(factory, first_fold), tick(factory, first_fold))
        assert await task_count(factory, owners) == 1
        await tick(factory, datetime(2026, 10, 25, 1, 30, tzinfo=UTC))
        await tick(factory, datetime(2026, 10, 25, 7, tzinfo=UTC))
        assert await task_count(factory, owners) == 1
        await tick(factory, datetime(2026, 10, 26, 7, tzinfo=UTC))
        assert await task_count(factory, owners) == 2
    finally:
        await cleanup(factory, owners)


@pytest.mark.asyncio
async def test_weekday_and_disabled_schedules_never_enqueue_on_sunday(
    db_engine, approved_synthetic_queue,
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    weekday = await seed(factory, count=1, cadence="weekdays")
    daily = await seed(factory, count=1)
    disabled = await seed(factory, count=1, enabled=False)
    owners = weekday + daily + disabled
    try:
        await tick(factory, datetime(2026, 10, 25, 7, tzinfo=UTC))
        assert await task_count(factory, weekday) == 0
        assert await task_count(factory, daily) == 1
        assert await task_count(factory, disabled) == 0
        async with factory() as session, session.begin():
            repo = SqlAlchemyMorningScheduleRepository(session)
            current = await repo.get(daily[0])
            assert current is not None
            await repo.upsert(current.model_copy(update={"enabled": False}))
        await tick(factory, datetime(2026, 10, 26, 7, tzinfo=UTC))
        assert await task_count(factory, daily) == 1  # queued task is not cancelled
        assert await task_count(factory, weekday) == 1
    finally:
        await cleanup(factory, owners)


@pytest.mark.asyncio
async def test_tick_pages_past_one_hundred_due_accounts(
    db_engine, approved_synthetic_queue,
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    owners = await seed(factory, count=101)
    try:
        processed = await tick(factory, datetime(2026, 10, 12, 6, tzinfo=UTC), page_size=25)
        assert processed == 101
        assert await task_count(factory, owners) == 101
    finally:
        await cleanup(factory, owners)


@pytest.mark.asyncio
async def test_tick_rejects_naive_clock_and_unbounded_pages(db_engine) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    with pytest.raises(ValueError):
        await tick(factory, datetime(2026, 10, 12, 8))
    with pytest.raises(ValueError):
        await tick(factory, datetime(2026, 10, 12, tzinfo=UTC), page_size=0)


def test_resident_scheduler_is_off_until_sources_are_reviewed(monkeypatch) -> None:
    monkeypatch.delenv("JOBSEARCH_MORNING_SCHEDULER_ENABLED", raising=False)
    assert main([]) == 0
    assert main(["--once"]) == 2
