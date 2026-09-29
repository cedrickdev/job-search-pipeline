"""Horizontal-safe rate limiting for abuse-prone endpoints (Phase 16 §49-52).

The whole layer is one algorithm — a **fixed window** — expressed once here and then implemented
twice: against Redis (the horizontal-safe production store, `backend/app/infrastructure/ratelimit`)
and in memory (single-process, for tests and local dev). Both count events into a bucket keyed by
`namespace:identity:window_start`, where `window_start = epoch - (epoch % window_seconds)` snaps the
clock to the start of the current window. The N+1th event in a window is refused; the caller is told
how many seconds remain until the window resets. Keying the bucket on `window_start` is what makes
the counter safe to share across processes: two web workers incrementing the same Redis key see one
running total, so a limit is a limit no matter which worker a request lands on (§49).

This module is a leaf — the rule, the category, the decision and the two pure limiters, nothing
about HTTP. The FastAPI dependency that turns a refusal into a 429 lives in the API layer
(`backend/app/api/dependencies.py`), and the settings that size each window live in
`backend/app/core/settings.py`. The Redis client stays behind the `RateLimiter` protocol in an
infrastructure adapter, so the default test suite never imports `redis` (the same discipline the
task queue's `RedisNotifyingDispatcher` follows).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class RateLimitCategory(str, Enum):
    """The abuse-prone endpoint families §49 names, each sized by its own rule.

    A category is both the settings field that sizes it and the Redis key namespace, so two
    categories never share a bucket — exhausting document generation must not lock out sign-in.
    Application submission is deliberately absent: it already carries Phase 12 policy limits, and
    §49 says to preserve those rather than add a second, coarser brake over them.
    """

    REGISTER = "register"
    LOGIN = "login"
    REAUTH = "reauth"
    CHAT = "chat"
    DOCUMENT = "document"
    EXPORT = "export"
    CHECKOUT = "checkout"


class RateLimitRule(BaseModel):
    """How many events fit in one window, and how long that window is.

    Frozen and closed like every settings value. Both bounds are `ge=1`: a zero `max_events` would
    refuse the very first request (a foot-gun disguised as a limit), and a zero `window_seconds`
    would divide by zero when snapping the clock. A deployment tunes these per category
    (`RateLimitSettings`), never in the code.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_events: int = Field(ge=1)
    window_seconds: int = Field(ge=1)


@dataclass(frozen=True)
class RateLimitDecision:
    """The verdict for one event: whether it is allowed, and when to retry if not.

    `retry_after_seconds` is meaningful only when `allowed` is false — the seconds until the
    current window resets, which the 429 handler puts in both the `Retry-After` header and the
    body. It carries nothing about which rule tripped or which identity was counted (§52).
    """

    allowed: bool
    retry_after_seconds: int


def window_start(now: datetime, window_seconds: int) -> int:
    """The epoch second the current fixed window began — the clock snapped down to the window."""
    epoch = int(now.timestamp())
    return epoch - (epoch % window_seconds)


def decide(count: int, rule: RateLimitRule, now: datetime) -> RateLimitDecision:
    """Turn a running count for the current window into an allow/deny verdict.

    The event that took the count to exactly `max_events` is still allowed; the next one is not.
    A refusal reports the seconds until this window ends, floored at 1 so a client never sees a
    `Retry-After: 0` telling it to retry immediately into the same closed window.
    """
    if count <= rule.max_events:
        return RateLimitDecision(allowed=True, retry_after_seconds=0)
    reset_at = window_start(now, rule.window_seconds) + rule.window_seconds
    return RateLimitDecision(
        allowed=False, retry_after_seconds=max(1, reset_at - int(now.timestamp())))


class RateLimiter(Protocol):
    """Count one event against a rule and return the verdict — the seam over Redis or memory.

    A protocol so the service and the request path depend on the behaviour, not the store: the
    production limiter talks to Redis (horizontal-safe, fail-open), the test limiter counts in a
    dict, and neither the dependency nor a test knows which it holds.
    """

    async def check(self, *, namespace: str, identity: str, rule: RateLimitRule,
                    now: datetime) -> RateLimitDecision:
        """Register one event for `identity` under `namespace` and return whether it is allowed."""
        ...


class InMemoryRateLimiter:
    """A single-process fixed-window limiter — the test and local-dev implementation.

    Mirrors the Redis algorithm exactly (same key, same window snap), so a test proves the
    behaviour the production limiter will show. Not horizontal-safe by construction — a dict lives
    in one process — which is precisely why production uses Redis; here there is only one process,
    so a dict is the honest store. Expired windows are pruned on every check, so a long-lived
    process (a dev server) does not accumulate a bucket per elapsed window forever.
    """

    def __init__(self) -> None:
        # key -> (count in the window, epoch second the window expires)
        self._windows: dict[str, tuple[int, int]] = {}

    async def check(self, *, namespace: str, identity: str, rule: RateLimitRule,
                    now: datetime) -> RateLimitDecision:
        epoch = int(now.timestamp())
        self._prune(epoch)
        start = epoch - (epoch % rule.window_seconds)
        key = f"{namespace}:{identity}:{start}"
        count = self._windows.get(key, (0, 0))[0] + 1
        self._windows[key] = (count, start + rule.window_seconds)
        return decide(count, rule, now)

    def _prune(self, epoch: int) -> None:
        """Drop every window that has already ended, so the dict cannot grow without bound."""
        expired = [key for key, (_, expires_at) in self._windows.items() if expires_at <= epoch]
        for key in expired:
            del self._windows[key]
