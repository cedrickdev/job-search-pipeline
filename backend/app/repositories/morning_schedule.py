"""Owner-scoped schedule writes and bounded operator reads."""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.domain.identifiers import UserId
from backend.app.domain.morning_schedule import MorningSchedule
from backend.app.infrastructure.database.models import MorningScheduleRow

_ZURICH = ZoneInfo("Europe/Zurich")
_MAX_PAGE = 100


def _to_domain(row: MorningScheduleRow) -> MorningSchedule:
    return MorningSchedule.model_validate({
        "user_id": UserId(row.user_id), "enabled": row.enabled,
        "local_time": row.local_time, "cadence": row.cadence,
        "updated_at": row.updated_at,
    })


class SqlAlchemyMorningScheduleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UserId) -> MorningSchedule | None:
        row = await self._session.get(MorningScheduleRow, user_id)
        return None if row is None else _to_domain(row)

    async def upsert(self, schedule: MorningSchedule) -> MorningSchedule:
        insert_statement = insert(MorningScheduleRow).values(
            user_id=schedule.user_id,
            enabled=schedule.enabled,
            local_time=schedule.local_time,
            cadence=schedule.cadence,
            updated_at=schedule.updated_at,
        )
        upsert_statement = insert_statement.on_conflict_do_update(
            index_elements=[MorningScheduleRow.user_id],
            set_={
                "enabled": insert_statement.excluded.enabled,
                "local_time": insert_statement.excluded.local_time,
                "cadence": insert_statement.excluded.cadence,
                "updated_at": insert_statement.excluded.updated_at,
            },
        ).returning(MorningScheduleRow)
        result = await self._session.execute(upsert_statement)
        return _to_domain(result.scalar_one())

    async def list_due(self, now: datetime, limit: int, offset: int) -> tuple[MorningSchedule, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if not 1 <= limit <= _MAX_PAGE or offset < 0:
            raise ValueError("scheduler page must be bounded")
        local = now.astimezone(_ZURICH)
        statement = select(MorningScheduleRow).where(
            MorningScheduleRow.enabled.is_(True),
            MorningScheduleRow.local_time <= local.time().replace(tzinfo=None),
        )
        if local.weekday() >= 5:
            statement = statement.where(MorningScheduleRow.cadence == "daily")
        statement = statement.order_by(MorningScheduleRow.user_id).limit(limit).offset(offset)
        result = await self._session.execute(statement)
        return tuple(_to_domain(row) for row in result.scalars())
