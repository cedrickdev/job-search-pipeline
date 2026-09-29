"""Observability wiring: structured logs, Prometheus metrics, health probes (§41-44).

`install_observability` is the single call `create_app` makes to turn the pieces in this package
into a live surface:

- it configures the root logger once with the secret-safe JSON formatter (§41);
- it creates the application's own `AppMetrics` bundle and stores it on `app.state` — instance
  state, never a process-global, so two `create_app()`s in one test process keep separate counts;
- it adds the `ObservabilityMiddleware` (correlation id + HTTP metrics, §41-42);
- it mounts `/health/live`, `/health/ready` and `/metrics` at the root (§42-43).

Everything an operator scrapes or probes therefore comes from one wiring point, and an application
that never calls it simply has none of the surface — the pieces carry no import-time side effects.
"""
from __future__ import annotations

from fastapi import FastAPI

from backend.app.observability.app_metrics import AppMetrics
from backend.app.observability.collectors import MetricsCollector
from backend.app.observability.context import (
    bind_correlation_id,
    get_correlation_id,
    new_correlation_id,
)
from backend.app.observability.health import METRICS_ATTRIBUTE
from backend.app.observability.health import router as health_router
from backend.app.observability.logging import configure_logging
from backend.app.observability.middleware import ObservabilityMiddleware

__all__ = [
    "AppMetrics",
    "MetricsCollector",
    "ObservabilityMiddleware",
    "bind_correlation_id",
    "configure_logging",
    "get_correlation_id",
    "install_observability",
    "new_correlation_id",
]


def install_observability(app: FastAPI, *, configure_logs: bool = True,
                          log_level: str = "INFO") -> AppMetrics:
    """Wire logging, metrics, the correlation-id/metrics middleware and the health routes (§41-44).

    Returns the `AppMetrics` bundle it created and stored on `app.state.metrics`, so a caller (or a
    test) can read the counters directly. `configure_logs` is on by default — production wants the
    JSON formatter installed — and left overridable so a host that manages logging itself, or a test
    that does not want the root logger touched, can opt out. Logging config is idempotent, so even
    when several applications are built in one process only the first installs a handler.
    """
    if configure_logs:
        configure_logging(level=log_level)
    metrics = AppMetrics()
    setattr(app.state, METRICS_ATTRIBUTE, metrics)
    app.add_middleware(ObservabilityMiddleware)
    app.include_router(health_router)
    return metrics
