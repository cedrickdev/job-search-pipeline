"""The dependency-free metrics primitives, asserted in isolation (§42).

`Counter`, `Gauge` and `Histogram` render the Prometheus 0.0.4 text exposition format, and the
`MetricRegistry`/`AppMetrics` bundle owns them as instance state. These tests pin the two rules the
design rests on — labels are declared and validated (so a high-cardinality label is a loud error,
never a silent series), and the render is the exact shape a scraper parses — plus the histogram
invariant `histogram_quantile` relies on: `_bucket{le="+Inf"} == _count`.
"""
import math

import pytest

from backend.app.observability.app_metrics import AppMetrics
from backend.app.observability.metrics import (
    DEFAULT_DURATION_BUCKETS,
    PROMETHEUS_CONTENT_TYPE,
    Counter,
    Gauge,
    Histogram,
    MetricRegistry,
    _format_value,
    render_prometheus,
)


def test_a_counter_sums_and_renders_one_line_per_label_set() -> None:
    counter = Counter("http_requests_total", "help", ("method", "status"))
    counter.inc(method="GET", status="200")
    counter.inc(2.0, method="GET", status="200")
    counter.inc(method="POST", status="402")

    assert counter.value(method="GET", status="200") == 3.0
    lines = counter.render()
    assert '# TYPE http_requests_total counter' in lines
    assert 'http_requests_total{method="GET",status="200"} 3' in lines
    assert 'http_requests_total{method="POST",status="402"} 1' in lines


def test_a_counter_refuses_to_decrease() -> None:
    counter = Counter("c", "help")
    with pytest.raises(ValueError, match="cannot decrease"):
        counter.inc(-1.0)


def test_a_sample_with_an_unknown_label_is_a_loud_error_not_a_new_series() -> None:
    # The §42 high-cardinality guard, structural: a stray `user_id` (or a typo) cannot mint a series.
    counter = Counter("http_requests_total", "help", ("method", "status"))
    with pytest.raises(ValueError, match="expects labels"):
        counter.inc(method="GET", status="200", user_id="42")
    with pytest.raises(ValueError, match="expects labels"):
        counter.inc(method="GET")


def test_a_gauge_goes_up_and_down_and_can_be_set() -> None:
    gauge = Gauge("in_flight", "help")
    gauge.inc()
    gauge.inc()
    gauge.dec()
    assert gauge.value() == 1.0
    gauge.set(0.0)
    assert gauge.value() == 0.0


def test_a_histogram_buckets_are_cumulative_and_inf_equals_count() -> None:
    histogram = Histogram("d", "help", ("route",))
    histogram.observe(0.03, route="/x")   # lands in le="0.05" and every larger bucket
    histogram.observe(3.0, route="/x")    # lands only from le="5.0" up
    lines = histogram.render()

    # le="0.025" is below both observations; le="0.05" holds the first; le="+Inf" holds both.
    assert 'd_bucket{route="/x",le="0.025"} 0' in lines
    assert 'd_bucket{route="/x",le="0.05"} 1' in lines
    assert 'd_bucket{route="/x",le="+Inf"} 2' in lines
    assert 'd_count{route="/x"} 2' in lines
    assert 'd_sum{route="/x"} 3.03' in lines


def test_the_inf_bucket_always_equals_the_count() -> None:
    histogram = Histogram("d", "help", buckets=(0.1, 1.0))
    for value in (0.05, 0.2, 5.0, 5.0):
        histogram.observe(value)
    lines = histogram.render()
    inf = next(line for line in lines if 'le="+Inf"' in line)
    count = next(line for line in lines if line.startswith("d_count"))
    assert inf.rsplit(" ", 1)[-1] == count.rsplit(" ", 1)[-1] == "4"


def test_the_registry_refuses_a_duplicate_name() -> None:
    registry = MetricRegistry()
    registry.counter("dup", "help")
    with pytest.raises(ValueError, match="already registered"):
        registry.gauge("dup", "help")


def test_render_is_registration_ordered_and_ends_with_a_newline() -> None:
    registry = MetricRegistry()
    registry.counter("a_total", "first")
    registry.gauge("b_gauge", "second")
    text = render_prometheus(registry)
    assert text.endswith("\n")
    assert text.index("a_total") < text.index("b_gauge")
    # A metric with no observations still renders its header, so a scraper knows the series exists.
    assert "# TYPE a_total counter" in text
    assert "# TYPE b_gauge gauge" in text


def test_a_label_value_with_a_quote_or_newline_is_escaped() -> None:
    counter = Counter("c", "help", ("route",))
    counter.inc(route='a"b\nc')
    line = next(line for line in counter.render() if line.startswith("c{"))
    assert 'route="a\\"b\\nc"' in line


def test_format_value_renders_integers_floats_and_infinities() -> None:
    assert _format_value(3.0) == "3"
    assert _format_value(0.03) == "0.03"
    assert _format_value(math.inf) == "+Inf"
    assert _format_value(-math.inf) == "-Inf"


def test_the_content_type_is_the_0_0_4_text_exposition_format() -> None:
    assert PROMETHEUS_CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"


def test_app_metrics_declares_the_whole_vocabulary_once() -> None:
    # The closed set of series the process exposes; adding one is a deliberate edit to AppMetrics.
    metrics = AppMetrics()
    names = set(metrics.registry.metrics)
    assert names == {
        "http_requests_total", "http_request_duration_seconds", "http_requests_in_flight",
        "db_up", "task_runs", "task_stale_leases", "llm_runs"}
    # Two AppMetrics keep independent registries — never a process-global (test isolation, §42).
    assert metrics.registry is not AppMetrics().registry
    assert DEFAULT_DURATION_BUCKETS[0] > 0
