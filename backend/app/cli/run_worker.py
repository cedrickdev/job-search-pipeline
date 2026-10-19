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
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.api import dependencies
from backend.app.application_engine.bootstrap import build_application_registry
from backend.app.application_engine.browser.form_capabilities import load_form_capabilities
from backend.app.application_engine.task_dispatcher import SubprocessTaskDispatcher
from backend.app.billing.entitlements import EntitlementResolver
from backend.app.billing.metering import MeteringService
from backend.app.claude_terminal.service import ClaudeTerminalSessionService
from backend.app.core.production import validate_production_readiness
from backend.app.core.settings import (
    BrowserSubmissionSettings,
    ClaudeTerminalSettings,
    DatabaseSettings,
    DocumentSettings,
    ExportSettings,
    LLMSecretSettings,
    RetentionSettings,
)
from backend.app.discovery.ats_board_catalog import ATS_REVIEWED_BOARDS_PATH, load_reviewed_boards
from backend.app.discovery.bootstrap import build_country_packs, build_discovery
from backend.app.domain.task import TaskKind, TaskLane
from backend.app.exports.store import LocalAccountExportStore
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
)
from backend.app.llm.secrets import FernetSecretCipher
from backend.app.repositories.ats_request_budget import SqlAlchemyAtsRequestBudget
from backend.app.repositories.morning_preparation import MorningPreparationRepository
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyAccountExportRepository,
    SqlAlchemyApplicationDecisionRepository,
    SqlAlchemyApplicationEventRepository,
    SqlAlchemyApplicationPolicyRepository,
    SqlAlchemyApplicationRepository,
    SqlAlchemyCandidateDocumentRepository,
    SqlAlchemyCandidateProfileRepository,
    SqlAlchemyClaudeTerminalSessionRepository,
    SqlAlchemyEligibilityResultRepository,
    SqlAlchemyMatchEvaluationRepository,
    SqlAlchemyOpportunityRepository,
    SqlAlchemyPlanRepository,
    SqlAlchemyProviderSessionRepository,
    SqlAlchemySessionRepository,
    SqlAlchemySubmissionAttemptRepository,
    SqlAlchemySubscriptionRepository,
    SqlAlchemyTaskRunRepository,
    SqlAlchemyUsageEventRepository,
)
from backend.app.retention.service import RetentionService
from backend.app.services.application_decisions import ApplicationDecisionService
from backend.app.services.application_queue import ApplicationQueueService
from backend.app.services.applications import ApplicationService
from backend.app.services.assessment import AssessmentService
from backend.app.services.claude_terminal import (
    ClaudeTerminalService,
    InternalTerminalManagerClient,
)
from backend.app.services.discovery_catalog import DiscoveryCatalogService
from backend.app.services.discovery_source_access import SourceAccessPolicy
from backend.app.services.morning_application import MorningApplicationService
from backend.app.services.morning_documents import MorningDocumentsService
from backend.app.services.morning_preparation import MorningPreparationService
from backend.app.tasks.clock import utc_now
from backend.app.tasks.dispatcher import PersistedTaskDispatcher
from backend.app.tasks.handlers import build_browser_handlers, build_general_handlers
from backend.app.tasks.settings import TaskQueueSettings
from backend.app.tasks.worker import TaskHandler, TaskWorker, run_lane

logger = logging.getLogger("jobsearch.tasks.run_worker")


