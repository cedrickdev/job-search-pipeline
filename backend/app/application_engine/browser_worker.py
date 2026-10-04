"""The isolated browser-worker entrypoint the subprocess dispatcher spawns (§6-7, §30, §44-48).

`python -m backend.app.application_engine.browser_worker --request R --result W` is what
`SubprocessTaskDispatcher` runs: it reads a typed `BrowserTask` from `R`, does the browser work
under the shared profile lock, and writes a typed `BrowserTaskResult` to `W`. Running here rather
than in the API process is the whole point (§30) — a crashing Chromium takes down a throwaway
worker, not the server, and the §88 crash rule then resolves a lost SUBMIT to STATE_UNKNOWN because
no result file was written.

What it now does, in place of the old placeholder: it rebuilds the submission's authoritative
context from PostgreSQL read-only (§6-7), scoped to the task's account; nominates the matching ATS
driver from the target URL (§4); launches a real, standard-Playwright Chromium on the persistent
profile (§17, §31); and runs the driver-agnostic `prepare`/`submit` orchestration, which enforces
the fingerprint re-check (§14), the unknown-required and sensitive-field stops (§10-11), the pinned
uploads (§13), the one irreversible click (§20) and the confirm-or-STATE_UNKNOWN rule (§21-22). It
adds no authority: by the time a SUBMIT task is dispatched, `ApplicationService.submit` has already
re-checked policy, eligibility, approval and the commercial quota (§1, §45-46).

Logging is structured and secret-safe (§33): correlation id, application id, driver key, phase and
typed outcome — never a field value, a page's text, a cookie or a credential.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import tempfile
from pathlib import Path

from backend.app.application_engine.browser.context_loader import ApplicationContextLoader
from backend.app.application_engine.browser.execute import execute_on_page
from backend.app.application_engine.browser.registry import BrowserDriverRegistry
from backend.app.application_engine.browser.runtime import browser_page, profile_dir_for
from backend.app.application_engine.task_dispatcher import (
    BrowserBusy,
    BrowserTask,
    BrowserTaskKind,
    BrowserTaskOutcome,
    BrowserTaskResult,
    browser_lock,
    load_browser_task,
    write_browser_result,
)
from backend.app.core.settings import (
    BrowserSubmissionSettings,
    DatabaseSettings,
    DocumentSettings,
)
from backend.app.documents.artifacts import LocalDocumentArtifactStore
from backend.app.domain.application_channel import HumanRequiredReason
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
    session_scope,
)
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyApplicationRepository,
    SqlAlchemyCandidateDocumentRepository,
    SqlAlchemyCandidateProfileRepository,
    SqlAlchemyOpportunityRepository,
)

logger = logging.getLogger("jobsearch.application_engine.browser_worker")


def run(request_path: Path, result_path: Path) -> None:
    """Read the task, do the work under the browser lock, write the typed result.

    A `BrowserBusy` is written as a `REQUIRES_HUMAN` "browser in use" result rather than raised: the
    worker's contract is to report an outcome, never to exit on a scheduling fact. Any other
    exception is deliberately *not* caught — it exits non-zero with no result file, which the
    dispatcher reads as the crash it is, resolving a lost SUBMIT to STATE_UNKNOWN (§88).
    """
    task = load_browser_task(request_path)
    logger.info(
        "browser task start phase=%s application=%s correlation=%s",
        task.kind.value, task.application_id, task.correlation_id)
    try:
        with browser_lock():
            if (task.kind is BrowserTaskKind.SUBMIT
                    and not BrowserSubmissionSettings.from_env().live_enabled):
                result = BrowserTaskResult(
                    outcome=BrowserTaskOutcome.REQUIRES_HUMAN,
                    human_required_reason=HumanRequiredReason.AUTOMATION_DISABLED,
                    detail="live browser submissions are disabled on this deployment")
            else:
                result = asyncio.run(_drive(task))
    except BrowserBusy:
        result = BrowserTaskResult(
            outcome=BrowserTaskOutcome.REQUIRES_HUMAN,
            human_required_reason=HumanRequiredReason.LOGIN_REQUIRED,
            detail="the browser profile is in use by another run")
    logger.info(
        "browser task done phase=%s application=%s outcome=%s driver=%s",
        task.kind.value, task.application_id, result.outcome.value, result.driver_key)
    write_browser_result(result_path, result)


async def _drive(task: BrowserTask) -> BrowserTaskResult:
    """Compose the read-only context loader and a real browser, then run the task (§6-7)."""
    registry = BrowserDriverRegistry()
    engine = create_async_database_engine(DatabaseSettings.from_env())
    factory = create_session_factory(engine)
    document_settings = DocumentSettings.from_env()
    artifact_store = LocalDocumentArtifactStore(Path(document_settings.artifact_root))
    try:
        with tempfile.TemporaryDirectory(prefix="v2-browser-upload-") as staging:
            # A read-only unit of work (commit=False): the worker reads domain state and never
            # transitions it — that stays the ApplicationService's job (§7).
            async with session_scope(factory, commit=False) as session:
                loader = ApplicationContextLoader(
                    applications=SqlAlchemyApplicationRepository(session),
                    opportunities=SqlAlchemyOpportunityRepository(session),
                    profiles=SqlAlchemyCandidateProfileRepository(session),
                    documents=SqlAlchemyCandidateDocumentRepository(session),
                    artifact_store=artifact_store,
                    staging_dir=Path(staging))
                async with browser_page(profile_dir=profile_dir_for(task.user_id)) as page:
                    return await execute_on_page(
                        page, task, registry=registry, loader=loader)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="V2 browser application worker")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run(args.request, args.result)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    main()
