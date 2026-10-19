"""An account owns exactly one persisted schedule; scheduler reads are bounded."""

from datetime import UTC, datetime, time

import pytest

from backend.app.domain.identifiers import new_user_id
from backend.app.domain.morning_schedule import MorningSchedule
from backend.app.infrastructure.database.models import UserRow
from backend.app.repositories.morning_schedule import SqlAlchemyMorningScheduleRepository

_SYNTHETIC_HASH = "not-a-real-password-hash"


@pytest.mark.asyncio
async def test_get_and_upsert_are_owner_scoped(db_session) -> None:
    owner, other = new_user_id(), new_user_id()
    db_session.add_all([
        UserRow(id=owner, email="owner@example.ch", password_hash=_SYNTHETIC_HASH),
        UserRow(id=other, email="other@example.ch", password_hash=_SYNTHETIC_HASH),
    ])
    await db_session.flush()
    repo = SqlAlchemyMorningScheduleRepository(db_session)
    first = MorningSchedule(user_id=owner, enabled=True, local_time=time(8),
                            cadence="daily", updated_at=datetime(2026, 10, 12, tzinfo=UTC))
    assert await repo.get(owner) is None
    assert await repo.upsert(first) == first
    assert await repo.get(other) is None
    assert await repo.get(owner) == first

    changed = first.model_copy(update={"local_time": time(9)})
    assert await repo.upsert(changed) == changed
    assert await repo.get(owner) == changed


@pytest.mark.asyncio
async def test_due_list_filters_disabled_and_later_clocks_and_pages(db_session) -> None:
    repo = SqlAlchemyMorningScheduleRepository(db_session)
    now = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)  # 08:00 Zurich
    owners = [new_user_id() for _ in range(4)]
    for i, owner in enumerate(owners):
        db_session.add(UserRow(id=owner, email=f"schedule-{i}@example.ch",
                               password_hash=_SYNTHETIC_HASH))
    await db_session.flush()
    for owner, enabled, at in zip(owners, [True, True, False, True],
                                  [time(8), time(7), time(8), time(9)], strict=True):
        await repo.upsert(MorningSchedule(
            user_id=owner, enabled=enabled, local_time=at, cadence="daily",
            updated_at=now,
        ))
    first_page = await repo.list_due(now, limit=1, offset=0)
    second_page = await repo.list_due(now, limit=1, offset=1)
    assert len(first_page) == len(second_page) == 1
    assert {first_page[0].user_id, second_page[0].user_id} == set(owners[:2])
    assert await repo.list_due(now, limit=1, offset=2) == ()


@pytest.mark.asyncio
async def test_due_list_rejects_unbounded_pages(db_session) -> None:
    repo = SqlAlchemyMorningScheduleRepository(db_session)
    with pytest.raises(ValueError):
        await repo.list_due(datetime(2026, 10, 12, tzinfo=UTC), limit=0, offset=0)
    with pytest.raises(ValueError):
        await repo.list_due(datetime(2026, 10, 12, tzinfo=UTC), limit=101, offset=0)
