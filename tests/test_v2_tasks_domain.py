"""`TaskRun`/`TaskSpec` — the durable state machine every worker guarantee rests on (§32-40).

Pure domain coverage: no database, no worker. It proves the invariants the runtime leans on so the
worker's own tests can trust them:

- the lane falls out of the kind and the id out of the idempotency key, so a browser job cannot be
  relabelled onto the general lane and a re-enqueue collides on one row (§35, §37);
- the transitions produce only coherent states — a leased task has spent an attempt and holds a
  lease; a succeeded/dead-lettered one holds none — and each transition refuses to run from the
  wrong state, so an incoherent `TaskRun` is unreachable by any path (§40);
- retryability is a property of the failure *and* the remaining budget, never a guess: a permanent
  failure or an exhausted transient one must dead-letter, and `retry_scheduled` refuses to pretend
  otherwise (§38).
"""
from datetime import UTC, datetime, timedelta

import pytest

from backend.app.domain.identifiers import new_user_id, task_run_id
from backend.app.domain.task import (
    TaskFailureClass,
    TaskKind,
    TaskLane,
    TaskRun,
    TaskSpec,
    TaskStatus,
    lane_for_kind,
)

NOW = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)


def _queued(kind: TaskKind = TaskKind.RETENTION_SWEEP, *, key: str = "k",
            max_attempts: int = 3) -> TaskRun:
    user = None if kind is TaskKind.RETENTION_SWEEP else new_user_id()
    return TaskSpec(
        kind=kind, idempotency_key=key, user_id=user, max_attempts=max_attempts,
    ).to_queued_run(as_of=NOW)


# --------------------------------------------------------------------------- derivation


def test_only_application_submission_is_a_browser_lane_kind() -> None:
    """`lane_for_kind` is the single mapping the isolation rule rests on (§35)."""
    assert lane_for_kind(TaskKind.APPLICATION_SUBMISSION) is TaskLane.BROWSER
    for kind in (TaskKind.RETENTION_SWEEP, TaskKind.ACCOUNT_EXPORT, TaskKind.MATCHING):
        assert lane_for_kind(kind) is TaskLane.GENERAL


def test_spec_derives_its_lane_and_run_id() -> None:
    """A caller supplies neither lane nor id — both are derived, so neither can be spoofed."""
    spec = TaskSpec(kind=TaskKind.APPLICATION_SUBMISSION, idempotency_key="app-9",
                    user_id=new_user_id())
    assert spec.lane is TaskLane.BROWSER
    assert spec.run_id == task_run_id("app-9")


def test_to_queued_run_is_pristine() -> None:
    """A fresh enqueue is QUEUED, zero attempts, available now, no lease, no failure (§32)."""
    run = _queued(key="fresh")
    assert run.status is TaskStatus.QUEUED
    assert run.attempts == 0
    assert run.available_at == NOW == run.enqueued_at
    assert run.lease_owner is None and run.last_failure_class is None


def test_a_run_whose_id_disagrees_with_its_key_is_refused() -> None:
    """The id must be `task_run_id(idempotency_key)` — the physical form of idempotency (§37)."""
    run = _queued(key="right")
    with pytest.raises(ValueError, match="task_run_id"):
        TaskRun(**{**run.model_dump(), "idempotency_key": "wrong"})


def test_a_browser_kind_cannot_be_relabelled_onto_the_general_lane() -> None:
    """The lane must be `lane_for_kind(kind)` — a mislabelled browser job cannot be built (§35)."""
    browser = _queued(TaskKind.APPLICATION_SUBMISSION, key="b")
    with pytest.raises(ValueError, match="lane_for_kind"):
        TaskRun(**{**browser.model_dump(), "lane": TaskLane.GENERAL})


# --------------------------------------------------------------------------- happy path


def test_lease_spends_an_attempt_and_starts_the_task() -> None:
    """QUEUED → RUNNING: an attempt is spent, the lease and `started_at` are stamped (§40)."""
    leased = _queued().leased(worker="w-1", lease_expires_at=LATER, as_of=NOW)
    assert leased.status is TaskStatus.RUNNING
    assert leased.attempts == 1
    assert leased.lease_owner == "w-1" and leased.lease_expires_at == LATER
    assert leased.started_at == NOW


def test_only_a_queued_task_can_be_leased() -> None:
    """Leasing a non-QUEUED task is a programming error the transition refuses."""
    running = _queued().leased(worker="w-1", lease_expires_at=LATER, as_of=NOW)
    with pytest.raises(ValueError, match="only a QUEUED task"):
        running.leased(worker="w-2", lease_expires_at=LATER, as_of=NOW)


def test_succeed_releases_the_lease_and_finishes() -> None:
    """RUNNING → SUCCEEDED: no lease, `finished_at` stamped, no failure (§37)."""
    done = _queued().leased(
        worker="w-1", lease_expires_at=LATER, as_of=NOW).succeeded(as_of=LATER)
    assert done.status is TaskStatus.SUCCEEDED
    assert done.lease_owner is None and done.finished_at == LATER


