"""The task-queue seam: durable dispatcher, retry backoff and deployment settings (§32-38).

Pure-logic coverage over the fakes and the settings/backoff maths — no database, no Redis. It
proves the three things a service and a worker rest on before any I/O is involved:

- `PersistedTaskDispatcher` enqueues by persisting one `QUEUED` `TaskRun`, and enqueuing the same
  job twice collapses onto one row and returns the same id (§37) — idempotency by construction,
  the property the whole at-least-once design leans on.
- `RetryPolicy` computes a deterministic, capped exponential backoff (§38), so a test can assert
  the exact instant a transient retry becomes leasable.
- `TaskQueueSettings.from_env` reads safe defaults, parses overrides, resolves the Redis URL
  most-specific-first, and refuses a non-numeric knob rather than guessing.
"""
from datetime import UTC, datetime, timedelta

import pytest

from backend.app.domain.identifiers import new_user_id, task_run_id
from backend.app.domain.task import TaskKind, TaskLane, TaskSpec, TaskStatus
from backend.app.tasks.clock import Clock
from backend.app.tasks.dispatcher import PersistedTaskDispatcher, TaskDispatcher
from backend.app.tasks.retry import RetryPolicy
from backend.app.tasks.settings import LOCAL_DEV_REDIS_URL, TaskQueueSettings
from tests.v2_fakes import FakeTaskDispatcher, FakeTaskRunRepository

NOW = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


def _clock(instant: datetime) -> Clock:
    """A frozen clock so an enqueue's timestamps are the test's, not the wall's."""
    return lambda: instant


def _export_spec(key: str = "export-1") -> TaskSpec:
    """A general-lane spec for a user-owned export — the common shape a service enqueues."""
    return TaskSpec(
        kind=TaskKind.ACCOUNT_EXPORT, idempotency_key=key,
        user_id=new_user_id(), payload={"export_id": "e"})


# --------------------------------------------------------------------------- dispatchers


@pytest.mark.asyncio
async def test_persisted_dispatcher_enqueues_one_queued_run() -> None:
    """`enqueue` persists exactly one pristine `QUEUED` run and returns its derived id (§32)."""
    tasks = FakeTaskRunRepository()
    dispatcher = PersistedTaskDispatcher(tasks, clock=_clock(NOW))

    run_id = await dispatcher.enqueue(_export_spec(key="export-abc"))

    assert run_id == task_run_id("export-abc")
    stored = await tasks.get_by_id(run_id)
    assert stored is not None
    assert stored.status is TaskStatus.QUEUED
    assert stored.attempts == 0
    assert stored.available_at == NOW
    assert stored.enqueued_at == NOW


@pytest.mark.asyncio
async def test_persisted_dispatcher_is_idempotent_on_re_enqueue() -> None:
    """Enqueuing the same job twice collapses onto one row and returns the same id (§37)."""
    tasks = FakeTaskRunRepository()
    dispatcher = PersistedTaskDispatcher(tasks, clock=_clock(NOW))
    spec = _export_spec(key="same-key")

    first = await dispatcher.enqueue(spec)
    second = await dispatcher.enqueue(spec)

    assert first == second
    assert len(tasks.tasks) == 1


@pytest.mark.asyncio
async def test_persisted_dispatcher_re_enqueue_never_resets_an_in_flight_run() -> None:
    """A redelivered enqueue returns the existing row untouched — never resets a RUNNING task."""
    tasks = FakeTaskRunRepository()
    dispatcher = PersistedTaskDispatcher(tasks, clock=_clock(NOW))
    spec = _export_spec(key="in-flight")
    run_id = await dispatcher.enqueue(spec)

    # The row is leased (RUNNING) — a re-enqueue must not drag it back to QUEUED.
    leased = (await tasks.get_by_id(run_id)).leased(  # type: ignore[union-attr]
        worker="w-1", lease_expires_at=NOW + timedelta(minutes=5), as_of=NOW)
    await tasks.save(leased)

    again = await dispatcher.enqueue(spec)

    assert again == run_id
    still = await tasks.get_by_id(run_id)
    assert still is not None and still.status is TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_persisted_dispatcher_satisfies_the_protocol() -> None:
    """The concrete dispatcher is a structural `TaskDispatcher` — the seam a service depends on."""
    assert isinstance(
        PersistedTaskDispatcher(FakeTaskRunRepository(), clock=_clock(NOW)), TaskDispatcher)


@pytest.mark.asyncio
async def test_fake_dispatcher_records_once_and_reports_kinds() -> None:
    """The test double is idempotent like the real one and exposes first-seen kinds for asserts."""
    dispatcher = FakeTaskDispatcher()
    spec = _export_spec(key="dup")

    await dispatcher.enqueue(spec)
    await dispatcher.enqueue(spec)

    assert dispatcher.enqueued_kinds == [TaskKind.ACCOUNT_EXPORT]
    assert isinstance(dispatcher, TaskDispatcher)


# --------------------------------------------------------------------------- retry policy


