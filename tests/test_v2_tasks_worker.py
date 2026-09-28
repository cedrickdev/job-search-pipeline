"""`TaskWorker` against PostgreSQL — lease, run, settle, and survive a crash (§34, §37-40, §71-72).

The pure-logic tests prove the domain and the seams; this proves the *runtime* over the real queue
table, because the worker's guarantees are properties of committed units of work that fakes cannot
show. It commits for real over independent `session_scope`s (the way a worker process does) and
truncates the queue on the way out. The headline acceptance properties:

- **§71 at-least-once safety** — a task runs to a committed terminal, and a redelivery (a second
  drain, a re-enqueue of the same key) repeats no effect: the row is no longer QUEUED, so there is
  nothing to run twice.
- **§72 lane isolation** — a general-lane worker never leases a BROWSER task and a browser-lane
  worker never leases a general one, so a wedged submission cannot consume a general worker.
- retry-with-backoff, permanent/unexpected dead-lettering, no-handler dead-lettering, lane-scoped
  stale-lease recovery (without refunding the spent attempt), and graceful shutdown that never
  abandons a leased task.
"""
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from backend.app.domain.identifiers import UserId, new_user_id
from backend.app.domain.task import (
    TaskFailureClass,
    TaskKind,
    TaskLane,
    TaskRun,
    TaskSpec,
    TaskStatus,
)
from backend.app.infrastructure.database.engine import create_session_factory, session_scope
from backend.app.repositories.sqlalchemy_repositories import SqlAlchemyTaskRunRepository
from backend.app.tasks.failures import UNEXPECTED_ERROR, TaskFailure
from backend.app.tasks.settings import TaskQueueSettings
from backend.app.tasks.worker import NO_HANDLER_REGISTERED, TaskHandler, TaskWorker
from tests.v2_rows import a_user_row

pytestmark = pytest.mark.asyncio

T0 = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


@pytest_asyncio.fixture
async def committed_world(db_engine):
    """A real session factory whose committed queue rows are truncated on teardown.

    A worker's properties live across connections that see each other's committed writes — a lease
    in one unit of work, a settle in the next — so these tests cannot lean on `db_session`'s
    rollback. On the way out `task_runs` (owner-nullable, so not reached through `users`) and
    `users` are truncated, leaving the once-per-session schema clean for whatever runs next.
    """
    factory = create_session_factory(db_engine)
    try:
        yield factory
    finally:
        async with db_engine.begin() as connection:
            await connection.execute(text("TRUNCATE task_runs, users RESTART IDENTITY CASCADE"))


