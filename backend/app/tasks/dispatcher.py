"""`TaskDispatcher` — the seam a service enqueues background work through (§32-33).

`docs/ARCHITECTURE.md` specifies `TaskDispatcher.enqueue(TaskSpec) -> TaskId`, and Phase 16 §32-33
asks that this abstraction exist *before* any service binds to Redis: a service that wants work
done later depends on this protocol, never on a queue library, so the queue technology can change
(or be a plain database poll) without touching a caller.

`PersistedTaskDispatcher` is the durable implementation the whole design rests on. It enqueues by
writing a `QUEUED` `TaskRun` to the queue table through `TaskRunRepository.add`, which is
idempotent by the derived id (§37): enqueuing the same job twice returns the row already there
rather than running it twice. The row is the source of truth a worker leases; a Redis/ARQ adapter
(`backend.app.infrastructure.tasks`) layers *notification* on top of this durable write, never
replaces it. Like every repository caller, the dispatcher does not commit — the surrounding
`session_scope` unit of work does — so an enqueue commits (or rolls back) atomically with whatever
domain change prompted it.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.app.domain.identifiers import TaskRunId
from backend.app.domain.task import TaskSpec
from backend.app.repositories.contracts import TaskRunRepository
from backend.app.tasks.clock import Clock


@runtime_checkable
class TaskDispatcher(Protocol):
    """What a service depends on to run work later — enqueue a `TaskSpec`, get back its run id."""

    async def enqueue(self, task: TaskSpec) -> TaskRunId:
        """Enqueue `task` for background execution and return the id of the run that now exists.

        Idempotent by construction: the run id derives from the spec's idempotency key, so two
        enqueues of the same job return the same id and produce one run, never two (§37).
        """
        ...


class PersistedTaskDispatcher:
    """A `TaskDispatcher` that enqueues by persisting a `QUEUED` `TaskRun` (§32, §37).

    Holds the queue repository and a `Clock`: `enqueue` stamps the run's timestamps from the clock
    and hands it to `add`, whose derived-id idempotency makes a redelivered enqueue a no-op. The
    transaction is the caller's `session_scope`; this class never commits.
    """

    def __init__(self, tasks: TaskRunRepository, *, clock: Clock) -> None:
        self._tasks = tasks
        self._clock = clock

    async def enqueue(self, task: TaskSpec) -> TaskRunId:
        """Persist the fresh `QUEUED` run this spec describes and return its (idempotent) id."""
        stored = await self._tasks.add(task.to_queued_run(as_of=self._clock()))
        return stored.id