def test_retry_backoff_grows_then_caps() -> None:
    """Backoff is `base * multiplier ** (attempts - 1)`, held flat at the cap (§38)."""
    policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=8.0, multiplier=2.0)

    assert policy.backoff_for(1) == timedelta(seconds=1)   # first retry: base
    assert policy.backoff_for(2) == timedelta(seconds=2)
    assert policy.backoff_for(3) == timedelta(seconds=4)
    assert policy.backoff_for(4) == timedelta(seconds=8)   # cap reached
    assert policy.backoff_for(9) == timedelta(seconds=8)   # cap holds it flat


def test_retry_backoff_clamps_a_zero_attempt_to_the_base() -> None:
    """A non-positive attempt count cannot produce a negative exponent — it uses the base delay."""
    policy = RetryPolicy(base_delay_seconds=3.0, max_delay_seconds=99.0, multiplier=2.0)
    assert policy.backoff_for(0) == timedelta(seconds=3)


def test_retry_backoff_survives_an_overflowing_exponent() -> None:
    """A huge attempt count falls back to the cap rather than raising `OverflowError` (§38)."""
    policy = RetryPolicy(base_delay_seconds=2.0, max_delay_seconds=300.0, multiplier=2.0)
    assert policy.backoff_for(100_000) == timedelta(seconds=300)


def test_retry_next_available_at_offsets_from_the_given_instant() -> None:
    """`next_available_at` is `as_of + backoff`, so a re-queue's timestamp is deterministic."""
    policy = RetryPolicy(base_delay_seconds=5.0, max_delay_seconds=60.0, multiplier=2.0)
    assert policy.next_available_at(attempts=2, as_of=NOW) == NOW + timedelta(seconds=10)


# --------------------------------------------------------------------------- settings


def test_settings_default_to_safe_modest_values() -> None:
    """An unset deployment still runs correctly — the dev Redis, one browser worker (§35)."""
    settings = TaskQueueSettings.from_env({})
    assert settings.redis_url == LOCAL_DEV_REDIS_URL
    assert settings.general_concurrency == 4
    assert settings.browser_concurrency == 1
    assert settings.max_attempts == 5


def test_settings_parse_overrides_from_the_environment() -> None:
    """Every knob is read from its variable, so a deployment tunes without touching code."""
    settings = TaskQueueSettings.from_env({
        "JOBSEARCH_WORKER_GENERAL_CONCURRENCY": "8",
        "JOBSEARCH_WORKER_BROWSER_CONCURRENCY": "2",
        "JOBSEARCH_WORKER_LEASE_SECONDS": "120",
        "JOBSEARCH_WORKER_POLL_SECONDS": "0.5",
        "JOBSEARCH_WORKER_MAX_ATTEMPTS": "3",
    })
    assert settings.general_concurrency == 8
    assert settings.browser_concurrency == 2
    assert settings.lease_seconds == 120
    assert settings.poll_seconds == 0.5
    assert settings.max_attempts == 3


def test_settings_resolve_redis_url_most_specific_first() -> None:
    """A project-scoped `JOBSEARCH_REDIS_URL` wins over a generic `REDIS_URL` (no silent redirect)."""
    settings = TaskQueueSettings.from_env({
        "JOBSEARCH_REDIS_URL": "redis://specific:6379/1",
        "REDIS_URL": "redis://generic:6379/0",
    })
    assert settings.redis_url == "redis://specific:6379/1"


def test_settings_fall_back_to_generic_redis_url() -> None:
    """With only the generic variable set, it is used — the fallback the resolution allows."""
    settings = TaskQueueSettings.from_env({"REDIS_URL": "redis://generic:6379/0"})
    assert settings.redis_url == "redis://generic:6379/0"


def test_settings_refuse_a_non_numeric_knob() -> None:
    """A malformed integer is a configuration error, never a silent default (the settings rule)."""
    with pytest.raises(ValueError, match="must be an integer"):
        TaskQueueSettings.from_env({"JOBSEARCH_WORKER_MAX_ATTEMPTS": "lots"})


def test_settings_keep_the_redis_url_out_of_repr() -> None:
    """The URL can carry a password, so it never appears in the repr (§41: never log a secret)."""
    settings = TaskQueueSettings.from_env({"JOBSEARCH_REDIS_URL": "redis://:secret@host:6379/0"})
    assert "secret" not in repr(settings)


def test_settings_retry_policy_mirrors_its_knobs() -> None:
    """`retry_policy()` carries the settings' backoff numbers verbatim (§38)."""
    settings = TaskQueueSettings.from_env({
        "JOBSEARCH_WORKER_RETRY_BASE_SECONDS": "3",
        "JOBSEARCH_WORKER_RETRY_MAX_SECONDS": "120",
        "JOBSEARCH_WORKER_RETRY_MULTIPLIER": "3",
    })
    policy = settings.retry_policy()
    assert policy.base_delay_seconds == 3.0
    assert policy.max_delay_seconds == 120.0
    assert policy.multiplier == 3.0
