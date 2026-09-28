"""`TaskWorker` — the runtime that leases a task, drives it, and survives a crash (§34, §38-40).

One worker serves one lane. Its loop is deliberately small and every step is a committed unit of
work, so a process that dies at any point leaves the queue coherent:

1. **Lease** the next due task on the lane (`lease_next`, `FOR UPDATE SKIP LOCKED`), committing so
   the `RUNNING`+lease is visible to every other worker — two workers racing for work each claim a
   *different* row (§40). Nothing ready → the lane is idle and the loop waits `poll_seconds`.
2. **Execute** the registered handler for the task's kind inside its own unit of work, so the
   handler's writes and the task's success transition commit together — a redelivery is then a
   no-op because the task is no longer `QUEUED`. A kind with no handler is a misconfiguration and
   dead-letters permanently rather than looping.
3. On failure, **classify** (the handler's `TaskFailure`, or a bounded-transient default for an
   unexpected exception) and either **reschedule** the retry with the policy's backoff or
   **dead-letter** it — visible, typed, durable (§38-39). The handler's partial writes rolled back
   with its unit of work; the failure is recorded in a fresh one.

Separately, a worker **recovers stale leases** (§40): a task left `RUNNING` by a worker that died
mid-run has a lapsing lease, and recovery returns it to `QUEUED` (without refunding the spent
attempt) so another worker takes it — relying on handler idempotency (§37) so the external effect,
if it happened, is not repeated. **Graceful shutdown** lets an in-flight task finish and exposes
which task is in flight; it never abandons a leased task uncommitted.

This is the SQLAlchemy-backed runtime: it owns its `session_scope` units of work and hands the
`AsyncSession` to each handler. The queue *technology* (Redis/ARQ) is a notification layer in
`backend.app.infrastructure.tasks`; the durable state a worker acts on is always the table.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.domain.identifiers import TaskRunId
from backend.app.domain.task import TaskFailureClass, TaskKind, TaskLane, TaskRun
from backend.app.infrastructure.database.engine import session_scope
from backend.app.repositories.sqlalchemy_repositories import SqlAlchemyTaskRunRepository
from backend.app.tasks.clock import Clock, utc_now
from backend.app.tasks.failures import TaskFailure, classify_unexpected
from backend.app.tasks.retry import RetryPolicy
from backend.app.tasks.settings import TaskQueueSettings

logger = logging.getLogger("jobsearch.tasks.worker")

# A task handler: given the leased run and a unit-of-work session, do the work (calling existing
# services), or raise. A `TaskFailure` states the retry verdict; any other exception is treated as
# a bounded-transient `UNEXPECTED_ERROR`. Success is "returned without raising".
TaskHandler = Callable[[TaskRun, AsyncSession], Awaitable[None]]

# The reason a task whose kind no handler serves dead-letters under — a permanent misconfiguration,
# never retried (§38). SCREAMING_SNAKE_CASE, so a dashboard can alert on it.
NO_HANDLER_REGISTERED: str = "NO_HANDLER_REGISTERED"


class TaskWorker:
    """A single-flight worker for one lane: lease, run, settle — and survive a crash (§34, §40).

    Serves exactly one `TaskLane`, processing one task at a time; lane concurrency comes from
    running several of these (see `run_lane`), not from one worker running tasks in parallel — a
    single-flight worker is what makes an in-flight task, and graceful shutdown, unambiguous. It
    holds a `session_factory` (its units of work), the `handlers` registry it dispatches on, the
    deployment `settings`, and a `Clock` a test overrides. `worker_id` is the lease holder recorded
    on every row it claims, so the dead-worker recovery sweep can name who held a lapsed lease.
    """

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession], lane: TaskLane,
                 worker_id: str, handlers: Mapping[TaskKind, TaskHandler],
                 settings: TaskQueueSettings, clock: Clock = utc_now) -> None:
        self._factory = session_factory
        self._lane = lane
        self._worker_id = worker_id
        self._handlers: dict[TaskKind, TaskHandler] = dict(handlers)
        self._settings = settings
        self._retry_policy: RetryPolicy = settings.retry_policy()
        self._clock = clock
        self._stop = asyncio.Event()
        self._current: TaskRunId | None = None
        self._last_recovery: datetime | None = None

    @property
    def lane(self) -> TaskLane:
        """The one lane this worker draws from — the isolation boundary it never crosses (§35)."""
        return self._lane

    @property
    def worker_id(self) -> str:
        """The lease holder recorded on every task this worker claims."""
        return self._worker_id

    @property
    def current_task(self) -> TaskRunId | None:
        """The task in flight now, or `None` when idle — graceful-shutdown visibility (§34)."""
        return self._current

    def request_stop(self) -> None:
        """Ask the loop to finish the in-flight task and then exit — never abandons a lease.

        Sets the stop flag; the loop checks it between claims and before leasing, so a task already
        leased is always run to a committed terminal (success/retry/dead-letter) before the worker
        exits. Idempotent — calling it twice is the same as once.
        """
        self._stop.set()

    async def run(self) -> None:
        """Lease-execute-settle until asked to stop, sweeping stale leases between claims (§40).

        The long-lived loop: recover any dead worker's lapsed leases when the sweep interval has
        elapsed, then lease the next due task and process it, or idle for `poll_seconds` when the
        lane is empty. Every step is its own committed unit of work, so a crash at any point leaves
        the queue coherent and a restart resumes cleanly.
        """
        while not self._stop.is_set():
            await self._maybe_recover_stale_leases()
            if self._stop.is_set():
                break
            task = await self._lease_next()
            if task is None:
                await self._idle()
                continue
            await self._process(task)

    async def run_until_idle(self) -> int:
        """Drain every ready task on the lane, then return how many ran — one-shot/test mode.

        Unlike `run`, this neither polls nor sleeps: it recovers stale leases once, then leases and
        processes until `lease_next` reports nothing ready, which is exactly what a deterministic
        test wants (no wall-clock wait) and what a `--once` operator drain does. Returns the count
        of tasks processed this pass.
        """
        await self._recover_stale_leases()
        processed = 0
        while not self._stop.is_set():
            task = await self._lease_next()
            if task is None:
                return processed
            await self._process(task)
            processed += 1
        return processed

    async def _lease_next(self) -> TaskRun | None:
        """Claim the next due task on this lane in its own committed unit of work (§40).

        The lease (QUEUED → RUNNING, an attempt spent, this worker's id and expiry stamped) commits
        here, so the `RUNNING` row is visible to every other worker the instant this returns — two
        workers racing each claim a different row (`FOR UPDATE SKIP LOCKED`).
        """
        now = self._clock()
        lease_expires_at = now + timedelta(seconds=self._settings.lease_seconds)
        async with session_scope(self._factory) as session:
            repository = SqlAlchemyTaskRunRepository(session)
            return await repository.lease_next(
                self._lane, worker=self._worker_id, lease_expires_at=lease_expires_at, as_of=now)

    async def _process(self, task: TaskRun) -> None:
        """Run the leased task's handler and settle it — success, retry or dead-letter (§34, §38).

        The handler's writes and the task's `SUCCEEDED` transition share one unit of work, so they
        commit together: a redelivery of a task already succeeded is then a no-op (it is no longer
        `QUEUED`). A `TaskFailure` states the retry verdict; any other exception is a bounded
        transient `UNEXPECTED_ERROR` (a handler bug must not wedge the lane). A kind no handler
        serves is a permanent misconfiguration and dead-letters rather than looping. `_current`
        exposes the in-flight task for graceful shutdown and is always cleared.
        """
        self._current = task.id
        try:
            handler = self._handlers.get(task.kind)
            if handler is None:
                await self._dead_letter_no_handler(task)
                return
            try:
                async with session_scope(self._factory) as session:
                    await handler(task, session)
                    now = self._clock()
                    await SqlAlchemyTaskRunRepository(session).save(task.succeeded(as_of=now))
            except TaskFailure as failure:
                await self._settle_failure(task, failure)
            except Exception as unexpected:  # unclassified handler bug ⇒ bounded transient retry
                await self._settle_failure(task, classify_unexpected(unexpected))
        finally:
            self._current = None

    async def _settle_failure(self, task: TaskRun, failure: TaskFailure) -> None:
        """Reschedule a retry or dead-letter the failed task, in a fresh unit of work (§38-39).

        The handler's partial writes rolled back with its own unit of work; this records the typed
        outcome in a new one. A retryable failure with an attempt left is re-queued at the policy's
        backoff; a permanent one, or a transient one that exhausted the attempt budget, dead-letters
        with its typed, secret-free reason (§39).
        """
        now = self._clock()
        if task.should_retry(failure.failure_class):
            available_at = self._retry_policy.next_available_at(attempts=task.attempts, as_of=now)
            settled = task.retry_scheduled(
                failure_class=failure.failure_class, reason=failure.reason,
                available_at=available_at, as_of=now, detail=failure.detail)
            logger.warning("task %s (%s) failed transiently after %d attempt(s); retry at %s: %s",
                           task.id, task.kind.value, task.attempts, available_at, failure.reason)
        else:
            settled = task.dead_lettered(
                failure_class=failure.failure_class, reason=failure.reason,
                as_of=now, detail=failure.detail)
            logger.error("task %s (%s) dead-lettered after %d attempt(s): %s",
                         task.id, task.kind.value, task.attempts, failure.reason)
        async with session_scope(self._factory) as session:
            await SqlAlchemyTaskRunRepository(session).save(settled)

    async def _dead_letter_no_handler(self, task: TaskRun) -> None:
        """Dead-letter a task whose kind no handler serves — a permanent misconfiguration (§38).

        Never retried: re-running finds the same empty registry. It is recorded, not silently
        dropped, so an operator sees the miswired kind in the dead-letter feed and fixes the
        deployment rather than watching work vanish.
        """
        now = self._clock()
        settled = task.dead_lettered(
            failure_class=TaskFailureClass.PERMANENT, reason=NO_HANDLER_REGISTERED,
            as_of=now, detail=f"no handler registered for kind {task.kind.value}")
        logger.error("task %s dead-lettered: no handler registered for kind %s",
                     task.id, task.kind.value)
        async with session_scope(self._factory) as session:
            await SqlAlchemyTaskRunRepository(session).save(settled)

    async def _maybe_recover_stale_leases(self) -> None:
        """Run the dead-worker recovery sweep when its interval has elapsed (§40).

        Rate-limited to once per `stale_recovery_seconds` so a busy loop does not scan the table on
        every iteration; the first pass runs immediately on startup so a crashed predecessor's work
        is reclaimed the moment a worker comes up.
        """
        now = self._clock()
        interval = timedelta(seconds=self._settings.stale_recovery_seconds)
        if self._last_recovery is not None and now - self._last_recovery < interval:
            return
        self._last_recovery = now
        await self._recover_stale_leases()

    async def _recover_stale_leases(self) -> None:
        """Return this lane's lapsed-lease tasks to `QUEUED` so another worker takes them (§40).

        A task left `RUNNING` by a worker that died mid-run has an expired lease; recovery re-queues
        it (without refunding the spent attempt, so a task that keeps killing its worker still
        exhausts its budget). Scoped to this worker's lane — the browser lane recovers its own, the
        general lane its own — matching the lane isolation the lease itself enforces (§35). Relies
        on handler idempotency (§37): if the dead worker's effect had already happened, re-running
        does not repeat it.
        """
        now = self._clock()
        async with session_scope(self._factory) as session:
            repository = SqlAlchemyTaskRunRepository(session)
            for task in await repository.list_stale_leases(now):
                if task.lane is not self._lane:
                    continue
                await repository.save(task.lease_recovered(as_of=now))
                logger.warning("recovered stale lease on task %s (worker %s presumed dead)",
                               task.id, task.lease_owner)

    async def _idle(self) -> None:
        """Wait up to `poll_seconds` for work, returning early the instant a stop is requested.

        Waiting on the stop event rather than a bare `sleep` makes graceful shutdown prompt: an
        idle worker exits at once instead of finishing a poll interval it no longer needs.
        """
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self._settings.poll_seconds)
        except TimeoutError:
            pass


async def run_lane(lane: TaskLane, *, session_factory: async_sessionmaker[AsyncSession],
                   handlers: Mapping[TaskKind, TaskHandler], settings: TaskQueueSettings,
                   clock: Clock = utc_now,
                   stop_event: asyncio.Event | None = None) -> None:
    """Run a lane's pool of single-flight workers concurrently until stopped (§35).

    The general lane runs `general_concurrency` workers; the browser lane runs
    `browser_concurrency` — separate counts, and in production separate processes/containers, so a
    wedged browser submission can never consume a general-lane worker (§35). Each worker gets a
    distinct id (`{lane}-{n}`) so a lease names exactly which worker holds it. An external
    `stop_event`, when set, asks every worker to finish its in-flight task and exit; the process
    entrypoint wires it to SIGINT/SIGTERM for graceful shutdown. With no `stop_event` the pool runs
    until cancelled.
    """
    concurrency = (settings.browser_concurrency if lane is TaskLane.BROWSER
                   else settings.general_concurrency)
    workers = [
        TaskWorker(session_factory=session_factory, lane=lane, worker_id=f"{lane.value}-{index}",
                   handlers=handlers, settings=settings, clock=clock)
        for index in range(concurrency)
    ]

    async def _watch_for_stop() -> None:
        if stop_event is None:
            return
        await stop_event.wait()
        for worker in workers:
            worker.request_stop()

    logger.info("starting %d worker(s) on the %s lane", concurrency, lane.value)
    await asyncio.gather(*(worker.run() for worker in workers), _watch_for_stop())





