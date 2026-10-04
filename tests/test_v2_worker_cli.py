"""The worker CLI must accept the lane names used by Compose and the runbook."""

from contextlib import nullcontext
from uuid import UUID

import pytest

from backend.app.application_engine import browser_worker
from backend.app.application_engine.task_dispatcher import (
    BrowserTask, BrowserTaskKind, BrowserTaskOutcome, BrowserTaskResult,
)
from backend.app.cli.run_worker import _parse_args
from backend.app.core.settings import BrowserSubmissionSettings
from backend.app.domain.identifiers import ApplicationId
from tests.v2_builders import USER


@pytest.mark.parametrize(
    ("lane", "expected"),
    [("general", "GENERAL"), ("browser", "BROWSER")],
)
def test_worker_cli_accepts_documented_lowercase_lanes(
    lane: str, expected: str,
) -> None:
    assert _parse_args(["--lane", lane]).lane == expected


def test_browser_live_switch_is_typed_and_off_by_default():
    assert not BrowserSubmissionSettings.from_env({}).live_enabled
    assert BrowserSubmissionSettings.from_env({
        "JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED": "true"}).live_enabled
    with pytest.raises(ValueError):
        BrowserSubmissionSettings.from_env({
            "JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED": "yes"})


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
