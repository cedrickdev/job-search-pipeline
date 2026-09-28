"""Queue-library-specific code — the notification layer over the durable queue (§33).

Phase 16 §33 is explicit about the boundary this package guards: a service depends on the
`TaskDispatcher` protocol and the durable `TaskRun` table, *never* on a queue library's API. So
everything that knows the name of a queue technology — here, Redis — lives in this one package,
behind the same `TaskDispatcher` seam a service already uses. Swapping Redis for another transport,
or dropping it entirely to run on the plain database poll, is a change confined to here.

The design keeps the database as the source of truth and Redis as *notification only*
(`RedisNotifyingDispatcher`): an enqueue first persists a `QUEUED` `TaskRun` (durable, idempotent),
then publishes a best-effort wake so an idle worker can react before its next poll. If Redis is
unreachable the persisted row still stands and the worker's poll picks the task up — a lost wake
costs latency, never work. The Redis client is imported lazily (only when a notifier is
constructed), so importing this package, and running the default test suite, needs neither the
`redis` package installed nor a server reachable.
"""
from __future__ import annotations

from backend.app.infrastructure.tasks.redis_dispatcher import (
    RedisNotifyingDispatcher,
    lane_wake_channel,
)

__all__ = ["RedisNotifyingDispatcher", "lane_wake_channel"]
