"""`python -m backend.app.cli.run_worker` — run one lane's background worker pool (§34-35).

The process entrypoint for background execution. It picks a single lane, composes that lane's
handler registry from the same services the rest of the application uses (§34: the worker calls
existing services, it does not reimplement them), and runs a pool of workers until the process is
asked to stop. Two lanes, two invocations, and in production two separate containers, so a wedged
browser submission can never consume a general-lane worker (§35):

    python -m backend.app.cli.run_worker --lane general     # discovery/export/retention lane
    python -m backend.app.cli.run_worker --lane browser      # Playwright application submissions
    python -m backend.app.cli.run_worker --lane general --once   # drain ready work and exit

`--once` drains every currently-ready task and exits — a one-shot operator drain and the shape a
scheduled `RETENTION_SWEEP` runner uses. The default runs forever, polling the durable queue table
and settling each task, and shuts down gracefully on SIGINT/SIGTERM: in-flight tasks finish and
commit their terminal state before the process exits (§40).

Only the general lane's safe, useful workflows are wired today (retention, export); the browser
lane wires Phase 12's application submission, whose service re-checks all authority before the
irreversible act (§36). The schema is never migrated here — that stays `alembic upgrade head`.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.api import dependencies
from backend.app.application_engine.bootstrap import build_application_registry
from backend.app.application_engine.task_dispatcher import SubprocessTaskDispatcher
from backend.app.billing.entitlements import EntitlementResolver
from backend.app.billing.metering import MeteringService
from backend.app.core.production import validate_production_readiness
from backend.app.core.settings import DatabaseSettings, ExportSettings, RetentionSettings
from backend.app.domain.task import TaskKind, TaskLane
from backend.app.exports.store import LocalAccountExportStore
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
)
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyAccountExportRepository,
    SqlAlchemyApplicationDecisionRepository,
    SqlAlchemyApplicationEventRepository,
    SqlAlchemyApplicationPolicyRepository,
    SqlAlchemyApplicationRepository,
    SqlAlchemyCandidateDocumentRepository,
    SqlAlchemyCandidateProfileRepository,
    SqlAlchemyEligibilityResultRepository,
    SqlAlchemyMatchEvaluationRepository,
    SqlAlchemyOpportunityRepository,
    SqlAlchemyPlanRepository,
    SqlAlchemyProviderSessionRepository,
    SqlAlchemySessionRepository,
    SqlAlchemySubmissionAttemptRepository,
    SqlAlchemySubscriptionRepository,
    SqlAlchemyUsageEventRepository,
)
from backend.app.retention.service import RetentionService
from backend.app.services.applications import ApplicationService
from backend.app.tasks.handlers import build_browser_handlers, build_general_handlers
from backend.app.tasks.settings import TaskQueueSettings
from backend.app.tasks.worker import TaskHandler, TaskWorker, run_lane

logger = logging.getLogger("jobsearch.tasks.run_worker")


def _general_registry(
        export_settings: ExportSettings,
        retention_settings: RetentionSettings) -> dict[TaskKind, TaskHandler]:
    """The general lane's handler registry, wired from the same services the API uses (§34).

    Each entry is a `(session) -> service` factory: the worker opens the unit of work, the factory
    composes the service against it, and the service's writes commit with the task's success
    transition. Retention is wired inline (three repositories and the export store); export reuses
    the API's own gatherer/service builders so the eighteen-repository read side is defined once.
    """
    export_store = LocalAccountExportStore(Path(export_settings.artifact_root))

    def retention_factory(session: AsyncSession) -> RetentionService:
        return RetentionService(
            sessions=SqlAlchemySessionRepository(session),
            provider_sessions=SqlAlchemyProviderSessionRepository(session),
            exports=SqlAlchemyAccountExportRepository(session),
            export_store=export_store,
            settings=retention_settings)

    def export_factory(session: AsyncSession) -> object:
        gatherer = dependencies.account_export_gatherer(session)
        return dependencies.account_export_service(session, gatherer, export_settings)

    return build_general_handlers(
        retention_service=retention_factory, export_service=export_factory)


def _browser_registry() -> dict[TaskKind, TaskHandler]:
    """The browser lane's handler registry: exactly `APPLICATION_SUBMISSION` (§35-36).

    The one difference from the API's fallback-only `application_service` is the registry: a worker
    composes `build_application_registry(task_dispatcher=SubprocessTaskDispatcher())`, which
    registers the browser adapter so a submission is actually driven — but the handler still calls
    Phase 12's `ApplicationService.submit`, which re-checks every authority before the irreversible
    act (§36). A queued task carries no authority; this wiring adds none.

    The service is composed with the *same* authoritative `MeteringService` the API wires (§4-9):
    the browser lane is where an autonomous submission is physically sent, so the commercial
    `APPLICATION_SUBMISSIONS` clause of the effective-permission AND must be enforced here too, or a
    queued submission would spend an allowance the account no longer has. `submit` evaluates the
    safety gate *first* and only then the quota, so an exhausted plan is refused with
    `QUOTA_EXCEEDED` before the SUBMITTING transition — the browser adapter's `submit` is never
    called — and the application stays APPROVED, retryable when the window resets. The metering
    never loosens a safety gate; it can only restrict a submission the gate already permitted.
    """
    registry = build_application_registry(task_dispatcher=SubprocessTaskDispatcher())

    def application_factory(session: AsyncSession) -> ApplicationService:
        metering = MeteringService(
            EntitlementResolver(
                SqlAlchemyPlanRepository(session),
                SqlAlchemySubscriptionRepository(session)),
            SqlAlchemyUsageEventRepository(session))
        return ApplicationService(
            applications=SqlAlchemyApplicationRepository(session),
            events=SqlAlchemyApplicationEventRepository(session),
            attempts=SqlAlchemySubmissionAttemptRepository(session),
            decisions=SqlAlchemyApplicationDecisionRepository(session),
            policies=SqlAlchemyApplicationPolicyRepository(session),
            matches=SqlAlchemyMatchEvaluationRepository(session),
            eligibilities=SqlAlchemyEligibilityResultRepository(session),
            profiles=SqlAlchemyCandidateProfileRepository(session),
            opportunities=SqlAlchemyOpportunityRepository(session),
            documents=SqlAlchemyCandidateDocumentRepository(session),
            registry=registry, metering=metering)

    return build_browser_handlers(application_service=application_factory)


def _registry_for(lane: TaskLane) -> dict[TaskKind, TaskHandler]:
    """The handler registry a `lane`'s workers dispatch on — never the other lane's (§35)."""
    if lane is TaskLane.BROWSER:
        return _browser_registry()
    return _general_registry(ExportSettings.from_env(), RetentionSettings.from_env())


async def _drain_once(lane: TaskLane, *, session_factory: async_sessionmaker[AsyncSession],
                      settings: TaskQueueSettings,
                      handlers: dict[TaskKind, TaskHandler]) -> int:  # pragma: no cover
    """`--once`: one worker recovers stale leases, drains every ready task, and returns the count.

    A one-shot operator drain and the shape a scheduled `RETENTION_SWEEP` runner uses. No polling,
    no sleeps — it exits the instant the lane has nothing ready.
    """
    worker = TaskWorker(
        session_factory=session_factory, lane=lane, worker_id=f"{lane.value}-once",
        handlers=handlers, settings=settings)
    processed = await worker.run_until_idle()
    logger.info("drained %d task(s) on the %s lane", processed, lane.value)
    return processed


def _install_signal_handlers(stop_event: asyncio.Event) -> None:  # pragma: no cover
    """Wire SIGINT/SIGTERM to the stop event so a signal asks the pool to finish and exit (§40).

    `add_signal_handler` is the asyncio-safe way to set an `asyncio.Event` from a signal — it wakes
    the loop rather than interrupting mid-await. A platform without it (Windows) simply relies on
    `KeyboardInterrupt` propagating out of `asyncio.run`; either way an in-flight task commits its
    terminal state before the process exits, because each step is its own unit of work.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX loop
            logger.debug("signal %s not settable on this loop; relying on KeyboardInterrupt", sig)


