"""The worker CLI must accept the lane names used by Compose and the runbook."""

from contextlib import nullcontext
from uuid import UUID

import pytest

from backend.app.application_engine import browser_worker
from backend.app.api.dependencies import application_registry
from backend.app.application_engine.task_dispatcher import (
    BrowserTask, BrowserTaskKind, BrowserTaskOutcome, BrowserTaskResult,
)
from backend.app.cli import run_worker
from backend.app.cli.run_worker import _parse_args
from backend.app.core.settings import BrowserSubmissionSettings, ExportSettings, RetentionSettings
from backend.app.domain.task import TaskKind
from backend.app.domain.identifiers import ApplicationId
from backend.app.domain.application_channel import ApplicationChannel
from tests.v2_builders import USER


@pytest.mark.parametrize(
    ("lane", "expected"),
    [("general", "GENERAL"), ("browser", "BROWSER")],
)
def test_worker_cli_accepts_documented_lowercase_lanes(
    lane: str, expected: str,
) -> None:
    assert _parse_args(["--lane", lane]).lane == expected


def test_one_shot_worker_exits_successfully_after_draining_tasks(monkeypatch):
    async def drained_one_task(_lane, *, once):
        assert once is True
        return 1

    monkeypatch.setattr(run_worker, "_run", drained_one_task)
    monkeypatch.setattr(run_worker, "validate_production_readiness", lambda: None)

    assert run_worker.main(["--lane", "general", "--once"]) == 0


def test_general_worker_registers_morning_matching_without_browser_submission(monkeypatch):
    monkeypatch.setenv("JOBSEARCH_TERMINAL_ENABLED", "false")
    handlers = run_worker._general_registry(
        ExportSettings.from_env({}), RetentionSettings.from_env({}),
        session_factory=object())
    assert TaskKind.MATCHING in handlers
    assert TaskKind.DOCUMENT_GENERATION in handlers
    assert TaskKind.APPLICATION_SUBMISSION not in handlers


def test_browser_live_switch_is_typed_and_off_by_default():
    assert not BrowserSubmissionSettings.from_env({}).live_enabled
    assert not BrowserSubmissionSettings.from_env({}).local_preparation_enabled
    assert BrowserSubmissionSettings.from_env({
        "JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED": "true"}).live_enabled
    assert BrowserSubmissionSettings.from_env({
        "JOBSEARCH_BROWSER_LOCAL_PREPARATION_ENABLED": "true"}).local_preparation_enabled
    with pytest.raises(ValueError):
        BrowserSubmissionSettings.from_env({
            "JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED": "yes"})
    with pytest.raises(ValueError):
        BrowserSubmissionSettings.from_env({
            "JOBSEARCH_BROWSER_LOCAL_PREPARATION_ENABLED": "yes"})


def test_api_registry_wires_browser_preparation_only_when_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("JOBSEARCH_BROWSER_LOCAL_PREPARATION_ENABLED", raising=False)
    disabled = application_registry()
    assert disabled.resolve(ApplicationChannel.BROWSER).capabilities.key == "generic-manual/1"

    monkeypatch.setenv("JOBSEARCH_BROWSER_LOCAL_PREPARATION_ENABLED", "true")
    enabled = application_registry()
    assert enabled.resolve(ApplicationChannel.BROWSER).capabilities.key == "browser/1"


def test_browser_subprocess_refuses_submit_when_live_switch_is_off(
        tmp_path, monkeypatch):
    monkeypatch.delenv("JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED", raising=False)
    monkeypatch.setattr(browser_worker, "browser_lock", lambda: nullcontext())

    async def must_not_drive(_task):
        raise AssertionError("browser started while live submissions disabled")

    monkeypatch.setattr(browser_worker, "_drive", must_not_drive)
    task = BrowserTask(user_id=USER,
                       application_id=ApplicationId(
                           UUID("22222222-2222-4222-8222-222222222222")),
                       kind=BrowserTaskKind.SUBMIT)
    request = tmp_path / "request.json"
    result = tmp_path / "result.json"
    request.write_text(task.model_dump_json())

    browser_worker.run(request, result)

    parsed = BrowserTaskResult.model_validate_json(result.read_text())
    assert parsed.outcome is BrowserTaskOutcome.REQUIRES_HUMAN