# --------------------------------------------------------------------------- failure paths


def test_retry_scheduled_requeues_with_a_deferred_availability() -> None:
    """A transient failure with an attempt left re-queues, deferred to the caller's backoff (§38)."""
    running = _queued(max_attempts=3).leased(worker="w-1", lease_expires_at=LATER, as_of=NOW)
    retry = running.retry_scheduled(
        failure_class=TaskFailureClass.TRANSIENT, reason="PROVIDER_DOWN",
        available_at=LATER, as_of=NOW, detail="a blip")
    assert retry.status is TaskStatus.QUEUED
    assert retry.available_at == LATER          # deferred, not immediately leasable
    assert retry.attempts == 1                  # the spent attempt is not refunded
    assert retry.last_failure_class is TaskFailureClass.TRANSIENT
    assert retry.failure_reason == "PROVIDER_DOWN"


def test_retry_scheduled_refuses_a_permanent_failure() -> None:
    """A permanent failure must dead-letter — it can never masquerade as a pending retry (§38)."""
    running = _queued().leased(worker="w-1", lease_expires_at=LATER, as_of=NOW)
    with pytest.raises(ValueError, match="dead-lettered"):
        running.retry_scheduled(
            failure_class=TaskFailureClass.PERMANENT, reason="CAPTCHA",
            available_at=LATER, as_of=NOW)


def test_retry_scheduled_refuses_once_the_budget_is_spent() -> None:
    """With no attempt left, even a transient failure must dead-letter, not loop (§38)."""
    running = _queued(max_attempts=1).leased(worker="w-1", lease_expires_at=LATER, as_of=NOW)
    assert running.attempts == 1  # the only attempt is spent
    with pytest.raises(ValueError, match="dead-lettered"):
        running.retry_scheduled(
            failure_class=TaskFailureClass.TRANSIENT, reason="PROVIDER_DOWN",
            available_at=LATER, as_of=NOW)


def test_dead_letter_records_the_terminal_failure() -> None:
    """RUNNING → DEAD_LETTERED: no lease, finished, the typed reason recorded as provenance (§39)."""
    dead = _queued().leased(worker="w-1", lease_expires_at=LATER, as_of=NOW).dead_lettered(
        failure_class=TaskFailureClass.PERMANENT, reason="CAPTCHA", as_of=LATER, detail="wall")
    assert dead.status is TaskStatus.DEAD_LETTERED
    assert dead.lease_owner is None and dead.finished_at == LATER
    assert dead.failure_reason == "CAPTCHA" and dead.failure_detail == "wall"


def test_should_retry_needs_both_a_retryable_class_and_a_remaining_attempt() -> None:
    """`should_retry` is the §38 decision made pure: transient AND budget-left, else False."""
    queued = _queued(max_attempts=2)
    assert queued.should_retry(TaskFailureClass.TRANSIENT) is True
    assert queued.should_retry(TaskFailureClass.PERMANENT) is False
    exhausted = queued.leased(worker="w-1", lease_expires_at=LATER, as_of=NOW).retry_scheduled(
        failure_class=TaskFailureClass.TRANSIENT, reason="X", available_at=NOW, as_of=NOW
    ).leased(worker="w-1", lease_expires_at=LATER, as_of=NOW)
    assert exhausted.attempts == 2  # budget spent
    assert exhausted.should_retry(TaskFailureClass.TRANSIENT) is False


# --------------------------------------------------------------------------- lease recovery


def test_is_lease_stale_and_recovery_requeues_without_refunding_the_attempt() -> None:
    """A dead worker's lapsed lease returns to QUEUED, available now, the attempt still spent (§40)."""
    running = _queued(max_attempts=3).leased(worker="w-1", lease_expires_at=LATER, as_of=NOW)
    assert running.is_lease_stale(LATER + timedelta(seconds=1)) is True
    assert running.is_lease_stale(NOW) is False

    recovered = running.lease_recovered(as_of=LATER + timedelta(seconds=1))
    assert recovered.status is TaskStatus.QUEUED
    assert recovered.lease_owner is None
    assert recovered.attempts == 1  # not refunded — a repeat killer still exhausts its budget
    assert recovered.available_at == LATER + timedelta(seconds=1)


def test_recovery_refuses_a_lease_that_is_not_stale() -> None:
    """Only a genuinely lapsed lease may be recovered — a live one is left alone (§40)."""
    running = _queued().leased(worker="w-1", lease_expires_at=LATER, as_of=NOW)
    with pytest.raises(ValueError, match="stale lease"):
        running.lease_recovered(as_of=NOW)


def test_is_ready_tracks_availability() -> None:
    """A QUEUED task is due only once its `available_at` has elapsed — a backoff holds it back."""
    future = _queued().leased(
        worker="w-1", lease_expires_at=LATER, as_of=NOW).retry_scheduled(
        failure_class=TaskFailureClass.TRANSIENT, reason="X",
        available_at=LATER, as_of=NOW)
    assert future.is_ready(NOW) is False       # deferred by the backoff
    assert future.is_ready(LATER) is True
