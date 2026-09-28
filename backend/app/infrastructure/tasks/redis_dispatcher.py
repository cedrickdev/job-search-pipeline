"""`RedisNotifyingDispatcher` — the concrete Redis queue adapter (§33).

The one place in the codebase that speaks Redis. It implements the same `TaskDispatcher` seam a
service depends on, so nothing upstream changes when this is the dispatcher rather than the plain
`PersistedTaskDispatcher`. Its contract is *durable-first, notify-second*:

1. Persist the `QUEUED` `TaskRun` through the wrapped durable dispatcher — the idempotent write the
   whole design rests on (§32, §37). This is what a worker actually leases.
2. Publish a best-effort wake on the lane's channel so an idle worker polls immediately instead of
   waiting out `poll_seconds`. A wake is a latency optimisation, not a delivery guarantee: if Redis
   is down the enqueue has *already* committed the durable row, so the failed publish is logged and
   swallowed and the worker's next poll finds the task regardless (§37: never assume Redis
   delivery). The wake carries no task data — an id at most — so nothing sensitive transits Redis.

`redis.asyncio` is imported lazily inside `_client`, so importing this module needs neither the
`redis` package installed nor a server reachable — the default test suite touches no Redis (the
project's testing rule), and a deployment installs the `worker` extra to get the client.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from backend.app.domain.identifiers import TaskRunId
from backend.app.domain.task import TaskLane, TaskSpec
from backend.app.tasks.dispatcher import TaskDispatcher

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from redis.asyncio import Redis

logger = logging.getLogger("jobsearch.tasks.redis")

# The channel a lane's wakes are published on. One channel per lane so a general-lane worker is
# never woken by browser-lane traffic and vice-versa — the §35 isolation, mirrored in the notifier.
_WAKE_CHANNEL_PREFIX = "jobsearch:tasks:wake:"


def lane_wake_channel(lane: TaskLane) -> str:
    """The Redis pub/sub channel wakes for `lane` are published on (§35)."""
    return f"{_WAKE_CHANNEL_PREFIX}{lane.value}"


class RedisNotifyingDispatcher:
    """A `TaskDispatcher` that persists durably, then publishes a best-effort Redis wake (§33).

    Wraps the durable dispatcher (never replaces it) and holds a lazily-connected Redis client. The
    Redis URL is kept off the `repr` because it can carry a password (§41: never log a credential).
    """

    def __init__(self, durable: TaskDispatcher, *, redis_url: str) -> None:
        self._durable = durable
        self._redis_url = redis_url
        self._redis: Redis | None = None

    def __repr__(self) -> str:  # never leak the URL's password
        return f"RedisNotifyingDispatcher(durable={self._durable!r})"

    def _client(self) -> Redis:
        """The Redis client, connected on first use — the only place `redis` is imported."""
        if self._redis is None:
            from redis.asyncio import Redis  # lazy: the worker extra, not a base dependency

            self._redis = Redis.from_url(self._redis_url)
        return self._redis

    async def enqueue(self, task: TaskSpec) -> TaskRunId:
        """Persist the task durably, then publish a best-effort wake; return the run id (§32-33).

        The durable write is authoritative and idempotent; the wake is fire-and-forget. A Redis
        failure never fails the enqueue — the committed row is what a worker leases, and its poll
        finds the task even if no wake ever arrives.
        """
        run_id = await self._durable.enqueue(task)
        await self._notify(task.lane)
        return run_id

    async def _notify(self, lane: TaskLane) -> None:
        """Publish a wake on the lane's channel, swallowing any Redis fault as mere lost latency."""
        try:
            await self._client().publish(lane_wake_channel(lane), "1")
        except Exception as error:  # a lost wake costs latency, never work (§37)
            logger.warning("task wake publish failed (worker will still poll): %s", error)

    async def aclose(self) -> None:
        """Release the Redis connection pool, if one was ever opened."""
        if self._redis is not None:
            client: Any = self._redis
            await client.aclose()
            self._redis = None
