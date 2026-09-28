"""Task handlers — the thin seam from a leased `TaskRun` to an existing service (§34, §36).

A handler calls one existing service method and translates its typed outcome into the worker's
verdict; it owns no business logic and adds no authority. These tests drive each handler over a
stub service (the `(session) -> service` factory the composition root supplies), asserting exactly
that translation and nothing more:

- a payload that does not carry the ids its kind needs is a *permanent* failure — a bad payload
  will not heal on redelivery (§38), so it dead-letters rather than retrying;
- retention surfaces an incomplete sweep as a *transient* retry; export is a plain idempotent call;
- application submission maps Phase 12's typed refusals: a rate limit is *transient* (the budget may
  free up), every other refusal — not-actionable, not-found, a policy/eligibility rejection — is
  *permanent*, because a queued task is not authority and re-running finds the same wall (§36).
"""
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from backend.app.domain.application import ApplicationState
from backend.app.domain.application_failure import ApplicationError, ApplicationFailureCode
from backend.app.domain.identifiers import new_user_id
from backend.app.domain.task import (
    TaskFailureClass,
    TaskKind,
    TaskRun,
    TaskSpec,
)
from backend.app.services.applications import ApplicationNotActionable, ApplicationNotFound
from backend.app.tasks.failures import TaskFailure
from backend.app.tasks.handlers import (
    APPLICATION_ABSENT,
    MALFORMED_PAYLOAD,
    MISSING_USER,
    NOT_ACTIONABLE,
    RATE_LIMITED,
    RETENTION_INCOMPLETE,
    SUBMISSION_REFUSED,
    build_account_export_handler,
    build_application_submission_handler,
    build_general_handlers,
    build_retention_handler,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)
SESSION: Any = object()  # the handlers pass it straight to the stub factory, which ignores it


def _queued(kind: TaskKind, *, key: str, user_id: Any = None,
            payload: dict[str, Any] | None = None) -> TaskRun:
    """A pristine QUEUED run of `kind` — the shape the worker hands a handler."""
    return TaskSpec(
        kind=kind, idempotency_key=key, user_id=user_id, payload=payload or {},
    ).to_queued_run(as_of=NOW)


def _clock() -> datetime:
    return NOW


# --------------------------------------------------------------------------- retention


async def test_retention_handler_calls_the_sweep_and_passes_on_a_clean_report() -> None:
    """A clean sweep is a completed unit of work — the handler returns without raising (§30-31)."""
    calls: list[datetime] = []

    async def sweep(*, now: datetime) -> SimpleNamespace:
        calls.append(now)
        return SimpleNamespace(is_clean=True, failed=0)

    handler = build_retention_handler(lambda _s: SimpleNamespace(sweep=sweep), clock=_clock)
    await handler(_queued(TaskKind.RETENTION_SWEEP, key="sweep-1"), SESSION)

    assert calls == [NOW]  # the sweep ran, at the handler's clock instant


async def test_retention_handler_retries_an_incomplete_sweep() -> None:
    """An archive whose bytes would not delete is a bounded *transient* retry, never swallowed."""
    async def sweep(*, now: datetime) -> SimpleNamespace:
        return SimpleNamespace(is_clean=False, failed=2)

    handler = build_retention_handler(lambda _s: SimpleNamespace(sweep=sweep), clock=_clock)

    with pytest.raises(TaskFailure) as caught:
        await handler(_queued(TaskKind.RETENTION_SWEEP, key="sweep-2"), SESSION)
    assert caught.value.failure_class is TaskFailureClass.TRANSIENT
    assert caught.value.reason == RETENTION_INCOMPLETE


# --------------------------------------------------------------------------- account export


async def test_export_handler_produces_the_payload_export_for_the_task_owner() -> None:
    """The owner is the task's `user_id` (never the payload); the export id is the payload's."""
    user = new_user_id()
    seen: dict[str, Any] = {}

    async def produce(owner: Any, export_id: Any, *, now: datetime) -> None:
        seen.update(owner=owner, export_id=export_id, now=now)

    handler = build_account_export_handler(lambda _s: SimpleNamespace(produce=produce), clock=_clock)
    export_uuid = "11111111-1111-4111-8111-111111111111"
    await handler(
        _queued(TaskKind.ACCOUNT_EXPORT, key="exp-1", user_id=user,
                payload={"export_id": export_uuid}),
        SESSION)

    assert seen["owner"] == user
    assert str(seen["export_id"]) == export_uuid
    assert seen["now"] == NOW


async def test_export_handler_dead_letters_a_task_with_no_owner() -> None:
    """A user-owned job needs an owner to act for — a missing `user_id` is a permanent failure."""
    handler = build_account_export_handler(lambda _s: SimpleNamespace(), clock=_clock)

    with pytest.raises(TaskFailure) as caught:
        await handler(
            _queued(TaskKind.ACCOUNT_EXPORT, key="exp-2",
                    payload={"export_id": "11111111-1111-4111-8111-111111111111"}),
            SESSION)
    assert caught.value.failure_class is TaskFailureClass.PERMANENT
    assert caught.value.reason == MISSING_USER


