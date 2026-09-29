"""The operational HTTP surface: liveness, readiness and the metrics scrape (§42-43).

Three routes, mounted at the root rather than under `/api/v2`, because they are the platform's
plumbing, not part of its product API — a Kubernetes probe and a Prometheus scraper expect them at
well-known top-level paths, and they carry no session cookie and no CSRF token (they are `GET`s and
observe nothing owned).

`§43` insists liveness and readiness are *distinct*:

- **`/health/live`** answers "is this process up?" and touches nothing external — no database, no
  LLM provider, no job board. It is what a liveness probe reads to decide whether to restart the
  container, so making it depend on a dependency would turn a database blip into a restart storm.
- **`/health/ready`** answers "can this process serve?" and checks the one dependency an API request
  genuinely needs: the database. A third-party outage (an LLM provider, a board) deliberately does
  *not* fail readiness — it degrades that capability, not the whole API (§43).

`/metrics` renders the Prometheus text exposition (§42). It refreshes the pull gauges from the
durable tables and stays up even when the database is down — it reports `db_up 0` rather than
500ing, because the alert that fires on a database outage reads this very endpoint (§44).

Security note: these routes are unauthenticated by design (a scraper has no cookie) and expose only
aggregate, non-identifying numbers and a fixed liveness string — never a user's data or a secret.
A deployment restricts them at the ingress/network layer so only the monitoring system reaches them
(docs/OBSERVABILITY.md); nothing here is safe to expose to the public internet.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.api.dependencies import now, session_factory
from backend.app.infrastructure.database.engine import session_scope
from backend.app.observability.app_metrics import AppMetrics
from backend.app.observability.collectors import MetricsCollector
from backend.app.observability.metrics import PROMETHEUS_CONTENT_TYPE
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyLLMRunRepository,
    SqlAlchemyTaskRunRepository,
)

# The attribute `install_observability` stores the metric bundle under on `app.state`.
METRICS_ATTRIBUTE = "metrics"

router = APIRouter(tags=["observability"])


def app_metrics(request: Request) -> AppMetrics:
    """The application's metric bundle, set once by `install_observability` (§42)."""
    metrics = getattr(request.app.state, METRICS_ATTRIBUTE, None)
    if not isinstance(metrics, AppMetrics):  # pragma: no cover - install always runs first
        raise RuntimeError("observability was not installed on this application")
    return metrics


async def _database_answers(factory: async_sessionmaker[AsyncSession]) -> bool:
    """Whether the database answered a `SELECT 1` — the one dependency readiness checks (§43).

    A read-only unit of work (`commit=False`): it proves the pool can hand out a live connection
    without writing anything. Any driver-level failure (a dead pool, a refused socket) is caught and
    reported as "not ready" rather than propagated, so readiness is a boolean, never a 500.
    """
    try:
        async with session_scope(factory, commit=False) as session:
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        return False
    return True


@router.get("/health/live")
async def health_live() -> Response:
    """Liveness: the process is up. Touches no dependency, so a blip never restarts it (§43)."""
    return JSONResponse(content={"status": "alive"})


@router.get("/health/ready")
async def health_ready(
        factory: Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)],
) -> Response:
    """Readiness: the database — the one dependency an API request needs — is usable (§43)."""
    if await _database_answers(factory):
        return JSONResponse(content={"status": "ready", "checks": {"database": "ok"}})
    return JSONResponse(
        status_code=503, content={"status": "unavailable", "checks": {"database": "down"}})


@router.get("/metrics")
async def metrics(
        metrics: Annotated[AppMetrics, Depends(app_metrics)],
        factory: Annotated[async_sessionmaker[AsyncSession], Depends(session_factory)],
        instant: Annotated[datetime, Depends(now)],
) -> Response:
    """The Prometheus text exposition, refreshed from the durable tables.

    Stays up even when the database is down — the outage alert reads this very endpoint (§42, §44).
    """
    collector = MetricsCollector(metrics)
    try:
        async with session_scope(factory, commit=False) as session:
            await session.execute(text("SELECT 1"))
            await collector.collect(
                tasks=SqlAlchemyTaskRunRepository(session),
                llm=SqlAlchemyLLMRunRepository(session),
                now=instant)
    except (SQLAlchemyError, OSError):
        collector.mark_database_unavailable()
    return PlainTextResponse(metrics.render(), media_type=PROMETHEUS_CONTENT_TYPE)
