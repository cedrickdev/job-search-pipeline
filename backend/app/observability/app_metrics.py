"""The application's declared metric set — one bundle an app owns on its state (§42).

`AppMetrics` is the closed list of series this deployment exposes, registered once against a
per-application `MetricRegistry` (never a process-global, so two `create_app()`s in one test
process keep their own counts). Two families live here:

- **Push metrics** the HTTP middleware writes on every request — request count, latency and the
  in-flight gauge — labelled by method, the *route template* (low cardinality by construction) and
  the response status. A quota denial (402) and a billing-webhook rejection (a non-2xx on the
  webhook route) fall out of `http_requests_total` without a bespoke counter, so §42's "quota
  denials" and "billing webhook failures" are derivable series rather than a second source of truth
  (docs/OBSERVABILITY.md gives the exact queries).
- **Pull gauges** the `MetricsCollector` sets at scrape time from the durable tables — `db_up`, the
  queue's shape (`task_runs` by lane and status), stale leases, and LLM run counts by status. These
  reuse the Phase 8 queue table and the Phase 11 telemetry table rather than duplicating them
  (acceptance §9), so worker/queue/LLM health is read straight off the source of truth.

The label keys are declared here once; a sample that supplies a different set is a programming
error the registry refuses at write time (backend/app/observability/metrics.py), which is what keeps
a stray high-cardinality label (a user id, an opportunity id) from ever entering a series (§42).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.app.observability.metrics import Counter, Gauge, Histogram, MetricRegistry


@dataclass
class AppMetrics:
    """Every metric the API process exposes, bound to one registry (§42).

    Constructed by `install_observability` and stored on `app.state`; the middleware writes the HTTP
    series and the collector sets the gauges. A frozen shape — the fields are the whole vocabulary,
    so adding a series is a deliberate edit here, never an ad-hoc `registry.counter(...)` scattered
    through a request handler.
    """

    registry: MetricRegistry = field(default_factory=MetricRegistry)

    http_requests_total: Counter = field(init=False)
    http_request_duration_seconds: Histogram = field(init=False)
    http_requests_in_flight: Gauge = field(init=False)
    db_up: Gauge = field(init=False)
    task_runs: Gauge = field(init=False)
    task_stale_leases: Gauge = field(init=False)
    llm_runs: Gauge = field(init=False)

    def __post_init__(self) -> None:
        self.http_requests_total = self.registry.counter(
            "http_requests_total",
            "Total HTTP requests handled, by method, route template and response status.",
            ("method", "route", "status"))
        self.http_request_duration_seconds = self.registry.histogram(
            "http_request_duration_seconds",
            "HTTP request handling latency in seconds, by method and route template.",
            ("method", "route"))
        self.http_requests_in_flight = self.registry.gauge(
            "http_requests_in_flight",
            "HTTP requests currently being handled.")
        self.db_up = self.registry.gauge(
            "db_up",
            "1 when the primary database answered a liveness ping on the last scrape, else 0.")
        self.task_runs = self.registry.gauge(
            "task_runs",
            "Background task runs currently in each state, by lane and status.",
            ("lane", "status"))
        self.task_stale_leases = self.registry.gauge(
            "task_stale_leases",
            "Running task runs whose worker lease has lapsed (a worker-health signal).")
        self.llm_runs = self.registry.gauge(
            "llm_runs",
            "LLM telemetry runs recorded, by terminal (or in-flight) status.",
            ("status",))

    def render(self) -> str:
        """The registry's Prometheus text exposition — what the `/metrics` route returns (§42)."""
        return self.registry.render()