async def test_export_handler_dead_letters_a_malformed_payload() -> None:
    """A payload missing its export id will not heal on redelivery — a permanent failure (§38)."""
    handler = build_account_export_handler(lambda _s: SimpleNamespace(), clock=_clock)

    with pytest.raises(TaskFailure) as caught:
        await handler(
            _queued(TaskKind.ACCOUNT_EXPORT, key="exp-3", user_id=new_user_id(), payload={}),
            SESSION)
    assert caught.value.failure_class is TaskFailureClass.PERMANENT
    assert caught.value.reason == MALFORMED_PAYLOAD


async def test_export_handler_dead_letters_a_non_uuid_export_id() -> None:
    """A non-UUID export id is malformed, not transient — the same permanent verdict."""
    handler = build_account_export_handler(lambda _s: SimpleNamespace(), clock=_clock)

    with pytest.raises(TaskFailure) as caught:
        await handler(
            _queued(TaskKind.ACCOUNT_EXPORT, key="exp-4", user_id=new_user_id(),
                    payload={"export_id": "not-a-uuid"}),
            SESSION)
    assert caught.value.reason == MALFORMED_PAYLOAD


# --------------------------------------------------------------------------- application submission


def _submission_task() -> TaskRun:
    return _queued(
        TaskKind.APPLICATION_SUBMISSION, key="app-1", user_id=new_user_id(),
        payload={"application_id": "22222222-2222-4222-8222-222222222222"})


def _handler_that_submits(submit: Any):
    return build_application_submission_handler(
        lambda _s: SimpleNamespace(submit=submit), clock=_clock)


async def test_submission_handler_calls_submit_with_the_owner_and_application() -> None:
    """The handler adds no authority — it just calls Phase 12's `submit`, which re-checks all (§36)."""
    seen: dict[str, Any] = {}

    async def submit(owner: Any, application_id: Any, *, now: datetime) -> None:
        seen.update(owner=owner, application_id=application_id, now=now)

    task = _submission_task()
    await _handler_that_submits(submit)(task, SESSION)

    assert seen["owner"] == task.user_id
    assert str(seen["application_id"]) == "22222222-2222-4222-8222-222222222222"
    assert seen["now"] == NOW


async def test_submission_handler_retries_only_a_rate_limit() -> None:
    """A rate-limit refusal is *transient* — the budget may free up, so it is worth retrying (§49)."""
    async def submit(*_a: Any, **_k: Any) -> None:
        raise ApplicationError(ApplicationFailureCode.APPLICATION_RATE_LIMITED)

    with pytest.raises(TaskFailure) as caught:
        await _handler_that_submits(submit)(_submission_task(), SESSION)
    assert caught.value.failure_class is TaskFailureClass.TRANSIENT
    assert caught.value.reason == RATE_LIMITED


async def test_submission_handler_dead_letters_any_other_application_error() -> None:
    """Every non-rate-limit refusal is *permanent* — re-running finds the same wall (§36)."""
    async def submit(*_a: Any, **_k: Any) -> None:
        raise ApplicationError(ApplicationFailureCode.APPLICATION_ADAPTER_ERROR)

    with pytest.raises(TaskFailure) as caught:
        await _handler_that_submits(submit)(_submission_task(), SESSION)
    assert caught.value.failure_class is TaskFailureClass.PERMANENT
    assert caught.value.reason == SUBMISSION_REFUSED


async def test_submission_handler_dead_letters_a_stale_not_actionable_task() -> None:
    """A queued task is not approval: an application no longer APPROVED is a permanent stop (§36)."""
    async def submit(*_a: Any, **_k: Any) -> None:
        raise ApplicationNotActionable(ApplicationState.SUBMITTED, "submit")

    with pytest.raises(TaskFailure) as caught:
        await _handler_that_submits(submit)(_submission_task(), SESSION)
    assert caught.value.failure_class is TaskFailureClass.PERMANENT
    assert caught.value.reason == NOT_ACTIONABLE


async def test_submission_handler_dead_letters_a_missing_application() -> None:
    """An application that is not there (or not this owner's) is a permanent failure, never retried."""
    async def submit(*_a: Any, **_k: Any) -> None:
        raise ApplicationNotFound("gone")

    with pytest.raises(TaskFailure) as caught:
        await _handler_that_submits(submit)(_submission_task(), SESSION)
    assert caught.value.failure_class is TaskFailureClass.PERMANENT
    assert caught.value.reason == APPLICATION_ABSENT


# --------------------------------------------------------------------------- registries


async def test_general_registry_carries_only_the_safe_general_kinds() -> None:
    """The general lane serves retention and export — and, deliberately, nothing else (§34)."""
    handlers = build_general_handlers(
        retention_service=lambda _s: SimpleNamespace(),
        export_service=lambda _s: SimpleNamespace())
    assert set(handlers) == {TaskKind.RETENTION_SWEEP, TaskKind.ACCOUNT_EXPORT}
    assert TaskKind.APPLICATION_SUBMISSION not in handlers  # never on the general lane (§35)
