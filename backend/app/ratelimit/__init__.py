"""Rate limiting for abuse-prone endpoints (Phase 16 §49-52).

The fixed-window algorithm and its two pure implementations (`InMemoryRateLimiter` for tests and
local dev, and the `RateLimiter` protocol the production Redis adapter satisfies) live in
`limiter`. The Redis-backed adapter lives in `backend/app/infrastructure/ratelimit`, lazily
imported so the default suite never touches Redis.
"""
from __future__ import annotations

from backend.app.ratelimit.limiter import (
    InMemoryRateLimiter,
    RateLimitCategory,
    RateLimitDecision,
    RateLimiter,
    RateLimitRule,
    decide,
    window_start,
)

__all__ = [
    "InMemoryRateLimiter",
    "RateLimitCategory",
    "RateLimitDecision",
    "RateLimitRule",
    "RateLimiter",
    "decide",
    "window_start",
]
