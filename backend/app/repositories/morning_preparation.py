"""Atomic, owner-filtered checkpoints for one Swiss Morning Run member."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from backend.app.domain.identifiers import DiscoveryRunId, OpportunityId, UserId
from backend.app.domain.morning_preparation import Preparation, PreparationStage
from backend.app.infrastructure.database.models import DiscoveryRunRow, MorningPreparationRow


def _set_checkpoint_time(row: MorningPreparationRow, at: datetime) -> None:
    # Equal timestamps are valid for adjacent stages. Force the domain clock into
    # the UPDATE instead of letting TimestampedMixin.onupdate use the DB clock.
    row.updated_at = at
    flag_modified(row, "updated_at")


def _to_domain(row: MorningPreparationRow) -> Preparation:
    return Preparation(
        user_id=UserId(row.user_id), discovery_run_id=DiscoveryRunId(row.discovery_run_id),
        opportunity_id=OpportunityId(row.opportunity_id), stage=row.stage,
        decision_id=row.decision_id,
        resume_version_id=row.resume_version_id,
        cover_letter_version_id=row.cover_letter_version_id,
        reason_code=row.reason_code, updated_at=row.updated_at)


class MorningPreparationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UserId, run_id: DiscoveryRunId,
                  opportunity_id: OpportunityId) -> Preparation | None:
        row = (await self._session.execute(select(MorningPreparationRow).where(
            MorningPreparationRow.user_id == user_id,
            MorningPreparationRow.discovery_run_id == run_id,
            MorningPreparationRow.opportunity_id == opportunity_id))).scalar_one_or_none()
        return None if row is None else _to_domain(row)

    async def list_for_run(self, user_id: UserId, run_id: DiscoveryRunId, *,
                           limit: int = 500) -> tuple[Preparation, ...]:
        if not 1 <= limit <= 501:
            raise ValueError("preparation page limit must be between 1 and 501")
        rows = (await self._session.execute(select(MorningPreparationRow).where(
            MorningPreparationRow.user_id == user_id,
            MorningPreparationRow.discovery_run_id == run_id,
        ).order_by(MorningPreparationRow.created_at, MorningPreparationRow.opportunity_id)
          .limit(limit))).scalars().all()
        return tuple(_to_domain(row) for row in rows)

    async def start_or_get(self, user_id: UserId, run_id: DiscoveryRunId,
                           opportunity_id: OpportunityId, *, now: datetime) -> Preparation:
        owner_run = (await self._session.execute(select(DiscoveryRunRow.id).where(
            DiscoveryRunRow.id == run_id, DiscoveryRunRow.user_id == user_id,
            DiscoveryRunRow.country == "CH"))).scalar_one_or_none()
        if owner_run is None:
            raise LookupError("Swiss discovery run not found for owner")
        await self._session.execute(pg_insert(MorningPreparationRow).values(
            user_id=user_id, discovery_run_id=run_id, opportunity_id=opportunity_id,
            stage=PreparationStage.DISCOVERED, created_at=now,
            updated_at=now).on_conflict_do_nothing(index_elements=[
                "user_id", "discovery_run_id", "opportunity_id"]))
        preparation = await self.get(user_id, run_id, opportunity_id)
        if preparation is None:
            raise RuntimeError("inserted preparation not visible to owner")
        return preparation

    async def _locked(self, user_id: UserId, run_id: DiscoveryRunId,
                      opportunity_id: OpportunityId) -> MorningPreparationRow:
        statement = select(MorningPreparationRow).where(
            MorningPreparationRow.user_id == user_id,
            MorningPreparationRow.discovery_run_id == run_id,
            MorningPreparationRow.opportunity_id == opportunity_id).with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise LookupError("preparation not found for owner")
        return row

    async def lock(self, user_id: UserId, run_id: DiscoveryRunId,
                   opportunity_id: OpportunityId) -> Preparation:
        """Hold a row lock until the caller commits its generation/checkpoint unit."""
        return _to_domain(await self._locked(user_id, run_id, opportunity_id))

    async def advance(self, user_id: UserId, run_id: DiscoveryRunId,
                      opportunity_id: OpportunityId, stage: PreparationStage, *,
                      now: datetime, reason_code: str | None = None) -> Preparation:
        row = await self._locked(user_id, run_id, opportunity_id)
        next_state = _to_domain(row).advance(stage, at=now, reason_code=reason_code)
        row.stage = next_state.stage
        row.reason_code = next_state.reason_code
        _set_checkpoint_time(row, next_state.updated_at)
        await self._session.flush()
        return next_state

    async def record_assessment(self, user_id: UserId, run_id: DiscoveryRunId,
                                opportunity_id: OpportunityId, *, decision_id: UUID,
                                now: datetime) -> Preparation:
        row = await self._locked(user_id, run_id, opportunity_id)
        current = _to_domain(row)
        if current.stage is PreparationStage.MATCHED and current.decision_id == decision_id:
            return current
        next_state = current.advance(PreparationStage.MATCHED, at=now)
        row.stage = next_state.stage
        row.decision_id = decision_id
        _set_checkpoint_time(row, next_state.updated_at)
        await self._session.flush()
        return next_state.model_copy(update={"decision_id": decision_id})

    async def record_document_version(self, user_id: UserId, run_id: DiscoveryRunId,
                                      opportunity_id: OpportunityId,
                                      document_type: str, version_id: UUID, *,
                                      now: datetime) -> Preparation:
        row = await self._locked(user_id, run_id, opportunity_id)
        next_state = _to_domain(row).record_document(document_type, version_id, at=now)
        row.stage = next_state.stage
        row.resume_version_id = next_state.resume_version_id
        row.cover_letter_version_id = next_state.cover_letter_version_id
        _set_checkpoint_time(row, next_state.updated_at)
        await self._session.flush()
        return next_state