async def _run(lane: TaskLane, *, once: bool) -> int:  # pragma: no cover
    """Compose this lane's engine, session factory and handlers, then run until stopped.

    The schema is never migrated here — that stays `alembic upgrade head`. The engine is disposed
    on the way out so a clean shutdown returns its pooled connections rather than leaking them.
    """
    settings = TaskQueueSettings.from_env()
    engine = create_async_database_engine(DatabaseSettings.from_env())
    session_factory = create_session_factory(engine)
    handlers = _registry_for(lane)
    try:
        if once:
            return await _drain_once(
                lane, session_factory=session_factory, settings=settings, handlers=handlers)
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        await run_lane(
            lane, session_factory=session_factory, handlers=handlers, settings=settings,
            stop_event=stop_event)
        return 0
    finally:
        await engine.dispose()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse `--lane {general,browser}` and the optional `--once` drain flag."""
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.cli.run_worker",
        description="Run one lane's background worker pool (Phase 16 §34-35).")
    parser.add_argument(
        "--lane", required=True, choices=[lane.value for lane in TaskLane],
        help="which lane this process serves; 'browser' runs only application submissions (§35)")
    parser.add_argument(
        "--once", action="store_true",
        help="drain every ready task on the lane and exit, instead of running forever")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover
    """Process entrypoint: parse the lane, configure logging, and run the pool to completion."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # A production worker fails closed on missing critical configuration exactly as the API does
    # (§53) — a no-op unless JOBSEARCH_ENV=production, so a dev drain is never gated.
    validate_production_readiness()
    lane = TaskLane(args.lane)
    try:
        return asyncio.run(_run(lane, once=args.once))
    except KeyboardInterrupt:  # a Ctrl-C before the signal handler is armed is still a clean exit
        logger.info("interrupted; shutting down the %s lane", lane.value)
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
