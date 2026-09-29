"""The correlation id that threads one request (or one task) through every log line (§41).

A correlation id is the single value that lets an operator pull every structured log for one
request out of a stream that interleaves thousands. It lives in a `ContextVar`, so it follows the
logical flow of control — across `await` points, into the service layer, down to a repository —
without being passed as an argument through code that has no other reason to know about it. The
HTTP middleware sets it per request (from an inbound `X-Correlation-ID`/`X-Request-ID` header when a
gateway already assigned one, or a fresh id otherwise); a worker sets it per task. The JSON log
formatter reads it. Nothing else needs to.

It is deliberately *not* a secret and never carries user data — it is an opaque handle, so putting
it in a response header and every log line leaks nothing (§41).
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from uuid import uuid4

# Unset rather than empty-string so a log record can distinguish "no request in flight" (a
# background thread, startup) from "a request whose id is the empty string" — the latter cannot
# happen, but the None sentinel keeps the formatter's branch honest.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_correlation_id() -> str:
    """A fresh opaque correlation id — a hex UUID4, compact and not a secret."""
    return uuid4().hex


def get_correlation_id() -> str | None:
    """The correlation id bound to the current context, or `None` when none is in flight."""
    return _correlation_id.get()


def set_correlation_id(correlation_id: str) -> Token[str | None]:
    """Bind `correlation_id` to the current context, returning the token that restores the prior."""
    return _correlation_id.set(correlation_id)


def reset_correlation_id(token: Token[str | None]) -> None:
    """Restore the correlation id the matching `set_correlation_id` replaced."""
    _correlation_id.reset(token)


@contextmanager
def bind_correlation_id(correlation_id: str) -> Iterator[str]:
    """Bind a correlation id for the duration of the block, restoring the previous on exit.

    The symmetric form the worker uses: it wraps one task's processing so every log line the
    handler emits carries the task's id, and the binding is unwound the instant the task settles so
    it can never bleed into the next one.
    """
    token = _correlation_id.set(correlation_id)
    try:
        yield correlation_id
    finally:
        _correlation_id.reset(token)
