"""A tiny, dependency-free metrics registry that renders the Prometheus text format (§42).

Phase 16 §42 asks for "metrics suitable for Prometheus-compatible scraping", not for a particular
client library. This project builds its infrastructure rather than pulling a heavy dependency that
carries process-global state (a global default registry fights the "a fresh app per `create_app`"
isolation the API tests rely on). So this module is the whole metrics layer: three primitives —
`Counter`, `Gauge`, `Histogram` — collected in a `MetricRegistry` an application owns as instance
state, and `render_prometheus`, which serialises the registry to the 0.0.4 text exposition format a
Prometheus scraper (or any compatible agent) reads.

Two rules the design enforces so a metric can never become a liability:

- **Labels are declared, fixed, and low-cardinality.** A metric names its label keys once; a sample
  must supply exactly those keys. There is no path to label by a user id or an opportunity id — the
  §42 warning against high-cardinality labels is structural, not a convention to remember.
- **Concurrency-safe.** Every mutation takes a lock, so the ASGI middleware incrementing a counter
  on one request cannot race the `/metrics` render reading it on another.
"""
from __future__ import annotations

import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# The default latency buckets (seconds), the Prometheus client's own defaults: fine-grained under a
# second where an HTTP handler lives, thinning out to 10s so a pathological request still lands in a
# bucket rather than only in `+Inf`.
DEFAULT_DURATION_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _format_value(value: float) -> str:
    """Render a sample value as the exposition format wants it (`+Inf`/`-Inf`, else a float)."""
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if math.isnan(value):
        return "NaN"
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def _escape_label_value(value: str) -> str:
    """Escape a label value per the exposition format: backslash, double-quote and newline."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(label_names: Sequence[str], label_values: Sequence[str],
                   extra: tuple[str, str] | None = None) -> str:
    """`{k="v",...}` for a sample, or `""` when none — with an optional extra pair (`le`)."""
    pairs = [(name, value) for name, value in zip(label_names, label_values, strict=True)]
    if extra is not None:
        pairs.append(extra)
    if not pairs:
        return ""
    body = ",".join(f'{name}="{_escape_label_value(value)}"' for name, value in pairs)
    return "{" + body + "}"


def _key(metric_name: str, label_names: Sequence[str],
         labels: Mapping[str, str]) -> tuple[str, ...]:
    """Validate a sample's labels against the metric's declared keys and return them in order.

    Exactly the declared keys, no more and no fewer: a typo (`stauts=`) or a stray high-cardinality
    label (`user_id=`) is a programming error caught here, not a silently distinct time series that
    quietly explodes the scrape.
    """
    if set(labels) != set(label_names):
        raise ValueError(
            f"metric {metric_name!r} expects labels {sorted(label_names)}, got {sorted(labels)}")
    return tuple(labels[name] for name in label_names)


class _Metric:
    """Shared state for a named, labelled metric: its identity and a lock over its samples."""

    kind: str

    def __init__(self, name: str, help_text: str, label_names: Sequence[str] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names: tuple[str, ...] = tuple(label_names)
        self._lock = threading.Lock()

    def _render_header(self) -> list[str]:
        return [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} {self.kind}"]


class Counter(_Metric):
    """A monotonically increasing total — request counts, job outcomes, denials (§42)."""

    kind = "counter"

    def __init__(self, name: str, help_text: str, label_names: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, label_names)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, /, **labels: str) -> None:
        if amount < 0:
            raise ValueError(f"counter {self.name!r} cannot decrease")
        key = _key(self.name, self.label_names, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        key = _key(self.name, self.label_names, labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> list[str]:
        with self._lock:
            samples = sorted(self._values.items())
        lines = self._render_header()
        for label_values, value in samples:
            labels = _render_labels(self.label_names, label_values)
            lines.append(f"{self.name}{labels} {_format_value(value)}")
        return lines


class Gauge(_Metric):
    """A value that goes up and down — in-flight requests, queue depth, `db_up` (§42)."""

    kind = "gauge"

    def __init__(self, name: str, help_text: str, label_names: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, label_names)
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, /, **labels: str) -> None:
        key = _key(self.name, self.label_names, labels)
        with self._lock:
            self._values[key] = value

    def inc(self, amount: float = 1.0, /, **labels: str) -> None:
        key = _key(self.name, self.label_names, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, /, **labels: str) -> None:
        self.inc(-amount, **labels)

    def value(self, **labels: str) -> float:
        key = _key(self.name, self.label_names, labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> list[str]:
        with self._lock:
            samples = sorted(self._values.items())
        lines = self._render_header()
        for label_values, value in samples:
            labels = _render_labels(self.label_names, label_values)
            lines.append(f"{self.name}{labels} {_format_value(value)}")
        return lines


@dataclass
class _HistogramCell:
    """One label-set's accumulated observations: cumulative bucket counts, the sum, the count."""

    bucket_counts: list[float]
    sum: float = 0.0
    count: float = 0.0


