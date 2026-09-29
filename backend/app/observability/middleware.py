"""The ASGI middleware that gives every request a correlation id and a metric (§41-42).

One pure-ASGI middleware, not a `BaseHTTPMiddleware`, for a reason that bites in this codebase: the
career-chat turn streams its response, and `BaseHTTPMiddleware` buffers the body, which would break
Server-Sent Events. Wrapping `send` directly leaves the stream untouched.

It does three things around the app it wraps, for HTTP requests only (a websocket or the lifespan
passes straight through):

- **Correlation id in and out (§41).** It honours an inbound `X-Correlation-ID` or `X-Request-ID`
  (a gateway or a client already assigned one) and otherwise mints a fresh opaque id, binds it to
  the context so every log line the request emits carries it, and echoes it on the response so a
  caller can quote it in a bug report. The id is not a secret and never carries user data, so
  putting it on the wire leaks nothing.
- **HTTP metrics (§42).** An in-flight gauge is raised for the duration, and on completion the
  request count and latency histogram are recorded against the *matched route template*
  (`/api/v2/applications/{application_id}`), never the raw path — so an id in a URL cannot explode
  the label set (§42 forbids high-cardinality labels). A quota denial (402) and a webhook rejection
  (a non-2xx on the webhook route) are just cells of `http_requests_total`, derivable without a
  bespoke counter.
- **A structured access line.** One INFO log per request carrying method, route, status and
  duration — the JSON formatter attaches the correlation id from the context, so the access line
  and every line the handler logged share one id.

The route template is read from `scope["route"]` *after* the inner app has run, because that is
when Starlette's router has matched it onto the scope. A request that matched no route (a 404 that
the SPA fallback later turns into `index.html`) is labelled `__unmatched__` — one low-cardinality
bucket, not the unbounded set of every bad path a scanner probes.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from backend.app.observability.app_metrics import AppMetrics
from backend.app.observability.context import bind_correlation_id, new_correlation_id

# The ASGI callable shapes, named so the wrapper reads as intent rather than nested generics.
Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# The inbound headers a correlation id may arrive on, in precedence order: a dedicated correlation
# header first, then the more common request-id a gateway sets. Lower-cased, as ASGI delivers them.
_CORRELATION_HEADERS: tuple[bytes, ...] = (b"x-correlation-id", b"x-request-id")
_CORRELATION_RESPONSE_HEADER: tuple[bytes, bytes] = (b"x-correlation-id", b"")

# The label a request that matched no route carries — one bucket, so a scanner probing a thousand
# bad paths cannot mint a thousand series (§42).
_UNMATCHED_ROUTE: str = "__unmatched__"

_access_logger = logging.getLogger("backend.app.observability.access")


def _inbound_correlation_id(scope: Scope) -> str:
    """The correlation id from an inbound header, or a fresh one when none was supplied."""
    headers = dict(scope.get("headers") or ())
    for name in _CORRELATION_HEADERS:
        raw = headers.get(name)
        if raw:
            # A header value is opaque bytes; decode defensively and cap it so a hostile client
            # cannot smuggle a megabyte (or a newline) into every log line through the id.
            candidate: str = raw.decode("latin-1", "replace").strip()[:128]
            if candidate:
                return candidate
    return new_correlation_id()


def _route_template(scope: Scope) -> str:
    """The matched route's template, or the unmatched bucket — the low-cardinality metric label."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else _UNMATCHED_ROUTE


class ObservabilityMiddleware:
    """Wrap an ASGI app: bind a correlation id, record one HTTP metric per request (§41-42)."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        correlation_id = _inbound_correlation_id(scope)
        metrics: AppMetrics | None = getattr(
            getattr(scope.get("app"), "state", None), "metrics", None)
        method = scope.get("method", "GET")
        status_holder: dict[str, int] = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message.get("status", 0))
                header = (_CORRELATION_RESPONSE_HEADER[0], correlation_id.encode("latin-1"))
                message["headers"] = [*message.get("headers", []), header]
            await send(message)

        with bind_correlation_id(correlation_id):
            if metrics is not None:
                metrics.http_requests_in_flight.inc()
            started = time.perf_counter()
            try:
                await self._app(scope, receive, send_wrapper)
            finally:
                elapsed = time.perf_counter() - started
                route = _route_template(scope)
                status = str(status_holder["status"])
                if metrics is not None:
                    metrics.http_requests_in_flight.dec()
                    metrics.http_requests_total.inc(
                        method=method, route=route, status=status)
                    metrics.http_request_duration_seconds.observe(
                        elapsed, method=method, route=route)
                _access_logger.info(
                    "http_request", extra={
                        "http_method": method, "route": route,
                        "status": status_holder["status"],
                        "duration_ms": round(elapsed * 1000, 3)})
