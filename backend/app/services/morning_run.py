"""Owner-scoped Swiss V2 discovery batch, reusing the durable task queue."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from backend.app.discovery.requests import countries_in_scope
from backend.app.domain.discovery_run import DiscoveryRun, SourceHealthSummary
from backend.app.domain.identifiers import (
    DiscoveryRunId,
    OpportunityId,
    SearchProfileId,
    TaskRunId,
    UserId,
)
from backend.app.domain.morning_preparation import Preparation, PreparationStage
from backend.app.domain.task import TaskStatus
from backend.app.repositories.contracts import SearchProfileRepository, TaskRunRepository
from backend.app.repositories.discovery_catalog import DiscoveryRunStore
from backend.app.repositories.morning_preparation import MorningPreparationRepository
from backend.app.repositories.morning_run import (
    MorningApplicationProgress,
    MorningRunStatusRepository,
)
from backend.app.services.discovery_catalog import DiscoveryEnqueueService


@dataclass(frozen=True, slots=True)
class MorningRunItem:
    search_profile_id: SearchProfileId
    run_id: TaskRunId
    status: TaskStatus


@dataclass(frozen=True, slots=True)
class MorningRunStatus:
    search_profile_id: SearchProfileId
    run_id: UUID | None
    status: Literal["NOT_STARTED", "QUEUED", "RUNNING", "SUCCEEDED", "PARTIAL", "FAILED"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    seen_count: int = 0
    matched_count: int = 0
    source_health: tuple[SourceHealthSummary, ...] = ()
    warning_codes: tuple[str, ...] = ()
    preparations: tuple[Preparation, ...] = ()
    preparations_truncated: bool = False
    preparation_tasks: dict[OpportunityId, tuple[TaskStatus, str | None]] = field(
        default_factory=dict)
    application_progress: dict[OpportunityId, MorningApplicationProgress] = field(
        default_factory=dict)


class MorningRunService:
    def __init__(self, searches: SearchProfileRepository,
                 enqueue: DiscoveryEnqueueService,
                 tasks: TaskRunRepository,
                 statuses: MorningRunStatusRepository | None = None,
                 runs: DiscoveryRunStore | None = None,
                 preparations: MorningPreparationRepository | None = None,
                 live_submissions_enabled: bool = False) -> None:
        self._searches = searches
        self._enqueue = enqueue
        self._tasks = tasks
        self._statuses = statuses
        self._runs = runs
        self._preparations = preparations
        self._live_submissions_enabled = live_submissions_enabled

    async def enqueue_active_ch(
        self, user_id: UserId, *, trigger: Literal["MANUAL", "MORNING"],
    ) -> tuple[MorningRunItem, ...]:
        items = []
        for profile in await self._searches.list_for_user(
                user_id, active_only=True, limit=100):
            if "CH" not in countries_in_scope(profile):
                continue
            run_id = await self._enqueue.enqueue_discovery(
                user_id, profile.id, trigger=trigger)
            task = await self._tasks.get_by_id(run_id)
            if task is None or task.user_id != user_id:
                raise RuntimeError("queued discovery task not visible to owner")
            items.append(MorningRunItem(search_profile_id=profile.id,
                                        run_id=run_id, status=task.status))
        return tuple(items)

    async def latest_for_user(self, user_id: UserId) -> tuple[MorningRunStatus, ...]:
        if self._statuses is None or self._runs is None:
            raise RuntimeError("morning-run status stores are not configured")
        items = []
        for profile in await self._searches.list_for_user(
                user_id, active_only=True, limit=100):
            if "CH" not in countries_in_scope(profile):
                continue
            task = await self._statuses.latest_task_for_search(user_id, profile.id)
            report = (await self._runs.get_run(user_id, DiscoveryRunId(task.id))
                      if task is not None else
                      await self._runs.get_latest_run(user_id, profile.id))
            if report is not None:
                status = self._from_report(report)
                if self._preparations is not None:
                    page = await self._preparations.list_for_run(
                        user_id, DiscoveryRunId(report.id), limit=501)
                    keys_by_opportunity = {
                        preparation.opportunity_id: _preparation_task_key(
                            user_id, report.id, preparation)
                        for preparation in page[:500]
                        if preparation.stage in {PreparationStage.DISCOVERED,
                                                 PreparationStage.MATCHED,
                                                 PreparationStage.CV_RENDERED,
                                                 PreparationStage.DOCUMENTS_RENDERED}
                    }
                    task_rows = await self._statuses.preparation_tasks(
                        user_id, tuple(keys_by_opportunity.values()))
                    task_states = {
                        opportunity_id: (
                            task_rows[key].status, task_rows[key].failure_reason)
                        for opportunity_id, key in keys_by_opportunity.items()
                        if key in task_rows
                    }
                    application_progress = await self._statuses.application_progress(
                        user_id,
                        tuple(preparation.opportunity_id for preparation in page[:500]),
                        live_submissions_enabled=self._live_submissions_enabled,
                    )
                    status = MorningRunStatus(
                        search_profile_id=status.search_profile_id, run_id=status.run_id,
                        status=status.status, started_at=status.started_at,
                        finished_at=status.finished_at, seen_count=status.seen_count,
                        matched_count=status.matched_count,
                        source_health=status.source_health,
                        warning_codes=status.warning_codes,
                        preparations=page[:500], preparations_truncated=len(page) > 500,
                        preparation_tasks=task_states,
                        application_progress=application_progress,
                    )
                items.append(status)
            elif task is None:
                items.append(MorningRunStatus(profile.id, None, "NOT_STARTED"))
            else:
                state: Literal["QUEUED", "RUNNING", "FAILED"] = (
                    "FAILED" if task.status is TaskStatus.DEAD_LETTERED
                    else "RUNNING" if task.status is TaskStatus.RUNNING
                    else "QUEUED")
                items.append(MorningRunStatus(
                    profile.id, task.id, state,
                    started_at=task.started_at, finished_at=task.finished_at,
                    warning_codes=("RUN_FAILED",) if state == "FAILED" else (),
                ))
        return tuple(items)

    @staticmethod
    def _from_report(report: DiscoveryRun) -> MorningRunStatus:
        return MorningRunStatus(
            search_profile_id=report.search_profile_id, run_id=report.id,
            status=report.status.value,
            started_at=report.started_at, finished_at=report.finished_at,
            seen_count=report.seen_count, matched_count=report.matched_count,
            source_health=report.source_health, warning_codes=report.warning_codes,
        )


def _preparation_task_key(user_id: UserId, run_id: UUID,
                          preparation: Preparation) -> str:
    prefix = ("morning-match" if preparation.stage is PreparationStage.DISCOVERED
              else "morning-application" if preparation.stage is PreparationStage.DOCUMENTS_RENDERED
              else "morning-docs")
    return f"{prefix}:{user_id}:{run_id}:{preparation.opportunity_id}"
