"""Task handlers — the thin seam from a leased `TaskRun` to an existing service (§34, §36).

A handler is the *only* code the worker runs for a given `TaskKind`, and Phase 16 §34 is emphatic
about what it may and may not do: it *calls an existing service*, it does not reimplement business
logic. So every handler here is a few lines — parse the ids the payload carries, read `now` from
the clock, call the one service method that already owns the workflow, and translate a typed
service error into the worker's retry verdict (`TaskFailure`). The heavy wiring (which repositories
a service needs) stays where it already lives; a handler receives a *factory* `(session) -> service`
and the unit-of-work `session` the worker opened, so the service's writes commit with the task's
success transition.

Three workflows are moved to background execution here, the ones that are safe and useful (§34):

- **`RETENTION_SWEEP`** — the operator job the retention service was built to be scheduled as
  (§30-31). Ownerless, idempotent; an incomplete sweep (an archive whose bytes would not delete)
  is surfaced as a bounded transient retry rather than swallowed.
- **`ACCOUNT_EXPORT`** — produce a requested account export off the queue (§23-25). Idempotent by
  the service's own contract: `produce` on an already-produced (or already-failed) export is a
  no-op, so an at-least-once redelivery repeats no work (§37).
- **`APPLICATION_SUBMISSION`** — the browser-lane job: the production handler
  commits a reservation, reloads current authority in a separate read, then invokes
  the browser adapter and finalizes in a new transaction. A queued task carries no
  authority of its own. An ambiguous send resolves to `STATE_UNKNOWN`, never a blind
  retry that could double-submit (§40).

A payload that does not carry the ids its kind needs is a *permanent* misconfiguration — re-running
finds the same broken payload — so it dead-letters rather than retrying (§38).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.billing.errors import BillingError, BillingErrorCode
from backend.app.domain.application import SubmissionOutcome, SubmissionResult
from backend.app.domain.application_channel import HumanRequiredReason
from backend.app.domain.application_failure import ApplicationError, ApplicationFailureCode
from backend.app.domain.identifiers import (
    AccountExportId,
    ApplicationId,
    DiscoveryRunId,
    SearchProfileId,
    UserId,
)
from backend.app.domain.task import TaskKind, TaskRun
from backend.app.exports.service import AccountExportService
from backend.app.infrastructure.database.engine import session_scope
from backend.app.repositories.sqlalchemy_repositories import SqlAlchemyApplicationRepository
from backend.app.retention.service import RetentionService
from backend.app.services.applications import (
    ApplicationNotActionable,
    ApplicationNotFound,
)
from backend.app.tasks.clock import Clock, utc_now
from backend.app.tasks.failures import TaskFailure
from backend.app.tasks.worker import TaskHandler

# A per-session service builder: the composition root already knows how to wire each service from a
# session (the API's dependencies do exactly this), so a handler is handed the factory rather than
# re-deriving the wiring. `T` is the service; the handler calls it with ids from the payload.
ServiceFactory = Callable[[AsyncSession], Any]

# The reasons a payload-shape or ownership problem dead-letters under — permanent, never retried
# (§38). SCREAMING_SNAKE_CASE so the dead-letter feed groups them without parsing prose.
MISSING_USER: str = "TASK_PAYLOAD_MISSING_USER"
MALFORMED_PAYLOAD: str = "TASK_PAYLOAD_MALFORMED"
NOT_ACTIONABLE: str = "APPLICATION_NOT_ACTIONABLE"
APPLICATION_ABSENT: str = "APPLICATION_NOT_FOUND"
SUBMISSION_REFUSED: str = "APPLICATION_SUBMISSION_REFUSED"
QUOTA_EXCEEDED: str = "APPLICATION_SUBMISSION_QUOTA_EXCEEDED"
RATE_LIMITED: str = "APPLICATION_RATE_LIMITED"
RETENTION_INCOMPLETE: str = "RETENTION_SWEEP_INCOMPLETE"
DISCOVERY_SEARCH_ABSENT: str = "DISCOVERY_SEARCH_NOT_FOUND"


def _require_user(task: TaskRun) -> UserId:
    """The task's owner, or a permanent failure — a user-owned job needs an owner to act for."""
    if task.user_id is None:
        raise TaskFailure.permanent(MISSING_USER, detail=f"{task.kind.value} requires a user_id")
    return task.user_id


