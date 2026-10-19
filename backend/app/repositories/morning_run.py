"""Bounded, owner-filtered task lookup for the Swiss morning-run status feed."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.domain.application import ApplicationState
from backend.app.domain.identifiers import ApplicationId, OpportunityId, SearchProfileId, UserId
from backend.app.domain.task import TaskKind, TaskRun, TaskStatus
from backend.app.infrastructure.database.mappers import task_run_to_domain
from backend.app.infrastructure.database.models import ApplicationRow, TaskRunRow


@dataclass(frozen=True, slots=True)
class MorningApplicationProgress:
    application_id: ApplicationId
    state: ApplicationState
    capability_id: str | None
    submission_task_status: TaskStatus | None
    blocker_code: str | None


class MorningRunStatusRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_task_for_search(
        self, user_id: UserId, search_profile_id: SearchProfileId,
    ) -> TaskRun | None:
        result = await self._session.execute(
            select(TaskRunRow)
            .where(TaskRunRow.user_id == user_id,
                   TaskRunRow.kind == TaskKind.OPPORTUNITY_DISCOVERY,
                   TaskRunRow.payload["search_profile_id"].astext == str(search_profile_id))
            .order_by(TaskRunRow.enqueued_at.desc(), TaskRunRow.id.desc())
            .limit(1))
        row = result.scalar_one_or_none()
        return None if row is None else task_run_to_domain(row)

    async def preparation_tasks(self, user_id: UserId,
                                keys: tuple[str, ...]) -> dict[str, TaskRun]:
        """Batch-read only this owner's matching/document tasks for one status page."""
        if not keys:
            return {}
        if len(keys) > 500:
            raise ValueError("preparation status page exceeds 500 tasks")
        rows = (await self._session.execute(select(TaskRunRow).where(
            TaskRunRow.user_id == user_id,
            TaskRunRow.kind.in_((TaskKind.MATCHING, TaskKind.DOCUMENT_GENERATION,
                                 TaskKind.APPLICATION_PREPARATION)),
            TaskRunRow.idempotency_key.in_(keys),
        ))).scalars().all()
        return {row.idempotency_key: task_run_to_domain(row) for row in rows}

    async def application_progress(
        self, user_id: UserId, opportunity_ids: tuple[OpportunityId, ...], *,
        live_submissions_enabled: bool,
    ) -> dict[OpportunityId, MorningApplicationProgress]:
        """Two bounded queries, always owner-filtered; queueing is never 'submitted'."""
        if not opportunity_ids:
            return {}
        if len(opportunity_ids) > 500:
            raise ValueError("application status page exceeds 500 opportunities")
        rows = (await self._session.execute(select(ApplicationRow).where(
            ApplicationRow.user_id == user_id,
            ApplicationRow.opportunity_id.in_(opportunity_ids),
        ).order_by(ApplicationRow.updated_at.desc(), ApplicationRow.id))).scalars().all()
        apps = {OpportunityId(row.opportunity_id): row for row in reversed(rows)
                if row.opportunity_id is not None}
        keys = tuple(f"application-submission:{row.id}" for row in apps.values())
        tasks = (await self._session.execute(select(TaskRunRow).where(
            TaskRunRow.user_id == user_id,
            TaskRunRow.kind == TaskKind.APPLICATION_SUBMISSION,
            TaskRunRow.idempotency_key.in_(keys),
        ))).scalars().all() if keys else ()
        by_key = {task.idempotency_key: task for task in tasks}
        result: dict[OpportunityId, MorningApplicationProgress] = {}
        for opportunity_id, row in apps.items():
            queued = by_key.get(f"application-submission:{row.id}")
            state = row.state
            blocker = (
                "SUBMISSION_STATE_UNKNOWN" if state is ApplicationState.SUBMISSION_STATE_UNKNOWN
                else "FORM_REVIEW_REQUIRED" if state is ApplicationState.READY_FOR_REVIEW
                else "REQUIRES_HUMAN" if state is ApplicationState.REQUIRES_HUMAN
                else "LIVE_SUBMISSIONS_DISABLED" if (state is ApplicationState.APPROVED
                                                       and not live_submissions_enabled)
                else queued.failure_reason if queued is not None
                     and queued.status is TaskStatus.DEAD_LETTERED
                else None
            )
            result[opportunity_id] = MorningApplicationProgress(
                application_id=ApplicationId(row.id), state=state,
                capability_id=row.form_capability_id,
                submission_task_status=queued.status if queued else None,
                blocker_code=blocker,
            )
        return result
