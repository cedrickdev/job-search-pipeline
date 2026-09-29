"""The three observability aggregates, asserted against PostgreSQL (§42).

`status_counts` on the queue and the LLM telemetry table, and `count_stale_leases` on the queue, are
the reads a `/metrics` scrape turns into gauges. They are operator-scope — one `GROUP BY`, no
`user_id`, no owned row — so these tests prove the SQL groups and filters exactly as the fakes the
collector unit test drives, and that the wired `/metrics` route renders the real numbers.

The repository tests run inside `db_session`'s transaction and roll back. The route test commits
(its rows must be visible to the fresh connection the route opens through the factory) and truncates
on teardown, exactly like the worker persistence tests.
"""
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text

from backend.app.api.dependencies import now, session_factory
from backend.app.domain.identifiers import LLMRunId
from backend.app.domain.task import TaskFailureClass, TaskKind, TaskLane, TaskSpec, TaskStatus
from backend.app.infrastructure.database.engine import create_session_factory, session_scope
from backend.app.llm.failures import LLMFailureCode
from backend.app.llm.telemetry import LLMRunStatus
from backend.app.observability import install_observability
from backend.app.observability.metrics import PROMETHEUS_CONTENT_TYPE
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyLLMRunRepository,
    SqlAlchemyTaskRunRepository,
)
from tests.v2_builders import an_llm_run

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)
SCRAPE = NOW + timedelta(minutes=10)
FRESH_LEASE = NOW + timedelta(hours=1)     # still valid at SCRAPE
STALE_LEASE = NOW + timedelta(minutes=1)   # lapsed by SCRAPE
BASE_URL = "http://obs-pg.test"


def _queued(kind: TaskKind, key: str):
    """A pristine QUEUED, ownerless run — general or browser lane, no `user_id` FK to satisfy."""
    return TaskSpec(kind=kind, idempotency_key=key, max_attempts=3).to_queued_run(as_of=NOW)


def _run(run_id: int, status: LLMRunStatus, **extra):
    """An ownerless, connectionless telemetry run — no FK to seed, just a status to group by."""
    failure = {"failure_code": LLMFailureCode.PROVIDER_UNAVAILABLE} if (
        status in {LLMRunStatus.FAILED, LLMRunStatus.TIMEOUT}) else {}
    return an_llm_run(id=LLMRunId(UUID(int=run_id)), status=status,
                      user_id=None, connection_id=None, **failure, **extra)


async def test_llm_status_counts_groups_by_status(db_session) -> None:
    repo = SqlAlchemyLLMRunRepository(db_session)
    await repo.upsert(_run(1, LLMRunStatus.SUCCEEDED))
    await repo.upsert(_run(2, LLMRunStatus.SUCCEEDED))
    await repo.upsert(_run(3, LLMRunStatus.FAILED))

    counts = {cell.status: cell.total for cell in await repo.status_counts()}

    assert counts == {LLMRunStatus.SUCCEEDED: 2, LLMRunStatus.FAILED: 1}


async def test_task_status_counts_groups_by_lane_and_status(db_session) -> None:
    repo = SqlAlchemyTaskRunRepository(db_session)
    await repo.save(_queued(TaskKind.RETENTION_SWEEP, "q-general"))
    await repo.save(_queued(TaskKind.MATCHING, "run-general").leased(
        worker="w", lease_expires_at=FRESH_LEASE, as_of=NOW))
    await repo.save(_queued(TaskKind.APPLICATION_SUBMISSION, "run-browser").leased(
        worker="w", lease_expires_at=FRESH_LEASE, as_of=NOW))

    counts = {(cell.lane, cell.status): cell.total for cell in await repo.status_counts()}

    assert counts == {
        (TaskLane.GENERAL, TaskStatus.QUEUED): 1,
        (TaskLane.GENERAL, TaskStatus.RUNNING): 1,
        (TaskLane.BROWSER, TaskStatus.RUNNING): 1}


async def test_count_stale_leases_counts_only_running_lapsed_leases(db_session) -> None:
    repo = SqlAlchemyTaskRunRepository(db_session)
    await repo.save(_queued(TaskKind.MATCHING, "stale").leased(
        worker="w", lease_expires_at=STALE_LEASE, as_of=NOW))
    await repo.save(_queued(TaskKind.MATCHING, "fresh").leased(
        worker="w", lease_expires_at=FRESH_LEASE, as_of=NOW))
    await repo.save(_queued(TaskKind.RETENTION_SWEEP, "queued"))

    # Only the lapsed RUNNING lease counts; the fresh lease and the QUEUED task do not.
    assert await repo.count_stale_leases(SCRAPE) == 1
    assert await repo.count_stale_leases(NOW) == 0


@pytest_asyncio.fixture
async def committed_factory(db_engine):
    """A real session factory whose committed rows a fresh connection can read, truncated after.

    The `/metrics` route opens its own session through the factory, on a different connection than
    any seeding one — so, like the worker tests, it cannot lean on `db_session`'s rollback and
    truncates `task_runs`, `llm_runs` and `users` on the way out.
    """
    factory = create_session_factory(db_engine)
    try:
        yield factory
    finally:
        async with db_engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE task_runs, llm_runs, users RESTART IDENTITY CASCADE"))


def _wired_app(factory) -> FastAPI:
    """A minimal app with observability wired to a real session factory (no root-logger touch)."""
    app = FastAPI()
    install_observability(app, configure_logs=False)
    app.dependency_overrides[session_factory] = lambda: factory
    app.dependency_overrides[now] = lambda: SCRAPE
    return app


async def test_metrics_renders_the_real_queue_and_llm_counts(committed_factory) -> None:
    async with session_scope(committed_factory) as session:
        await SqlAlchemyTaskRunRepository(session).save(_queued(TaskKind.RETENTION_SWEEP, "q1"))
        await SqlAlchemyTaskRunRepository(session).save(
            _queued(TaskKind.MATCHING, "run1").leased(
                worker="w", lease_expires_at=FRESH_LEASE, as_of=NOW))
        await SqlAlchemyLLMRunRepository(session).upsert(_run(1, LLMRunStatus.SUCCEEDED))

    app = _wired_app(committed_factory)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == PROMETHEUS_CONTENT_TYPE
    body = response.text
    assert "db_up 1" in body
    assert 'task_runs{lane="GENERAL",status="QUEUED"} 1' in body
    assert 'task_runs{lane="GENERAL",status="RUNNING"} 1' in body
    assert 'llm_runs{status="SUCCEEDED"} 1' in body


async def test_health_ready_is_200_against_a_live_database(committed_factory) -> None:
    app = _wired_app(committed_factory)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}