def _require_uuid(payload: dict[str, Any], key: str, kind: TaskKind) -> UUID:
    """Parse a UUID the payload must carry, or a permanent failure — a bad payload will not heal."""
    raw = payload.get(key)
    if not isinstance(raw, str):
        raise TaskFailure.permanent(
            MALFORMED_PAYLOAD, detail=f"{kind.value} payload missing string {key!r}")
    try:
        return UUID(raw)
    except ValueError as error:
        raise TaskFailure.permanent(
            MALFORMED_PAYLOAD, detail=f"{kind.value} payload {key!r} is not a UUID") from error


def build_retention_handler(
        retention_service: ServiceFactory, *, clock: Clock = utc_now) -> TaskHandler:
    """A handler that runs the retention sweep through the existing service (§30-31, §34).

    Ownerless operator work: it calls `RetentionService.sweep(now=...)` and, if the sweep could not
    purge every archive's bytes (a store fault left rows `READY`), raises a bounded *transient*
    failure so the incomplete sweep is retried with backoff and stays visible — never silently
    dropped. The sweep is idempotent, so a redelivery re-runs it safely (§37).
    """

    async def handle(task: TaskRun, session: AsyncSession) -> None:
        service: RetentionService = retention_service(session)
        report = await service.sweep(now=clock())
        if not report.is_clean:
            raise TaskFailure.transient(
                RETENTION_INCOMPLETE,
                detail=f"{report.failed} export archive(s) could not be purged; will retry")

    return handle


def build_account_export_handler(
        export_service: ServiceFactory, *, clock: Clock = utc_now) -> TaskHandler:
    """A handler that produces a requested account export through the existing service (§23-25).

    The owner is the task's `user_id` (never the payload — the same rule the export routes follow),
    and the export id is the payload's. It calls `AccountExportService.produce`, which is idempotent
    by contract: a non-`PENDING` export is returned unchanged, so an at-least-once redelivery
    repeats no gather or store (§37). `produce` records a `FAILED` export rather than raising on a
    production fault, so the handler needs no failure translation — a produced-or-failed export is a
    completed unit of work.
    """

    async def handle(task: TaskRun, session: AsyncSession) -> None:
        user_id = _require_user(task)
        export_id = AccountExportId(_require_uuid(task.payload, "export_id", task.kind))
        service: AccountExportService = export_service(session)
        await service.produce(user_id, export_id, now=clock())

    return handle


def build_opportunity_discovery_handler(service_factory: ServiceFactory) -> TaskHandler:
    """Run a queued Swiss search through the same service as manual execution."""

    async def handle(task: TaskRun, session: AsyncSession) -> None:
        user_id = _require_user(task)
        search_id = SearchProfileId(_require_uuid(
            task.payload, "search_profile_id", task.kind))
        trigger = task.payload.get("trigger")
        if trigger not in {"MANUAL", "MORNING"}:
            raise TaskFailure.permanent(
                MALFORMED_PAYLOAD, detail="discovery trigger must be MANUAL or MORNING")
        try:
            await service_factory(session).run_profile(
                user_id, search_id, trigger=trigger,
                run_id=DiscoveryRunId(task.id))
        except LookupError as error:
            raise TaskFailure.permanent(
                DISCOVERY_SEARCH_ABSENT,
                detail="active Swiss saved search is unavailable") from error

    return handle