def _general_registry(
        export_settings: ExportSettings,
        retention_settings: RetentionSettings,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        ) -> dict[TaskKind, TaskHandler]:
    """The general lane's handler registry, wired from the same services the API uses (§34).

    Each entry is a `(session) -> service` factory: the worker opens the unit of work, the factory
    composes the service against it, and the service's writes commit with the task's success
    transition. Retention is wired inline (three repositories and the export store); export reuses
    the API's own gatherer/service builders so the eighteen-repository read side is defined once.
    """
    export_store = LocalAccountExportStore(Path(export_settings.artifact_root))
    terminal_settings = ClaudeTerminalSettings.from_env()

    def retention_factory(session: AsyncSession) -> RetentionService:
        terminal_reaper = None
        if (terminal_settings.enabled and terminal_settings.manager_url
                and terminal_settings.manager_token):
            terminal_reaper = ClaudeTerminalService(
                ClaudeTerminalSessionService(
                    SqlAlchemyClaudeTerminalSessionRepository(session),
                    session_ttl=timedelta(minutes=terminal_settings.session_ttl_minutes),
                ),
                InternalTerminalManagerClient(
                    base_url=terminal_settings.manager_url,
                    token=terminal_settings.manager_token,
                ),
            )
        return RetentionService(
            sessions=SqlAlchemySessionRepository(session),
            provider_sessions=SqlAlchemyProviderSessionRepository(session),
            exports=SqlAlchemyAccountExportRepository(session),
            export_store=export_store,
            settings=retention_settings,
            terminal_reaper=terminal_reaper)

    def export_factory(session: AsyncSession) -> object:
        gatherer = dependencies.account_export_gatherer(session)
        return dependencies.account_export_service(session, gatherer, export_settings)

    if session_factory is None:
        return build_general_handlers(
            retention_service=retention_factory, export_service=export_factory)

    catalog = load_reviewed_boards(ATS_REVIEWED_BOARDS_PATH)
    orchestrator = build_discovery(
        boards=catalog.boards,
        budget=SqlAlchemyAtsRequestBudget(session_factory)).orchestrator
    source_policy = SourceAccessPolicy.from_env(catalog)
    packs = build_country_packs()
    document_settings = DocumentSettings.from_env()
    secret_settings = LLMSecretSettings.from_env()
    cipher = (FernetSecretCipher(
        secret_settings.master_key, version=secret_settings.active_version,
        older=dict(secret_settings.previous_keys))
        if secret_settings.master_key else None)

    def discovery_factory(_session: AsyncSession) -> DiscoveryCatalogService:
        return DiscoveryCatalogService(session_factory, orchestrator,
                                       source_policy=source_policy,
                                       prepare_discovered=True)

    def matching_factory(session: AsyncSession) -> MorningPreparationService:
        profiles = SqlAlchemyCandidateProfileRepository(session)
        opportunities = SqlAlchemyOpportunityRepository(session)
        matches = SqlAlchemyMatchEvaluationRepository(session)
        eligibilities = SqlAlchemyEligibilityResultRepository(session)
        return MorningPreparationService(
            ledger=MorningPreparationRepository(session),
            assessor=AssessmentService(profiles, opportunities, matches,
                                       eligibilities, packs),
            decider=ApplicationDecisionService(
                profiles=profiles, opportunities=opportunities, matches=matches,
                eligibilities=eligibilities,
                policies=SqlAlchemyApplicationPolicyRepository(session),
                decisions=SqlAlchemyApplicationDecisionRepository(session)),
            dispatcher=PersistedTaskDispatcher(SqlAlchemyTaskRunRepository(session),
                                               clock=utc_now),
        )

    def assessment_factory(session: AsyncSession) -> AssessmentService:
        return AssessmentService(
            SqlAlchemyCandidateProfileRepository(session),
            SqlAlchemyOpportunityRepository(session),
            SqlAlchemyMatchEvaluationRepository(session),
            SqlAlchemyEligibilityResultRepository(session), packs)

    def decision_factory(session: AsyncSession) -> ApplicationDecisionService:
        return ApplicationDecisionService(
            profiles=SqlAlchemyCandidateProfileRepository(session),
            opportunities=SqlAlchemyOpportunityRepository(session),
            matches=SqlAlchemyMatchEvaluationRepository(session),
            eligibilities=SqlAlchemyEligibilityResultRepository(session),
            policies=SqlAlchemyApplicationPolicyRepository(session),
            decisions=SqlAlchemyApplicationDecisionRepository(session))

    def document_factory(_session: AsyncSession) -> MorningDocumentsService:
        return MorningDocumentsService(
            session_factory,
            document_builder=lambda session, user_id: (
                dependencies.compose_document_service_for_user(
                    session, user_id, settings=document_settings, cipher=cipher)),
            assessor_builder=assessment_factory,
            decider_builder=decision_factory,
            dispatcher_builder=lambda session: PersistedTaskDispatcher(
                SqlAlchemyTaskRunRepository(session), clock=utc_now),
        )

    return build_general_handlers(
        retention_service=retention_factory, export_service=export_factory,
        discovery_service=discovery_factory, matching_service=matching_factory,
        document_service=document_factory)


def _browser_registry(
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        ) -> dict[TaskKind, TaskHandler]:
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
    browser_settings = BrowserSubmissionSettings.from_env()

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
            registry=registry, metering=metering,
            form_catalog=load_form_capabilities(
                browser_settings.form_catalog_path, today=date.today()))

    live_enabled = browser_settings.live_enabled

    def queue_factory(session: AsyncSession) -> ApplicationQueueService:
        return ApplicationQueueService(
            SqlAlchemyApplicationRepository(session),
            SqlAlchemyApplicationPolicyRepository(session),
            SqlAlchemyOpportunityRepository(session),
            PersistedTaskDispatcher(SqlAlchemyTaskRunRepository(session), clock=utc_now),
            live_enabled=live_enabled,
            form_catalog=load_form_capabilities(
                browser_settings.form_catalog_path, today=date.today()),
        )

    return build_browser_handlers(
        application_service=application_factory,
        application_preparation_service=(MorningApplicationService(
            session_factory, application_builder=application_factory,
            queue_builder=queue_factory,
            task_dispatcher_builder=lambda session: PersistedTaskDispatcher(
                SqlAlchemyTaskRunRepository(session), clock=utc_now),
            live_enabled=live_enabled)
            if session_factory is not None else None),
        session_factory=session_factory,
        live_submissions_enabled=live_enabled)


def _registry_for(lane: TaskLane, *,
                  session_factory: async_sessionmaker[AsyncSession] | None = None,
                  ) -> dict[TaskKind, TaskHandler]:
    """The handler registry a `lane`'s workers dispatch on — never the other lane's (§35)."""
    if lane is TaskLane.BROWSER:
        return _browser_registry(session_factory)
    return _general_registry(ExportSettings.from_env(), RetentionSettings.from_env(),
                             session_factory)


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
    handlers = _registry_for(lane, session_factory=session_factory)
    try:
        if lane is TaskLane.BROWSER:
            reconciler = MorningApplicationService(
                session_factory,
                task_dispatcher_builder=lambda session: PersistedTaskDispatcher(
                    SqlAlchemyTaskRunRepository(session), clock=utc_now),
            )
            await reconciler.reconcile_ready()
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
        "--lane", required=True, type=str.upper,
        choices=[lane.value for lane in TaskLane],
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
        asyncio.run(_run(lane, once=args.once))
        return 0
    except KeyboardInterrupt:  # a Ctrl-C before the signal handler is armed is still a clean exit
        logger.info("interrupted; shutting down the %s lane", lane.value)
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
