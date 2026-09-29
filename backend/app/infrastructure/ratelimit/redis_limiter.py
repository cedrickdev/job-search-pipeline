"""`RedisRateLimiter` — the horizontal-safe fixed-window limiter (§49).

The one place the rate-limit layer speaks Redis. It satisfies the `RateLimiter` protocol the API
dependency depends on, so nothing upstream changes when this is the limiter rather than the
in-memory one. The window counter is a Redis key `ratelimit:<namespace>:<identity>:<window_start>`:
`INCR` returns the running total (shared across every web worker pointed at the same Redis, which
is what makes the limit horizontal-safe, §49), and an `EXPIRE` on the first event in a window lets
the key — and the count — reset itself when the window ends.

**Fail-open on a Redis fault.** If Redis is unreachable the limiter allows the request and logs a
warning; it never turns an availability blip in the rate store into an outage of sign-in or
generation. This is safe because rate limiting here is *additive* (§50): the Phase 4 DB-backed
account lockout still bounds password guessing per account with no Redis in the path, so a
fail-open rate limiter cannot reopen the protection §50 says to keep. The warning names only the
error type — never the URL (it can carry a password) and never the identity being counted (§41).

`redis.asyncio` is imported lazily inside `_client`, so importing this module needs neither the
`redis` package installed nor a server reachable — the default test suite touches no Redis (the
project's testing rule), and a deployment installs the `worker` extra to get the client. This
mirrors the task queue's `RedisNotifyingDispatcher` exactly.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from backend.app.ratelimit.limiter import RateLimitDecision, RateLimitRule, decide

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from redis.asyncio import Redis

logger = logging.getLogger("jobsearch.ratelimit.redis")

# The key prefix every window counter lives under, so a rate-limit key is never mistaken for a
# queue wake or any other value sharing the Redis instance.
_KEY_PREFIX = "ratelimit:"


class RedisRateLimiter:
    """A `RateLimiter` backed by Redis `INCR`/`EXPIRE` — horizontal-safe, fail-open (§49).

    Holds a lazily-connected client. The Redis URL is kept off the `repr` because it can carry a
    password (§41: never log a credential), exactly as `RedisNotifyingDispatcher` does.
    """

    def __init__(self, *, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis: Redis | None = None

    def __repr__(self) -> str:  # never leak the URL's password
        return "RedisRateLimiter(redis_url=<hidden>)"

    def _client(self) -> Redis:
        """The Redis client, connected on first use — the only place `redis` is imported."""
        if self._redis is None:
            from redis.asyncio import Redis  # lazy: the worker extra, not a base dependency

            self._redis = Redis.from_url(self._redis_url)
        return self._redis

    async def check(self, *, namespace: str, identity: str, rule: RateLimitRule,
                    now: datetime) -> RateLimitDecision:
        """Increment the window counter and decide; on any Redis fault, fail open and allow."""
        epoch = int(now.timestamp())
        start = epoch - (epoch % rule.window_seconds)
        key = f"{_KEY_PREFIX}{namespace}:{identity}:{start}"
        try:
            client = self._client()
            count = int(await client.incr(key))
            if count == 1:
                # First event in this window: give the key a TTL so the count resets when the
                # window ends. Set only on the first event, never after — refreshing the TTL on
                # every increment would turn a fixed window into a rolling one that never resets.
                await client.expire(key, rule.window_seconds)
        except Exception as error:  # a rate-store blip must never break sign-in or generation (§50)
            logger.warning(
                "rate limiter unavailable, allowing request (fail-open): %s",
                type(error).__name__)
            return RateLimitDecision(allowed=True, retry_after_seconds=0)
        return decide(count, rule, now)

    async def aclose(self) -> None:
        """Release the Redis connection pool, if one was ever opened."""
        if self._redis is not None:
            client: Any = self._redis
            await client.aclose()
            self._redis = None