class Histogram(_Metric):
    """A distribution over fixed buckets — request/LLM latency (§42).

    Buckets are cumulative (`le`, "less than or equal"), and every histogram carries an implicit
    `+Inf` bucket equal to the total count, so `_bucket{le="+Inf"} == _count` always holds — the
    invariant a Prometheus `histogram_quantile` relies on.
    """

    kind = "histogram"

    def __init__(self, name: str, help_text: str, label_names: Sequence[str] = (),
                 buckets: Sequence[float] = DEFAULT_DURATION_BUCKETS) -> None:
        super().__init__(name, help_text, label_names)
        self._upper_bounds: tuple[float, ...] = tuple(sorted(buckets))
        self._cells: dict[tuple[str, ...], _HistogramCell] = {}

    def observe(self, value: float, /, **labels: str) -> None:
        key = _key(self.name, self.label_names, labels)
        with self._lock:
            cell = self._cells.get(key)
            if cell is None:
                cell = _HistogramCell(bucket_counts=[0.0] * len(self._upper_bounds))
                self._cells[key] = cell
            cell.sum += value
            cell.count += 1.0
            for index, bound in enumerate(self._upper_bounds):
                if value <= bound:
                    cell.bucket_counts[index] += 1.0

    def render(self) -> list[str]:
        with self._lock:
            cells = sorted(self._cells.items())
        lines = self._render_header()
        for label_values, cell in cells:
            for bound, cumulative in zip(self._upper_bounds, cell.bucket_counts, strict=True):
                le = ("le", _format_value(bound))
                labels = _render_labels(self.label_names, label_values, le)
                lines.append(f"{self.name}_bucket{labels} {_format_value(cumulative)}")
            inf = _render_labels(self.label_names, label_values, ("le", "+Inf"))
            lines.append(f"{self.name}_bucket{inf} {_format_value(cell.count)}")
            plain = _render_labels(self.label_names, label_values)
            lines.append(f"{self.name}_sum{plain} {_format_value(cell.sum)}")
            lines.append(f"{self.name}_count{plain} {_format_value(cell.count)}")
        return lines


AnyMetric = Counter | Gauge | Histogram

# The content type a Prometheus scraper expects for the text exposition format (version 0.0.4).
PROMETHEUS_CONTENT_TYPE: str = "text/plain; version=0.0.4; charset=utf-8"


@dataclass
class MetricRegistry:
    """An application's owned set of metrics — instance state, never a process-global (§42).

    Held on `app.state` so two `create_app()` calls in one test process each get their own counts;
    a metric is registered once at construction and read on every scrape. Registration refuses a
    duplicate name so a copy-paste that shadows an existing series fails loudly at wiring time
    rather than silently double-counting.
    """

    _metrics: dict[str, AnyMetric] = field(default_factory=dict)

    def register(self, metric: AnyMetric) -> AnyMetric:
        if metric.name in self._metrics:
            raise ValueError(f"metric {metric.name!r} is already registered")
        self._metrics[metric.name] = metric
        return metric

    def counter(self, name: str, help_text: str, label_names: Sequence[str] = ()) -> Counter:
        counter = Counter(name, help_text, label_names)
        self.register(counter)
        return counter

    def gauge(self, name: str, help_text: str, label_names: Sequence[str] = ()) -> Gauge:
        gauge = Gauge(name, help_text, label_names)
        self.register(gauge)
        return gauge

    def histogram(self, name: str, help_text: str, label_names: Sequence[str] = (),
                  buckets: Sequence[float] = DEFAULT_DURATION_BUCKETS) -> Histogram:
        histogram = Histogram(name, help_text, label_names, buckets)
        self.register(histogram)
        return histogram

    def render(self) -> str:
        return render_prometheus(self)

    @property
    def metrics(self) -> Mapping[str, AnyMetric]:
        return self._metrics


def render_prometheus(registry: MetricRegistry) -> str:
    """Serialise every registered metric to the Prometheus text exposition format (§42).

    One `# HELP`/`# TYPE` block per metric, in registration order, ending with a trailing newline —
    the shape a scraper parses. A metric with no observations yet renders only its header, which is
    valid and tells the scraper the series exists.
    """
    blocks: list[str] = []
    for metric in registry.metrics.values():
        blocks.extend(metric.render())
    return "\n".join(blocks) + "\n"