class Clock:
    """A hand-cranked clock: a worker reads `now` from it, a test advances it deterministically."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _settings(**overrides: object) -> TaskQueueSettings:
    """Queue settings with a fast, deterministic default (short poll, one general worker)."""
    base: dict[str, object] = {
        "general_concurrency": 1, "browser_concurrency": 1, "lease_seconds": 300,
        "poll_seconds": 0.01, "stale_recovery_seconds": 30.0, "max_attempts": 3,
        "retry_base_delay_seconds": 60.0, "retry_max_delay_seconds": 600.0,
        "retry_multiplier": 2.0}
    base.update(overrides)
    return TaskQueueSettings(**base)  # type: ignore[arg-type]


async def _seed(factory, task: TaskRun) -> None:
    """Commit one `TaskRun` into the queue, exactly as a dispatcher or a prior worker left it."""
    async with session_scope(factory) as session:
        await SqlAlchemyTaskRunRepository(session).save(task)


async def _read(factory, run_id) -> TaskRun:
    """Read a task back in its own read-only unit of work — the committed truth after a settle."""
    async with session_scope(factory, commit=False) as session:
        found = await SqlAlchemyTaskRunRepository(session).get_by_id(run_id)
    assert found is not None
    return found


async def _seed_user(factory) -> UserId:
    """Commit one owner row so a browser task's `user_id` foreign key resolves; return its id.

    Browser-lane tasks are user-owned (`APPLICATION_SUBMISSION`), and `task_runs.user_id` carries a
    foreign key to `users`; a `TRUNCATE`d `committed_world` needs the owner present first. Ownerless
    general jobs (retention) never touch this.
    """
    user_id = new_user_id()
    async with session_scope(factory) as session:
        session.add(a_user_row(id=user_id))
    return user_id


def _queued(kind: TaskKind, *, key: str, as_of: datetime = T0,
            max_attempts: int = 3, user_id: UserId | None = None) -> TaskRun:
    """A pristine QUEUED run — an ownerless operator job unless the kind (or caller) needs an owner."""
    owner = user_id if user_id is not None else (
        None if kind is TaskKind.RETENTION_SWEEP else new_user_id())
    return TaskSpec(
        kind=kind, idempotency_key=key, user_id=owner, max_attempts=max_attempts,
    ).to_queued_run(as_of=as_of)


def _general_worker(factory, clock: Clock, handlers: dict[TaskKind, TaskHandler],
                    **settings: object) -> TaskWorker:
    return TaskWorker(
        session_factory=factory, lane=TaskLane.GENERAL, worker_id="general-0",
        handlers=handlers, settings=_settings(**settings), clock=clock)


def _browser_worker(factory, clock: Clock, handlers: dict[TaskKind, TaskHandler],
                    **settings: object) -> TaskWorker:
    return TaskWorker(
        session_factory=factory, lane=TaskLane.BROWSER, worker_id="browser-0",
        handlers=handlers, settings=_settings(**settings), clock=clock)


# --------------------------------------------------------------------------- success & §71


async def test_worker_runs_a_task_to_success_and_exposes_it_in_flight(committed_world) -> None:
    """A leased task runs its handler and commits SUCCEEDED; `current_task` names it mid-run (§34)."""
    factory = committed_world
    clock = Clock(T0)
    seen_in_flight: list[object] = []

    async def handler(task: TaskRun, session) -> None:
        seen_in_flight.append(worker.current_task)   # the worker exposes the in-flight task

    task = _queued(TaskKind.RETENTION_SWEEP, key="sweep-ok")
    await _seed(factory, task)
    worker = _general_worker(factory, clock, {TaskKind.RETENTION_SWEEP: handler})

    processed = await worker.run_until_idle()

    assert processed == 1
    assert seen_in_flight == [task.id]              # visible while running
    assert worker.current_task is None              # cleared when idle
    settled = await _read(factory, task.id)
    assert settled.status is TaskStatus.SUCCEEDED
    assert settled.attempts == 1


async def test_redelivery_repeats_no_effect(committed_world) -> None:
    """§71/§37: a second drain finds the task no longer QUEUED, so the handler runs exactly once."""
    factory = committed_world
    clock = Clock(T0)
    runs = 0

    async def handler(task: TaskRun, session) -> None:
        nonlocal runs
        runs += 1

    task = _queued(TaskKind.RETENTION_SWEEP, key="sweep-once")
    await _seed(factory, task)
    worker = _general_worker(factory, clock, {TaskKind.RETENTION_SWEEP: handler})

    first = await worker.run_until_idle()
    second = await worker.run_until_idle()          # the redelivery / re-drain

    assert first == 1 and second == 0               # nothing left to run the second time
    assert runs == 1                                 # the effect happened exactly once
    assert (await _read(factory, task.id)).status is TaskStatus.SUCCEEDED


# --------------------------------------------------------------------------- §72 lane isolation


async def test_general_worker_never_leases_a_browser_task(committed_world) -> None:
    """§72: the general worker drains its own task and leaves the BROWSER task untouched (§35)."""
    factory = committed_world
    clock = Clock(T0)
    general_runs = 0

    async def general_handler(task: TaskRun, session) -> None:
        nonlocal general_runs
        general_runs += 1

    general = _queued(TaskKind.RETENTION_SWEEP, key="general-1")
    browser = _queued(TaskKind.APPLICATION_SUBMISSION, key="browser-1",
                      user_id=await _seed_user(factory))
    await _seed(factory, general)
    await _seed(factory, browser)

    # A general worker even *offered* the browser handler must still never lease a browser row —
    # the isolation is enforced at the claim (lane-scoped `lease_next`), not merely by the registry.
    async def unreachable(task: TaskRun, session) -> None:  # pragma: no cover - must never run
        raise AssertionError("a general worker leased a browser task")

    worker = _general_worker(
        factory, clock,
        {TaskKind.RETENTION_SWEEP: general_handler, TaskKind.APPLICATION_SUBMISSION: unreachable})

    processed = await worker.run_until_idle()

    assert processed == 1 and general_runs == 1
    assert (await _read(factory, general.id)).status is TaskStatus.SUCCEEDED
    untouched = await _read(factory, browser.id)
    assert untouched.status is TaskStatus.QUEUED    # never leased by the general lane
    assert untouched.attempts == 0


async def test_browser_worker_leases_only_the_browser_task(committed_world) -> None:
    """§72: the browser worker claims the BROWSER task and leaves the general one to its lane."""
    factory = committed_world
    clock = Clock(T0)
    browser_runs = 0

    async def browser_handler(task: TaskRun, session) -> None:
        nonlocal browser_runs
        browser_runs += 1

    general = _queued(TaskKind.RETENTION_SWEEP, key="general-2")
    browser = _queued(TaskKind.APPLICATION_SUBMISSION, key="browser-2",
                      user_id=await _seed_user(factory))
    await _seed(factory, general)
    await _seed(factory, browser)
    worker = _browser_worker(
        factory, clock, {TaskKind.APPLICATION_SUBMISSION: browser_handler})

    processed = await worker.run_until_idle()

    assert processed == 1 and browser_runs == 1
    assert (await _read(factory, browser.id)).status is TaskStatus.SUCCEEDED
    assert (await _read(factory, general.id)).status is TaskStatus.QUEUED  # left for its lane


# ------------------------------------------------------------------ retry & dead-letter §38-39


async def test_transient_failure_retries_with_backoff_then_dead_letters_at_budget(
        committed_world) -> None:
    """§38-39: a transient fault re-queues at the backoff, and once the budget is spent it dies."""
    factory = committed_world
    clock = Clock(T0)
    runs = 0

    async def handler(task: TaskRun, session) -> None:
        nonlocal runs
        runs += 1
        raise TaskFailure.transient("PROVIDER_DOWN", detail="a blip")

    task = _queued(TaskKind.RETENTION_SWEEP, key="retry-me", max_attempts=2)
    await _seed(factory, task)
    worker = _general_worker(factory, clock, {TaskKind.RETENTION_SWEEP: handler})

    first = await worker.run_until_idle()               # attempt 1 → transient → rescheduled
    assert first == 1 and runs == 1
    scheduled = await _read(factory, task.id)
    assert scheduled.status is TaskStatus.QUEUED
    assert scheduled.attempts == 1
    assert scheduled.available_at == T0 + timedelta(seconds=60)  # base backoff, deferred
    assert scheduled.last_failure_class is TaskFailureClass.TRANSIENT

    held_back = await worker.run_until_idle()           # still before the backoff → nothing due
    assert held_back == 0 and runs == 1

    clock.advance(timedelta(seconds=61))                # past the backoff window
    final = await worker.run_until_idle()               # attempt 2 → budget spent → dead-letter
    assert final == 1 and runs == 2
    dead = await _read(factory, task.id)
    assert dead.status is TaskStatus.DEAD_LETTERED
    assert dead.attempts == 2                           # both attempts spent, none refunded
    assert dead.failure_reason == "PROVIDER_DOWN"


async def test_permanent_failure_dead_letters_on_the_first_attempt(committed_world) -> None:
    """§38: a permanent fault never retries — it dead-letters the instant it is seen."""
    factory = committed_world
    clock = Clock(T0)
    runs = 0

    async def handler(task: TaskRun, session) -> None:
        nonlocal runs
        runs += 1
        raise TaskFailure.permanent("CAPTCHA", detail="a wall")

    task = _queued(TaskKind.RETENTION_SWEEP, key="permanent", max_attempts=3)
    await _seed(factory, task)
    worker = _general_worker(factory, clock, {TaskKind.RETENTION_SWEEP: handler})

    processed = await worker.run_until_idle()

    assert processed == 1 and runs == 1                 # ran once, never retried
    dead = await _read(factory, task.id)
    assert dead.status is TaskStatus.DEAD_LETTERED
    assert dead.attempts == 1                           # budget untouched beyond the one try
    assert dead.failure_reason == "CAPTCHA"
    assert dead.failure_detail == "a wall"


async def test_unexpected_exception_is_a_bounded_transient_that_dead_letters(
        committed_world) -> None:
    """§38-39: an unclassified handler bug is a bounded transient; at max_attempts=1 it dies once."""
    factory = committed_world
    clock = Clock(T0)

    async def handler(task: TaskRun, session) -> None:
        raise RuntimeError("boom")                      # not a TaskFailure — the worker classifies it

    task = _queued(TaskKind.RETENTION_SWEEP, key="unexpected", max_attempts=1)
    await _seed(factory, task)
    worker = _general_worker(factory, clock, {TaskKind.RETENTION_SWEEP: handler})

    processed = await worker.run_until_idle()

    assert processed == 1
    dead = await _read(factory, task.id)
    assert dead.status is TaskStatus.DEAD_LETTERED      # one attempt, no budget to retry
    assert dead.attempts == 1
    assert dead.failure_reason == UNEXPECTED_ERROR
    assert dead.failure_detail is not None and "boom" in dead.failure_detail


async def test_a_kind_with_no_handler_dead_letters_permanently(committed_world) -> None:
    """A kind no handler serves is a misconfiguration — recorded, permanent, never looped (§38)."""
    factory = committed_world
    clock = Clock(T0)

    task = _queued(TaskKind.RETENTION_SWEEP, key="orphan")
    await _seed(factory, task)
    worker = _general_worker(factory, clock, {})        # deliberately empty registry

    processed = await worker.run_until_idle()

    assert processed == 1                               # it was leased, then settled
    dead = await _read(factory, task.id)
    assert dead.status is TaskStatus.DEAD_LETTERED
    assert dead.failure_reason == NO_HANDLER_REGISTERED


# ------------------------------------------------------------------ §40 lease recovery & shutdown


async def test_stale_lease_recovery_is_lane_scoped_and_reruns_without_refunding(
        committed_world) -> None:
    """§40: a general worker recovers only its lane's lapsed lease, re-runs it, keeps the attempt."""
    factory = committed_world
    clock = Clock(T0 + timedelta(seconds=1))            # just past the leases stamped below
    runs = 0

    async def handler(task: TaskRun, session) -> None:
        nonlocal runs
        runs += 1

    # Two tasks a since-dead worker left RUNNING, each with a lease that has already lapsed.
    general_dead = _queued(TaskKind.RETENTION_SWEEP, key="stale-general").leased(
        worker="dead-0", lease_expires_at=T0, as_of=T0)
    browser_dead = _queued(
        TaskKind.APPLICATION_SUBMISSION, key="stale-browser",
        user_id=await _seed_user(factory)).leased(
        worker="dead-1", lease_expires_at=T0, as_of=T0)
    await _seed(factory, general_dead)
    await _seed(factory, browser_dead)

    worker = _general_worker(factory, clock, {TaskKind.RETENTION_SWEEP: handler})
    processed = await worker.run_until_idle()

    assert processed == 1 and runs == 1                 # recovered, re-leased, and run once
    recovered = await _read(factory, general_dead.id)
    assert recovered.status is TaskStatus.SUCCEEDED
    assert recovered.attempts == 2                      # the dead worker's attempt is not refunded

    untouched = await _read(factory, browser_dead.id)
    assert untouched.status is TaskStatus.RUNNING       # the browser lane recovers its own (§35)
    assert untouched.attempts == 1


async def test_graceful_shutdown_before_start_never_leases_a_task(committed_world) -> None:
    """§34/§40: a stop requested before the loop starts exits at once, leaving the task QUEUED."""
    factory = committed_world
    clock = Clock(T0)

    async def handler(task: TaskRun, session) -> None:  # pragma: no cover - must never run
        raise AssertionError("a stopped worker ran a task")

    task = _queued(TaskKind.RETENTION_SWEEP, key="never-run")
    await _seed(factory, task)
    worker = _general_worker(factory, clock, {TaskKind.RETENTION_SWEEP: handler})

    worker.request_stop()
    await worker.run()                                  # returns at once — the loop guard is set

    assert worker.current_task is None                  # nothing was ever in flight
    still_queued = await _read(factory, task.id)
    assert still_queued.status is TaskStatus.QUEUED     # untouched, waiting for a live worker
    assert still_queued.attempts == 0


