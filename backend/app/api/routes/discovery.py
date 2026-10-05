"""Owner-scoped manual Swiss discovery and its safe status report."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from backend.app.api.dependencies import (
    CurrentSession,
    discovery_catalog_service,
    discovery_enqueue_service,
)
from backend.app.api.errors import ApiError
from backend.app.api.schemas import DiscoveryQueuedResponse, DiscoveryRunResponse
from backend.app.domain.discovery_run import DiscoveryRunStatus
from backend.app.domain.identifiers import DiscoveryRunId, SearchProfileId
from backend.app.domain.task import TaskStatus
from backend.app.services.discovery_catalog import (
    DiscoveryCatalogService,
    DiscoveryEnqueueService,
)

router = APIRouter(tags=["v2-discovery"])


def _not_found() -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, "discovery_not_found",
                    "no discovery run or active Swiss search was found")


@router.post("/me/search-profiles/{search_profile_id}/discovery-runs",
             response_model=DiscoveryQueuedResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def enqueue_discovery(
    search_profile_id: SearchProfileId,
    current: CurrentSession,
    service: Annotated[DiscoveryEnqueueService, Depends(discovery_enqueue_service)],
) -> DiscoveryQueuedResponse:
    try:
        task_id = await service.enqueue_discovery(current.user.id, search_profile_id,
                                                  trigger="MANUAL")
    except LookupError as exc:
        raise _not_found() from exc
    return DiscoveryQueuedResponse(run_id=task_id)


@router.get("/me/discovery-runs/{run_id}", response_model=DiscoveryRunResponse)
async def read_discovery(
    run_id: DiscoveryRunId,
    current: CurrentSession,
    service: Annotated[DiscoveryCatalogService, Depends(discovery_catalog_service)],
) -> DiscoveryRunResponse:
    run = await service.get_run(current.user.id, run_id)
    if run is not None:
        return DiscoveryRunResponse.of(run)
    task = await service.get_queued_task(current.user.id, run_id)
    if task is None:
        raise _not_found()
    profile_id = SearchProfileId(UUID(task.payload["search_profile_id"]))
    if task.status is TaskStatus.DEAD_LETTERED:
        return DiscoveryRunResponse(
            run_id=run_id, search_profile_id=profile_id, country="CH",
            trigger=task.payload["trigger"], status=DiscoveryRunStatus.FAILED,
            started_at=task.started_at, finished_at=task.finished_at,
            seen_count=0, matched_count=0, warning_codes=("RUN_FAILED",),
        )
    return DiscoveryRunResponse(
        run_id=run_id, search_profile_id=profile_id, country="CH",
        trigger=task.payload["trigger"],
        status=("QUEUED" if task.status is TaskStatus.QUEUED
                else DiscoveryRunStatus.RUNNING),
        started_at=task.started_at, seen_count=0, matched_count=0,
    )


@router.get("/me/search-profiles/{search_profile_id}/discovery-runs/latest",
            response_model=DiscoveryRunResponse)
async def read_latest_discovery(
    search_profile_id: SearchProfileId,
    current: CurrentSession,
    service: Annotated[DiscoveryCatalogService, Depends(discovery_catalog_service)],
) -> DiscoveryRunResponse:
    run = await service.get_latest_run(current.user.id, search_profile_id)
    if run is None:
        raise _not_found()
    return DiscoveryRunResponse.of(run)
