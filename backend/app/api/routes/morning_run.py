"""The authenticated, CH-only V2 morning-run controls and status feed."""

from datetime import datetime, time
from typing import Annotated

from fastapi import APIRouter, Depends, status

from backend.app.api.dependencies import (
    CurrentSession,
    morning_run_service,
    morning_schedule_repository,
    now,
)
from backend.app.api.errors import ApiError
from backend.app.api.schemas import (
    MorningRunBatchResponse,
    MorningRunLatestResponse,
    MorningRunQueuedItemResponse,
    MorningRunStatusResponse,
    MorningScheduleRequest,
    MorningScheduleResponse,
)
from backend.app.domain.morning_schedule import MorningSchedule
from backend.app.repositories.morning_schedule import SqlAlchemyMorningScheduleRepository
from backend.app.services.discovery_source_access import DiscoverySourceAccessDenied
from backend.app.services.morning_run import MorningRunService

router = APIRouter(tags=["v2-morning-run"])


@router.get("/me/morning-schedule", response_model=MorningScheduleResponse)
async def read_schedule(
    current: CurrentSession,
    schedules: Annotated[SqlAlchemyMorningScheduleRepository,
                         Depends(morning_schedule_repository)],
) -> MorningScheduleResponse:
    saved = await schedules.get(current.user.id)
    if saved is None:
        return MorningScheduleResponse(enabled=False, local_time="08:00", cadence="daily")
    return MorningScheduleResponse(
        enabled=saved.enabled, local_time=saved.local_time.strftime("%H:%M"),
        cadence=saved.cadence, updated_at=saved.updated_at,
    )


@router.put("/me/morning-schedule", response_model=MorningScheduleResponse)
async def save_schedule(
    body: MorningScheduleRequest,
    current: CurrentSession,
    schedules: Annotated[SqlAlchemyMorningScheduleRepository,
                         Depends(morning_schedule_repository)],
    instant: Annotated[datetime, Depends(now)],
) -> MorningScheduleResponse:
    saved = await schedules.upsert(MorningSchedule(
        user_id=current.user.id, enabled=body.enabled,
        local_time=time.fromisoformat(body.local_time), cadence=body.cadence,
        updated_at=instant,
    ))
    return MorningScheduleResponse(
        enabled=saved.enabled, local_time=saved.local_time.strftime("%H:%M"),
        cadence=saved.cadence, updated_at=saved.updated_at,
    )


@router.post("/me/morning-runs", response_model=MorningRunBatchResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def trigger_run(
    current: CurrentSession,
    service: Annotated[MorningRunService, Depends(morning_run_service)],
) -> MorningRunBatchResponse:
    try:
        queued = await service.enqueue_active_ch(current.user.id, trigger="MANUAL")
    except DiscoverySourceAccessDenied as exc:
        raise ApiError(status.HTTP_409_CONFLICT, "discovery_source_unapproved", str(exc)) from exc
    return MorningRunBatchResponse(
        runs=tuple(MorningRunQueuedItemResponse.of(item) for item in queued))


@router.get("/me/morning-runs/latest", response_model=MorningRunLatestResponse)
async def latest_runs(
    current: CurrentSession,
    service: Annotated[MorningRunService, Depends(morning_run_service)],
) -> MorningRunLatestResponse:
    runs = await service.latest_for_user(current.user.id)
    return MorningRunLatestResponse(
        runs=tuple(MorningRunStatusResponse.of(item) for item in runs))
