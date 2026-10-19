"""The morning-run HTTP surface takes its owner only from the session."""

from uuid import UUID
from datetime import UTC, datetime

import pytest

from backend.app.api.dependencies import morning_run_service, morning_schedule_repository
from backend.app.domain.discovery_run import SourceHealthSummary
from backend.app.domain.identifiers import SearchProfileId, TaskRunId
from backend.app.domain.task import TaskStatus
from backend.app.domain.morning_preparation import Preparation, PreparationStage
from backend.app.domain.identifiers import DiscoveryRunId, OpportunityId
from backend.app.domain.application import ApplicationState
from backend.app.services.morning_run import (
    MorningApplicationProgress,
    MorningRunItem,
    MorningRunStatus,
)
from backend.app.services.discovery_source_access import DiscoverySourceAccessDenied
from tests.v2_api import api_harness

RUN_ID = TaskRunId(UUID("00000000-0000-4000-8000-000000000e31"))
SEARCH_ID = SearchProfileId(UUID("00000000-0000-4000-8000-000000000e32"))
OPPORTUNITY_ID = OpportunityId(UUID("00000000-0000-4000-8000-000000000e33"))


class Schedules:
    def __init__(self) -> None:
        self.rows = {}

    async def get(self, user_id):
        return self.rows.get(user_id)

    async def upsert(self, schedule):
        self.rows[schedule.user_id] = schedule
        return schedule


class Runs:
    def __init__(self, owner) -> None:
        self.owner = owner

    async def enqueue_active_ch(self, user_id, *, trigger):
        if user_id != self.owner:
            return ()
        assert trigger == "MANUAL"
        return (MorningRunItem(SEARCH_ID, RUN_ID, TaskStatus.QUEUED),)

    async def latest_for_user(self, user_id):
        if user_id != self.owner:
            return ()
        return (MorningRunStatus(
            SEARCH_ID, RUN_ID, "PARTIAL", seen_count=3, matched_count=2,
            source_health=(SourceHealthSummary(
                source_key="fixture_board", status="UNAVAILABLE",
                reason="SOURCE_TIMEOUT"),),
            warning_codes=("SOURCE_TIMEOUT",),
            preparations=(Preparation(
                user_id=user_id, discovery_run_id=DiscoveryRunId(RUN_ID),
                opportunity_id=OPPORTUNITY_ID, stage=PreparationStage.CV_RENDERED,
                resume_version_id=UUID("00000000-0000-4000-8000-000000000e34"),
                updated_at=datetime(2026, 10, 13, tzinfo=UTC)),),
            preparation_tasks={OPPORTUNITY_ID: (
                TaskStatus.DEAD_LETTERED, "PROVIDER_UNAVAILABLE")},
            application_progress={OPPORTUNITY_ID: MorningApplicationProgress(
                application_id=UUID("00000000-0000-4000-8000-000000000e35"),
                state=ApplicationState.READY_FOR_REVIEW,
                capability_id=None, submission_task_status=None,
                blocker_code="FORM_REVIEW_REQUIRED",
            )},
        ),)


@pytest.mark.asyncio
async def test_schedule_defaults_disabled_and_rejects_bad_values_and_csrf(tmp_path) -> None:
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        owner = next(iter(api.users.users))
        schedules = Schedules()
        api.app.dependency_overrides[morning_schedule_repository] = lambda: schedules

        initial = await api.read("/me/morning-schedule")
        assert initial.status_code == 200
        assert initial.json()["enabled"] is False
        assert initial.json()["local_time"] == "08:00"

        bad_time = await api.write("PUT", "/me/morning-schedule", json={
            "enabled": True, "local_time": "25:00", "cadence": "daily"})
        bad_cadence = await api.write("PUT", "/me/morning-schedule", json={
            "enabled": True, "local_time": "08:00", "cadence": "hourly"})
        missing_csrf = await api.client.put(api.url("/me/morning-schedule"), json={
            "enabled": True, "local_time": "08:00", "cadence": "daily"})
        assert bad_time.status_code == bad_cadence.status_code == 422
        assert missing_csrf.status_code == 403
        assert schedules.rows == {}

        saved = await api.write("PUT", "/me/morning-schedule", json={
            "enabled": True, "local_time": "08:00", "cadence": "weekdays"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["cadence"] == "weekdays"
        assert owner in schedules.rows


@pytest.mark.asyncio
async def test_trigger_is_202_queued_and_latest_exposes_partial_coverage(tmp_path) -> None:
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        owner = next(iter(api.users.users))
        api.app.dependency_overrides[morning_run_service] = lambda: Runs(owner)

        queued = await api.write("POST", "/me/morning-runs")
        latest = await api.read("/me/morning-runs/latest")
        assert queued.status_code == 202, queued.text
        assert queued.json()["runs"][0]["status"] == "QUEUED"
        assert latest.json()["runs"][0]["status"] == "PARTIAL"
        assert latest.json()["runs"][0]["matched_count"] == 2
        assert latest.json()["runs"][0]["source_health"][0]["source_key"] == "fixture_board"
        assert latest.json()["runs"][0]["warning_codes"] == ["SOURCE_TIMEOUT"]
        assert latest.json()["runs"][0]["preparations"][0]["stage"] == "CV_RENDERED"
        assert latest.json()["runs"][0]["preparations"][0]["resume_version_id"] == "00000000-0000-4000-8000-000000000e34"
        assert latest.json()["runs"][0]["preparations"][0]["task_status"] == "DEAD_LETTERED"
        assert latest.json()["runs"][0]["preparations"][0]["task_failure_code"] == "PROVIDER_UNAVAILABLE"
        assert latest.json()["runs"][0]["preparations"][0]["application_id"] == "00000000-0000-4000-8000-000000000e35"
        assert latest.json()["runs"][0]["preparations"][0]["application_state"] == "READY_FOR_REVIEW"
        assert latest.json()["runs"][0]["preparations"][0]["application_blocker_code"] == "FORM_REVIEW_REQUIRED"

        await api.sign_in(email="other@example.com")
        foreign = await api.read("/me/morning-runs/latest")
        assert foreign.json()["runs"] == []


@pytest.mark.asyncio
async def test_morning_manual_trigger_explains_unapproved_source(tmp_path) -> None:
    class DeniedRuns:
        async def enqueue_active_ch(self, user_id, *, trigger):
            raise DiscoverySourceAccessDenied("Swiss source access is not approved")

    async with api_harness(tmp_path) as api:
        await api.sign_in()
        api.app.dependency_overrides[morning_run_service] = lambda: DeniedRuns()
        response = await api.write("POST", "/me/morning-runs")
        assert response.status_code == 409
        assert response.json()["error"] == "discovery_source_unapproved"
