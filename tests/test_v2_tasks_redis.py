"""`RedisNotifyingDispatcher` — the durable-first, notify-second queue adapter (§33, §37).

The one place the codebase speaks Redis, tested without a server: a fake client stands in for
`redis.asyncio.Redis` (the real one is imported lazily and never touched here, honouring the rule
that the default suite needs no Redis). The contract these tests pin is the one correctness rests
on — the durable enqueue is authoritative and a wake is best-effort:

- an enqueue persists through the wrapped durable dispatcher and returns *its* id, then publishes a
  wake on the lane's channel (one channel per lane, the §35 isolation mirrored in the notifier);
- a Redis fault on publish is swallowed — the committed row already stands, so the enqueue still
  returns and the worker's poll finds the task regardless (§37: never assume Redis delivery);
- the Redis URL, which can carry a password, never appears in the repr (§41).
"""
from datetime import UTC, datetime

import pytest

from backend.app.domain.identifiers import new_user_id
from backend.app.domain.task import TaskKind, TaskLane, TaskSpec
from backend.app.infrastructure.tasks import RedisNotifyingDispatcher, lane_wake_channel
from backend.app.tasks.dispatcher import TaskDispatcher
from tests.v2_fakes import FakeTaskDispatcher

NOW = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


class FakeRedis:
    """A stand-in for `redis.asyncio.Redis`: records publishes, or fails on demand (no server)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, str]] = []
        self.closed = False
        self._fail = fail

    async def publish(self, channel: str, message: str) -> int:
        if self._fail:
            raise ConnectionError("redis is down")
        self.published.append((channel, message))
        return 1

    async def aclose(self) -> None:
        self.closed = True


def _spec(kind: TaskKind, key: str) -> TaskSpec:
    """A spec whose derived lane the notifier publishes on."""
    user = None if kind is TaskKind.RETENTION_SWEEP else new_user_id()
    return TaskSpec(kind=kind, idempotency_key=key, user_id=user, payload={})


def _with_client(durable: TaskDispatcher, client: FakeRedis) -> RedisNotifyingDispatcher:
    """A notifier wrapping `durable`, its lazy client pre-seeded so no real Redis is imported."""
    dispatcher = RedisNotifyingDispatcher(durable, redis_url="redis://:secret@host:6379/0")
    dispatcher._redis = client  # type: ignore[attr-defined]  # inject the fake for the lazy client
    return dispatcher


def test_lane_wake_channel_is_one_channel_per_lane() -> None:
    """Each lane wakes on its own channel, so a general worker is never woken by browser traffic."""
    assert lane_wake_channel(TaskLane.GENERAL) == "jobsearch:tasks:wake:GENERAL"
    assert lane_wake_channel(TaskLane.BROWSER) == "jobsearch:tasks:wake:BROWSER"


@pytest.mark.asyncio
async def test_enqueue_persists_durably_then_wakes_the_lane() -> None:
    """The durable write is authoritative; the wake lands on the enqueued task's lane channel."""
    durable = FakeTaskDispatcher()
    client = FakeRedis()
    dispatcher = _with_client(durable, client)
    spec = _spec(TaskKind.APPLICATION_SUBMISSION, key="app-1")

    run_id = await dispatcher.enqueue(spec)

    assert run_id == spec.run_id                          # returns the durable row's id
    assert durable.enqueued_kinds == [TaskKind.APPLICATION_SUBMISSION]  # persisted first
    assert client.published == [("jobsearch:tasks:wake:BROWSER", "1")]  # then woke the right lane


@pytest.mark.asyncio
async def test_enqueue_survives_a_redis_publish_failure() -> None:
    """A wake failure is swallowed: the committed row stands and the enqueue still returns (§37)."""
    durable = FakeTaskDispatcher()
    client = FakeRedis(fail=True)
    dispatcher = _with_client(durable, client)
    spec = _spec(TaskKind.RETENTION_SWEEP, key="sweep-1")

    run_id = await dispatcher.enqueue(spec)

    assert run_id == spec.run_id                       # the enqueue succeeded despite Redis
    assert durable.enqueued_kinds == [TaskKind.RETENTION_SWEEP]  # the durable write happened
    assert client.published == []                      # nothing was published — it raised


@pytest.mark.asyncio
async def test_dispatcher_satisfies_the_protocol() -> None:
    """The notifier is a structural `TaskDispatcher` — a drop-in for the durable one upstream."""
    assert isinstance(
        RedisNotifyingDispatcher(FakeTaskDispatcher(), redis_url="redis://x"), TaskDispatcher)


def test_repr_never_leaks_the_redis_url_password() -> None:
    """The URL can carry a password, so the repr shows only the wrapped dispatcher (§41)."""
    dispatcher = RedisNotifyingDispatcher(
        FakeTaskDispatcher(), redis_url="redis://:hunter2@host:6379/0")
    assert "hunter2" not in repr(dispatcher)


@pytest.mark.asyncio
async def test_aclose_releases_an_opened_client_and_no_ops_otherwise() -> None:
    """`aclose` closes a client that was opened, and is a safe no-op when none ever was."""
    never_opened = RedisNotifyingDispatcher(FakeTaskDispatcher(), redis_url="redis://x")
    await never_opened.aclose()  # no client was built — must not raise

    client = FakeRedis()
    opened = _with_client(FakeTaskDispatcher(), client)
    await opened.aclose()
    assert client.closed is True
