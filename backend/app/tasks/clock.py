"""`Clock` — the one way background execution reads the current time.

The services this package drives take `now`/`as_of` as an argument rather than reading the clock
themselves, so what is "expired" or "due" is a function of inputs and a test can freeze time. The
dispatcher and the worker are long-lived, though: they must read the clock repeatedly as they run,
so they hold a `Clock` — a zero-argument callable returning a timezone-aware UTC instant — that a
test overrides with a controllable stub. `utc_now` is the production one.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

# A source of the current instant. Injected into the dispatcher and worker so a test can drive
# time deterministically; production passes `utc_now`.
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """The current instant as a timezone-aware UTC datetime — the production `Clock`."""
    return datetime.now(UTC)
