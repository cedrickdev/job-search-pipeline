"""Commit the Morning Run application before inspecting its browser form."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.domain.application import Application, ApplicationState
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.documents import CandidateDocumentType
from backend.app.domain.identifiers import (
    DiscoveryRunId,
    DocumentVersionId,
    OpportunityId,
    UserId,
    task_run_id,
)
from backend.app.domain.morning_preparation import Preparation, PreparationStage
from backend.app.domain.task import TaskKind, TaskSpec
from backend.app.infrastructure.database.engine import session_scope
from backend.app.infrastructure.database.mappers import application_to_domain
from backend.app.infrastructure.database.models import (
    ApplicationRow,
    DiscoveryRunRow,
    MorningPreparationRow,
    TaskRunRow,
)
from backend.app.repositories.morning_preparation import MorningPreparationRepository
from backend.app.repositories.sqlalchemy_repositories import SqlAlchemyApplicationRepository
from backend.app.services.application_queue import ApplicationQueueService
from backend.app.services.applications import ApplicationService
from backend.app.tasks.dispatcher import TaskDispatcher


class MorningApplicationService:
    """Idempotent bridge; two transactions make the created row visible to Chromium."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *,
        application_builder: Callable[[AsyncSession], ApplicationService] | None = None,
        queue_builder: Callable[[AsyncSession], ApplicationQueueService] | None = None,
        task_dispatcher_builder: Callable[[AsyncSession], TaskDispatcher] | None = None,
        live_enabled: bool = False,
    ) -> None:
        if live_enabled and queue_builder is None:
            raise ValueError("live browser routing requires a checked queue service")
        self._session_factory = session_factory
        self._application_builder = application_builder
        self._queue_builder = queue_builder
        self._task_dispatcher_builder = task_dispatcher_builder
        self._live_enabled = live_enabled

    async def reconcile_ready(self, *, batch_size: int = 100) -> int:
        """Enqueue old rendered checkpoints with bounded, restartable keyset scans."""
        if self._task_dispatcher_builder is None:
            raise ValueError("reconciliation requires a durable dispatcher")
        if not 1 <= batch_size <= 500:
            raise ValueError("batch_size must be between 1 and 500")
        cursor = None
        created = 0
        while True:
            async with session_scope(self._session_factory) as session:
                key = tuple_(MorningPreparationRow.user_id,
                             MorningPreparationRow.discovery_run_id,
                             MorningPreparationRow.opportunity_id)
                statement = (select(
                    MorningPreparationRow.user_id,
                    MorningPreparationRow.discovery_run_id,
                    MorningPreparationRow.opportunity_id,
                ).join(DiscoveryRunRow, (
                    DiscoveryRunRow.id == MorningPreparationRow.discovery_run_id)
                    & (DiscoveryRunRow.user_id == MorningPreparationRow.user_id)
                ).where(
                    DiscoveryRunRow.country == "CH",
                    MorningPreparationRow.stage == PreparationStage.DOCUMENTS_RENDERED,
                ).order_by(*key.clauses).limit(batch_size))
                if cursor is not None:
                    statement = statement.where(key > cursor)
                rows = (await session.execute(statement)).all()
                if not rows:
                    break
                specs = [TaskSpec(
                    kind=TaskKind.APPLICATION_PREPARATION,
                    idempotency_key=(f"morning-application:{owner}:"
                                     f"{run_id}:{opportunity_id}"),
                    user_id=UserId(owner),
                    payload={"discovery_run_id": str(run_id),
                             "opportunity_id": str(opportunity_id)},
                    max_attempts=2,
                ) for owner, run_id, opportunity_id in rows]
                ids = tuple(task_run_id(spec.idempotency_key) for spec in specs)
                existing = set((await session.execute(select(TaskRunRow.id).where(
                    TaskRunRow.id.in_(ids)))).scalars())
                dispatcher = self._task_dispatcher_builder(session)
                for spec, identifier in zip(specs, ids, strict=True):
                    if identifier not in existing:
                        await dispatcher.enqueue(spec)
                        created += 1
                cursor = tuple(rows[-1])
                if len(rows) < batch_size:
                    break
        return created

    async def _checkpoint(
        self, session: AsyncSession, user_id: UserId, run_id: DiscoveryRunId,
        opportunity_id: OpportunityId,
    ) -> Preparation:
        country = (await session.execute(select(DiscoveryRunRow.country).where(
            DiscoveryRunRow.id == run_id, DiscoveryRunRow.user_id == user_id,
        ))).scalar_one_or_none()
        checkpoint = await MorningPreparationRepository(session).get(
            user_id, run_id, opportunity_id)
        if (country != "CH" or checkpoint is None
                or checkpoint.stage is not PreparationStage.DOCUMENTS_RENDERED
                or checkpoint.resume_version_id is None
                or checkpoint.cover_letter_version_id is None):
            raise LookupError("owner's rendered Swiss run member is unavailable")
        return checkpoint

    async def prepare(
        self, user_id: UserId, run_id: DiscoveryRunId,
        opportunity_id: OpportunityId, *, now: datetime,
    ) -> Application:
        if self._application_builder is None:
            raise RuntimeError("application builder is required for preparation")
        # The per-owner advisory lock makes the read/create idempotency check serial
        # across distinct searches and workers that found the same posting.
        async with session_scope(self._session_factory) as create_session:
            await self._checkpoint(create_session, user_id, run_id, opportunity_id)
            await SqlAlchemyApplicationRepository(
                create_session).lock_submission_budget(user_id)
            existing = (await create_session.execute(select(ApplicationRow).where(
                ApplicationRow.user_id == user_id,
                ApplicationRow.opportunity_id == opportunity_id,
                ApplicationRow.channel == ApplicationChannel.BROWSER,
            ).with_for_update())).scalar_one_or_none()
            created = (application_to_domain(existing) if existing is not None else
                       await self._application_builder(create_session).create(
                           user_id, opportunity_id, now=now))

        # The subprocess reads PostgreSQL in another connection. Never dispatch it
        # while the application is still uncommitted in the creation transaction.
        async with session_scope(self._session_factory) as prepare_session:
            checkpoint = await self._checkpoint(
                prepare_session, user_id, run_id, opportunity_id)
            await prepare_session.execute(select(ApplicationRow.id).where(
                ApplicationRow.id == created.id,
                ApplicationRow.user_id == user_id,
            ).with_for_update())
            service = self._application_builder(prepare_session)
            current = await service.get(user_id, created.id)
            if current.state is not ApplicationState.PLANNED:
                return current
            if (checkpoint.resume_version_id is None
                    or checkpoint.cover_letter_version_id is None):
                raise LookupError("owner's document versions are unavailable")
            prepared = await service.prepare(
                user_id, created.id, now=now,
                required_versions={
                    CandidateDocumentType.RESUME: DocumentVersionId(
                        checkpoint.resume_version_id),
                    CandidateDocumentType.COVER_LETTER: DocumentVersionId(
                        checkpoint.cover_letter_version_id),
                },
            )
            if (prepared.state is ApplicationState.APPROVED
                    and self._live_enabled and self._queue_builder is not None):
                await self._queue_builder(prepare_session).enqueue_approved(
                    user_id, prepared.id)
            return prepared
