"""Atomic, committed PostgreSQL reservations for ATS collection attempts."""

from datetime import date

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.infrastructure.database.models import AtsRequestBudgetRow


class SqlAlchemyAtsRequestBudget:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def reserve(self, source_key: str, board_token: str,
                      swiss_date: date, limit: int) -> bool:
        """Charge before HTTP; a denied slot or DB error never authorizes a fetch."""
        if limit <= 0:
            return False
        if source_key not in {"greenhouse", "lever"} or not board_token:
            return False
        statement = insert(AtsRequestBudgetRow).values(
            source_key=source_key, board_token=board_token,
            swiss_date=swiss_date, request_count=1,
        )
        upsert_statement = statement.on_conflict_do_update(
            index_elements=[AtsRequestBudgetRow.source_key,
                            AtsRequestBudgetRow.board_token,
                            AtsRequestBudgetRow.swiss_date],
            set_={"request_count": AtsRequestBudgetRow.request_count + 1,
                  "updated_at": func.now()},
            where=AtsRequestBudgetRow.request_count < limit,
        ).returning(AtsRequestBudgetRow.request_count)
        async with self._factory.begin() as session:
            result = await session.execute(upsert_statement)
            return result.scalar_one_or_none() is not None
