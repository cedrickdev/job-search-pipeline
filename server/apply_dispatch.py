"""Background apply dispatch.

Spawns one `python -m pipeline.apply_one --request-id N` subprocess per apply
request, Anthropic keys stripped via child_env(). Unlike RunManager this is NOT
a singleton guard: the apply worker serializes itself via the browser lock, and
an apply must not 409 against a discovery/morning run.
"""
import asyncio
import sys
from typing import Awaitable, Callable, Optional

from pipeline import paths
from server._env import child_env

Spawner = Callable[[int], Awaitable[None]]

# Tasks awaiting spawned workers' exit, so the children are reaped (no zombies)
# on a long-lived server. Each task removes itself on completion.
_reapers: set = set()


async def _default_spawn(request_id: int) -> None:
    # Fire-and-forget: start the detached worker and return. It manages its own
    # lifecycle and writes its outcome back to apply_requests. We keep no hold
    # on its outcome, but we DO reap it so the long-lived server does not
    # accumulate zombie children.
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pipeline.apply_one",
        "--request-id",
        str(request_id),
        cwd=str(paths.ROOT),
        env=child_env(),
    )
    task = asyncio.create_task(proc.wait())
    _reapers.add(task)
    task.add_done_callback(_reapers.discard)


class ApplyDispatcher:
    def __init__(self, *, spawn: Optional[Spawner] = None) -> None:
        self._spawn: Spawner = spawn or _default_spawn

    async def dispatch(self, request_id: int) -> None:
        await self._spawn(request_id)