def build_application_submission_handler(
        application_service: ServiceFactory, *, clock: Clock = utc_now,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        live_submissions_enabled: bool = False) -> TaskHandler:
    """The browser-lane handler that submits an application through Phase 12 (§36, §40).

    §36 made physical: the handler adds no authority. In the production lane it
    commits `reserve_submission`, reloads policy and pinned context, and finalizes
    the typed result after execution. A queued task is not approval — an application
    no longer `APPROVED` raises `ApplicationNotActionable`, which is permanent.
    An ambiguous send is `STATE_UNKNOWN`, never a blind retry (§40).

    When metering is wired the service also enforces the commercial `APPLICATION_SUBMISSIONS`
    quota, which refuses an exhausted plan with `BillingError(QUOTA_EXCEEDED)` *before* the
    irreversible send (the browser adapter's `submit` is never reached). That is *permanent* here,
    not transient: a plan ceiling is resolved by an upgrade or the billing window resetting, not by
    a short retry (the same reasoning `backend.app.billing.errors` gives for mapping it to 402, not
    429), so a bounded retry would only burn attempts before dead-lettering anyway. The application
    stays APPROVED, so the user can re-queue once the window resets or the plan is upgraded (§4).
    """

    async def handle(task: TaskRun, session: AsyncSession) -> None:
        user_id = _require_user(task)
        application_id = ApplicationId(_require_uuid(task.payload, "application_id", task.kind))
        try:
            if session_factory is None:
                # Legacy in-process seam. The production lane always supplies a factory.
                await application_service(session).submit(user_id, application_id, now=clock())
                return
            async with session_scope(session_factory) as reservation_session:
                reservation = await application_service(
                    reservation_session).reserve_submission(user_id, application_id, now=clock())
            # Serialize policy brakes with the final check and browser action. A
            # pause/update must not commit and claim effect before this click ends.
            # This read-only transaction is rolled back when the browser returns.
            async with session_scope(session_factory, commit=False) as read_session:
                await SqlAlchemyApplicationRepository(
                    read_session).lock_submission_budget(user_id)
                prepared = await application_service(read_session).load_reserved_execution(
                    reservation, now=clock())
                if isinstance(prepared, SubmissionResult):
                    result = prepared
                elif not live_submissions_enabled:
                    result = SubmissionResult(
                        outcome=SubmissionOutcome.REQUIRES_HUMAN,
                        human_required_reason=HumanRequiredReason.AUTOMATION_DISABLED,
                        detail="live browser submissions are disabled on this deployment")
                else:
                    adapter, context = prepared
                    try:
                        result = await adapter.submit(context)
                    except Exception:
                        result = SubmissionResult(
                            outcome=SubmissionOutcome.STATE_UNKNOWN,
                            detail="the adapter stopped during submission; outcome is unknown")
            async with session_scope(session_factory) as final_session:
                await application_service(final_session).finalize_submission(
                    reservation, result, now=clock())
        except BillingError as error:
            if error.code is BillingErrorCode.QUOTA_EXCEEDED:
                raise TaskFailure.permanent(QUOTA_EXCEEDED, detail=error.detail) from error
            raise TaskFailure.permanent(SUBMISSION_REFUSED, detail=error.detail) from error
        except ApplicationError as error:
            if error.code is ApplicationFailureCode.APPLICATION_RATE_LIMITED:
                raise TaskFailure.transient(RATE_LIMITED, detail=error.detail) from error
            raise TaskFailure.permanent(SUBMISSION_REFUSED, detail=error.detail) from error
        except ApplicationNotActionable as error:
            raise TaskFailure.permanent(NOT_ACTIONABLE, detail=str(error)) from error
        except ApplicationNotFound as error:
            raise TaskFailure.permanent(APPLICATION_ABSENT, detail=str(error)) from error

    return handle


def build_general_handlers(*, retention_service: ServiceFactory, export_service: ServiceFactory,
                           discovery_service: ServiceFactory | None = None,
                           clock: Clock = utc_now) -> dict[TaskKind, TaskHandler]:
    """The `TaskKind -> handler` registry the general-lane worker dispatches on (§34).

    Only the general-lane kinds that are safe and useful to background today: the retention sweep
    and account export. A kind absent here dead-letters permanently at the worker if enqueued, so a
    workflow is *added* to the registry when it is moved to the background, never implied.
    """
    handlers = {
        TaskKind.RETENTION_SWEEP: build_retention_handler(retention_service, clock=clock),
        TaskKind.ACCOUNT_EXPORT: build_account_export_handler(export_service, clock=clock),
    }
    if discovery_service is not None:
        handlers[TaskKind.OPPORTUNITY_DISCOVERY] = build_opportunity_discovery_handler(
            discovery_service)
    return handlers


def build_browser_handlers(*, application_service: ServiceFactory,
                           clock: Clock = utc_now,
                           session_factory: async_sessionmaker[AsyncSession] | None = None,
                           live_submissions_enabled: bool = False) -> dict[TaskKind, TaskHandler]:
    """The `TaskKind -> handler` registry the browser-lane worker dispatches on (§35-36).

    Exactly one kind: `APPLICATION_SUBMISSION`, the only browser-lane work. Kept a separate
    registry from the general lane so the browser worker is wired with *only* the submission
    handler — it cannot be handed, and so cannot run, a general-lane job, the isolation §35 asks
    for made a property of what each worker knows how to do.
    """
    return {
        TaskKind.APPLICATION_SUBMISSION: build_application_submission_handler(
            application_service, clock=clock, session_factory=session_factory,
            live_submissions_enabled=live_submissions_enabled),
    }
