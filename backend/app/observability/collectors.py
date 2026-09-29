"""Pull-based gauges, read from the durable tables at scrape time (§42).

A Prometheus scrape is a *pull*: the `/metrics` route asks this collector to refresh the gauges
that describe state the middleware never sees — the database's liveness, the queue's shape, and the
LLM's run outcomes. Each is read straight off the source of truth (the Phase 8 `task_runs` table,
the Phase 11 `llm_runs` table) rather than kept in a parallel counter that could drift, which is
also what keeps the LLM numbers a *view* of Phase 11 telemetry rather than a duplicate of it
(acceptance §9).

Two properties matter:

- **Zero-fill, so a series never vanishes.** Every declared `(lane, status)` cell and every LLM
  status is set — to its count, or to zero when the table holds none — so a scraper sees
  `task_runs{status="DEAD_LETTERED"} 0` rather than a gap it cannot tell from "the exporter is
  gone". A `histogram_quantile` or an `increase()` needs the series to exist continuously.
- **Database-down is a reading, not a crash.** When the `/metrics` route cannot reach the database
  it calls `mark_database_unavailable()` and renders anyway: `db_up 0` and the last-known gauges,
  plus every HTTP series the middleware already holds in memory. A metrics endpoint that 500s when
  the database is down blinds the very alert that needs it (§43-44).
"""
from __future__ import annotations

from datetime import datetime

from backend.app.domain.task import TaskLane, TaskStatus
from backend.app.llm.telemetry import LLMRunStatus
from backend.app.observability.app_metrics import AppMetrics
from backend.app.repositories.contracts import LLMRunRepository, TaskRunRepository


class MetricsCollector:
    """Refreshes the pull gauges of one `AppMetrics` from the durable tables (§42).

    Holds only the metric bundle; the repositories and the clock are handed to `collect` per scrape,
    so the collector carries no session and no request state — it is a pure mapping from rows to
    gauge values that a test drives over fakes exactly as the route drives it over PostgreSQL.
    """

    def __init__(self, metrics: AppMetrics) -> None:
        self._metrics = metrics

    def mark_database_unavailable(self) -> None:
        """Record that the database did not answer this scrape's ping — `db_up 0` (§43).

        The queue and LLM gauges cannot be read without a session, so they keep their last-known
        values; the render still succeeds and still carries every in-memory HTTP series, which is
        what an operator's "database unavailable" alert reads.
        """
        self._metrics.db_up.set(0.0)

    async def collect(self, *, tasks: TaskRunRepository, llm: LLMRunRepository,
                      now: datetime) -> None:
        """Refresh every pull gauge from the durable tables — the healthy-scrape path (§42).

        `db_up` is 1 (this ran, so the session answered), the queue gauges are the `(lane, status)`
        snapshot plus the stale-lease count, and the LLM gauge is the run count per status. Reads
        are aggregate and operator-scope — one `GROUP BY` each, no owned row, no id label.
        """
        self._metrics.db_up.set(1.0)

        for lane in TaskLane:
            for status in TaskStatus:
                self._metrics.task_runs.set(0.0, lane=lane.value, status=status.value)
        for cell in await tasks.status_counts():
            self._metrics.task_runs.set(
                float(cell.total), lane=cell.lane.value, status=cell.status.value)
        self._metrics.task_stale_leases.set(float(await tasks.count_stale_leases(now)))

        for run_status in LLMRunStatus:
            self._metrics.llm_runs.set(0.0, status=run_status.value)
        for run_cell in await llm.status_counts():
            self._metrics.llm_runs.set(float(run_cell.total), status=run_cell.status.value)
