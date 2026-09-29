"""The ASGI middleware and the operational routes, driven over HTTP (§41-43).

A pure-ASGI middleware (not `BaseHTTPMiddleware`, which would buffer the career-chat SSE stream)
gives every request a correlation id and one HTTP metric. These tests drive a *minimal* app — the
observability wiring plus a few stand-in routes — so the middleware and the `/health/*` and
`/metrics` routes are exercised without the whole application graph or a database. The fully-healthy
`/metrics` render (db_up 1, populated queue/LLM gauges) needs real repositories and lives in
`test_v2_persistence_observability.py`; here the database-down path proves the endpoint stays up.
"""
from datetime import UTC, datetime
from typing import Any
from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from backend.app.api.dependencies import now, session_factory
from backend.app.observability import install_observability
from backend.app.observability.metrics import PROMETHEUS_CONTENT_TYPE

pytestmark = pytest.mark.asyncio

INSTANT = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)
BASE_URL = "http://obs.test"


class _OkSession:
    """A stand-in session that answers `SELECT 1` — enough for a readiness probe, no engine."""

    async def __aenter__(self) -> "_OkSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, statement: Any) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def commit(self) -> None:
        return None


def _dead_factory() -> Callable[[], Any]:
    """A factory whose every checkout raises as a refused socket would — the database-down case."""
    def factory() -> Any:
        raise OSError("connection refused")
    return factory


def _build_app(*, db: str = "ok") -> tuple[FastAPI, Any]:
    """A minimal app with observability wired and three stand-in routes. `db` picks the DB behaviour.

    `configure_logs=False` so the test never touches the process-wide root logger.
    """
    app = FastAPI()

    @app.get("/ok")
    async def ok() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    @app.get("/quota")
    async def quota() -> JSONResponse:
        # Stands in for a real quota denial — status 402, the series §42 says is derivable.
        return JSONResponse(status_code=402, content={"error": "quota_exceeded"})

    metrics = install_observability(app, configure_logs=False)
    app.dependency_overrides[now] = lambda: INSTANT
    if db == "ok":
        app.dependency_overrides[session_factory] = lambda: (lambda: _OkSession())
    else:
        app.dependency_overrides[session_factory] = _dead_factory
    return app, metrics


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


async def test_a_fresh_correlation_id_is_minted_and_echoed_when_none_is_supplied() -> None:
    app, _ = _build_app()
    async with _client(app) as client:
        response = await client.get("/ok")
    assert response.status_code == 200
    correlation_id = response.headers["x-correlation-id"]
    assert correlation_id  # non-empty opaque handle


@pytest.mark.parametrize("header", ["x-correlation-id", "x-request-id"])
async def test_an_inbound_correlation_header_is_honoured(header: str) -> None:
    app, _ = _build_app()
    async with _client(app) as client:
        response = await client.get("/ok", headers={header: "given-id-42"})
    assert response.headers["x-correlation-id"] == "given-id-42"


async def test_the_metric_is_labelled_by_route_template_not_the_raw_path() -> None:
    app, metrics = _build_app()
    async with _client(app) as client:
        await client.get("/items/abc")
        await client.get("/items/xyz")
    # Two different ids collapse onto one low-cardinality series — an id can never explode the labels.
    assert metrics.http_requests_total.value(
        method="GET", route="/items/{item_id}", status="200") == 2.0


async def test_an_unmatched_path_falls_into_one_bucket_and_still_gets_an_id() -> None:
    app, metrics = _build_app()
    async with _client(app) as client:
        response = await client.get("/no/such/route")
    assert response.status_code == 404
    assert response.headers["x-correlation-id"]
    # A scanner probing a thousand bad paths cannot mint a thousand series.
    assert metrics.http_requests_total.value(
        method="GET", route="__unmatched__", status="404") == 1.0


async def test_a_quota_denial_is_a_derivable_402_cell_not_a_bespoke_counter() -> None:
    app, metrics = _build_app()
    async with _client(app) as client:
        response = await client.get("/quota")
    assert response.status_code == 402
    assert metrics.http_requests_total.value(
        method="GET", route="/quota", status="402") == 1.0


async def test_the_in_flight_gauge_returns_to_zero_and_latency_is_observed() -> None:
    app, metrics = _build_app()
    async with _client(app) as client:
        await client.get("/ok")
    assert metrics.http_requests_in_flight.value() == 0.0
    # The duration histogram recorded the request against its route template.
    rendered = metrics.render()
    assert 'http_request_duration_seconds_count{method="GET",route="/ok"} 1' in rendered


async def test_health_live_is_always_up_and_touches_no_dependency() -> None:
    # Even with a dead database, liveness answers 200 — a blip must never trigger a restart (§43).
    app, _ = _build_app(db="down")
    async with _client(app) as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_health_ready_is_200_when_the_database_answers() -> None:
    app, _ = _build_app(db="ok")
    async with _client(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


async def test_health_ready_is_503_when_the_database_is_down() -> None:
    app, _ = _build_app(db="down")
    async with _client(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "down"}


async def test_metrics_stays_up_when_the_database_is_down_and_reports_db_up_zero() -> None:
    app, _ = _build_app(db="down")
    async with _client(app) as client:
        response = await client.get("/metrics")
    # The alert that fires on a database outage reads this very endpoint, so it must not 500 (§44).
    assert response.status_code == 200
    assert response.headers["content-type"] == PROMETHEUS_CONTENT_TYPE
    body = response.text
    assert "db_up 0" in body
    # The in-memory HTTP series the middleware holds are still rendered.
    assert "http_requests_total" in body
