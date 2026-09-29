"""Rate-store-specific code — the Redis-backed limiter behind the `RateLimiter` seam (§49).

Everything that knows the rate store is Redis lives in this one package, behind the same
`RateLimiter` protocol the API dependency depends on — the same boundary
`backend/app/infrastructure/tasks` guards for the queue. Swapping Redis for another counter store,
or dropping it to run the in-memory limiter, is a change confined to here.

The Redis client is imported lazily (only when the limiter first talks to the store), so importing
this package, and running the default test suite, needs neither the `redis` package installed nor a
server reachable.
"""
from __future__ import annotations

from backend.app.infrastructure.ratelimit.redis_limiter import RedisRateLimiter

__all__ = ["RedisRateLimiter"]
