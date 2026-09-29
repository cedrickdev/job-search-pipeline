"""The pull-based collector, driven over the fakes exactly as `/metrics` drives it over Postgres.

A scrape refreshes the gauges the middleware never sees — the database's liveness, the queue's
shape, the LLM's run outcomes — off the durable tables. These tests pin the two properties the
design promises: every declared cell is *zero-filled* so a series never vanishes into a gap a
scraper cannot read, and a database-down scrape is a *reading* (`db_up 0`, last-known gauges kept),
never a crash.
"""
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from backend.app.domain.identifiers import LLMRunId, new_user_id
from backend.app.domain.task import (
    TaskFailureClass,
    TaskKind,
    TaskLane,
    TaskSpec,
    TaskStatus,
)
from backend.app.llm.failures import LLMFailureCode
from backend.app.llm.telemetry import LLMRunStatus
from backend.app.observability.app_metrics import AppMetrics
from backend.app.observability.collectors import MetricsCollector
from tests.v2_builders import an_llm_run
from tests.v2_fakes import FakeLLMRunRepository, FakeTaskRunRepository

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
SCRAPE = NOW + timedelta(minutes=10)
FRESH_LEASE = NOW + timedelta(hours=1)     # still valid at SCRAPE
STALE_LEASE = NOW + timedelta(minutes=1)   # lapsed by SCRAPE


def _queued(kind: TaskKind, key: str, *, user_id=None):
    owner = user_id if kind is TaskKind.APPLICATION_SUBMISSION else None
    return TaskSpec(kind=kind, idempotency_key=key, user_id=owner,
                    max_attempts=3).to_queued_run(as_of=NOW)


async def _seed_queue(tasks: FakeTaskRunRepository) -> None:
    """One task in every (lane, status) an operator watches, plus a second stale RUNNING general."""
    await tasks.save(_queued(TaskKind.RETENTION_SWEEP, "q-general"))
    await tasks.save(_queued(TaskKind.MATCHING, "run-general").leased(
        worker="w", lease_expires_at=FRESH_LEASE, as_of=NOW))
    await tasks.save(_queued(TaskKind.MATCHING, "stale-general").leased(
        worker="w", lease_expires_at=STALE_LEASE, as_of=NOW))
    await tasks.save(_queued(TaskKind.MATCHING, "ok-general").leased(
        worker="w", lease_expires_at=FRESH_LEASE, as_of=NOW).succeeded(as_of=LATER))
    await tasks.save(_queued(TaskKind.MATCHING, "dead-general").leased(
        worker="w", lease_expires_at=FRESH_LEASE, as_of=NOW).dead_lettered(
        failure_class=TaskFailureClass.PERMANENT, reason="PERMANENT_FAILURE", as_of=LATER))
    await tasks.save(_queued(TaskKind.APPLICATION_SUBMISSION, "run-browser",
                             user_id=new_user_id()).leased(
        worker="w", lease_expires_at=FRESH_LEASE, as_of=NOW))


async def _seed_runs(llm: FakeLLMRunRepository) -> None:
    """Two succeeded, one failed, one still in flight — the LLM outcomes a scrape rolls up."""
    await llm.upsert(an_llm_run(id=LLMRunId(UUID(int=1)), status=LLMRunStatus.SUCCEEDED))
    await llm.upsert(an_llm_run(id=LLMRunId(UUID(int=2)), status=LLMRunStatus.SUCCEEDED))
    await llm.upsert(an_llm_run(
        id=LLMRunId(UUID(int=3)), status=LLMRunStatus.FAILED,
        failure_code=LLMFailureCode.PROVIDER_UNAVAILABLE))
    await llm.upsert(an_llm_run(
        id=LLMRunId(UUID(int=4)), status=LLMRunStatus.STARTED, finished_at=None))


async def test_collect_maps_the_queue_and_llm_tables_onto_gauges() -> None:
    metrics = AppMetrics()
    tasks, llm = FakeTaskRunRepository(), FakeLLMRunRepository()
    await _seed_queue(tasks)
    await _seed_runs(llm)

    await MetricsCollector(metrics).collect(tasks=tasks, llm=llm, now=SCRAPE)

    assert metrics.db_up.value() == 1.0
    # The queue's shape, cell by cell.
    tr = metrics.task_runs
    assert tr.value(lane="GENERAL", status="QUEUED") == 1.0
    assert tr.value(lane="GENERAL", status="RUNNING") == 2.0
    assert tr.value(lane="GENERAL", status="SUCCEEDED") == 1.0
    assert tr.value(lane="GENERAL", status="DEAD_LETTERED") == 1.0
    assert tr.value(lane="BROWSER", status="RUNNING") == 1.0
    # Only the stale general lease is counted; the two fresh leases are not.
    assert metrics.task_stale_leases.value() == 1.0
    # The LLM outcomes.
    assert metrics.llm_runs.value(status="SUCCEEDED") == 2.0
    assert metrics.llm_runs.value(status="FAILED") == 1.0
    assert metrics.llm_runs.value(status="STARTED") == 1.0


async def test_every_declared_cell_is_zero_filled_so_no_series_vanishes() -> None:
    metrics = AppMetrics()
    await MetricsCollector(metrics).collect(
        tasks=FakeTaskRunRepository(), llm=FakeLLMRunRepository(), now=SCRAPE)

    # A scraper sees an explicit 0 for every lane×status and every LLM status, never a gap.
    for lane in TaskLane:
        for status in TaskStatus:
            assert metrics.task_runs.value(lane=lane.value, status=status.value) == 0.0
    for run_status in LLMRunStatus:
        assert metrics.llm_runs.value(status=run_status.value) == 0.0
    assert metrics.task_stale_leases.value() == 0.0
    assert metrics.db_up.value() == 1.0


async def test_a_status_with_no_rows_reads_zero_even_when_others_have_rows() -> None:
    metrics = AppMetrics()
    tasks, llm = FakeTaskRunRepository(), FakeLLMRunRepository()
    await _seed_queue(tasks)
    await _seed_runs(llm)

    await MetricsCollector(metrics).collect(tasks=tasks, llm=llm, now=SCRAPE)

    # No cancelled or timed-out run was seeded, and no browser task is queued.
    assert metrics.llm_runs.value(status="CANCELLED") == 0.0
    assert metrics.llm_runs.value(status="TIMEOUT") == 0.0
    assert metrics.task_runs.value(lane="BROWSER", status="QUEUED") == 0.0


async def test_database_unavailable_sets_db_up_zero_and_keeps_the_last_known_gauges() -> None:
    metrics = AppMetrics()
    tasks, llm = FakeTaskRunRepository(), FakeLLMRunRepository()
    await _seed_queue(tasks)
    collector = MetricsCollector(metrics)
    await collector.collect(tasks=tasks, llm=llm, now=SCRAPE)

    collector.mark_database_unavailable()

    assert metrics.db_up.value() == 0.0
    # The queue gauge keeps its last-known reading — the render still carries it for the alert.
    assert metrics.task_runs.value(lane="GENERAL", status="RUNNING") == 2.0
