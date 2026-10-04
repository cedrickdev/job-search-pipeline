"""The one driving core: a task plus a live page becomes a typed result (§4, §8).

Shared by the production worker (real Chromium, real PostgreSQL) and any in-process driver — the
logic is identical, only the page and the repositories differ. It nominates a driver from the
target URL (§4), rebuilds the read-only context from the database (§6-7), and hands off to the
phase orchestrator. An unnominated target is an `UNSUPPORTED_CHANNEL` human hand-off, never a blind
attempt (§2, §29); an application that cannot be loaded is a safe pre-navigation failure.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from backend.app.application_engine.browser.context_loader import ApplicationContextLoader
from backend.app.application_engine.browser.execution import run_prepare, run_submit
from backend.app.application_engine.browser.registry import BrowserDriverRegistry
from backend.app.application_engine.task_dispatcher import (
    BrowserTask,
    BrowserTaskKind,
    BrowserTaskOutcome,
    BrowserTaskResult,
)
from backend.app.domain.application_channel import HumanRequiredReason

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import Page


async def execute_on_page(
        page: Page, task: BrowserTask, *,
        registry: BrowserDriverRegistry,
        loader: ApplicationContextLoader) -> BrowserTaskResult:
    """Run one browser task on a live page and return its typed result (§4, §6-7)."""
    driver = registry.detect(task.target_url)
    if driver is None:
        # No driver claims this target: hand off to a human, never drive it blind (§2, §29).
        return BrowserTaskResult(
            outcome=BrowserTaskOutcome.REQUIRES_HUMAN,
            human_required_reason=HumanRequiredReason.UNSUPPORTED_CHANNEL,
            detail="no supported ATS driver matches this posting; a human must apply")

    loaded = await loader.load(
        task.user_id, task.application_id,
        target_url=task.target_url, correlation_id=task.correlation_id)
    if loaded is None:
        # The application or its profile could not be read: nothing was navigated or sent, so this
        # is a safe pre-click failure the service can retry, never an ambiguous submission (§23).
        return BrowserTaskResult(
            outcome=BrowserTaskOutcome.FAILED,
            detail="the application context could not be loaded; nothing was attempted")

    if task.kind is BrowserTaskKind.PREPARE:
        return await run_prepare(page, driver, loaded.context, loaded.profile)
    return await run_submit(page, driver, loaded.context)
