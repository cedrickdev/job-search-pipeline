"""The port that keeps the browser worker behind an interface (§44-48).

Playwright is an execution adapter, never the decision engine (CLAUDE.md). A browser
adapter that needs to drive a page does not import Playwright and run it inline; it
hands a typed `BrowserTask` to a `TaskDispatcher`, which runs it — in this process
for a test, in an isolated worker subprocess in production — and returns a typed
`BrowserTaskResult`. The engine above the dispatcher cannot tell which, and a test
never launches a browser (CLAUDE.md §Testing).

The V1 browser lock is preserved, not replaced (§46). V1's apply worker takes an
exclusive `flock` on `data/browser_state/.lock` so browser runs never collide on the
shared profile; V2 takes the *same* lock through `browser_lock`, so a V2 submission
and a V1 morning-run applier still serialize against each other rather than fighting
over one Chromium profile. A contended lock is a first-class outcome — `BrowserBusy`,
which an adapter turns into a `REQUIRES_HUMAN`/retry — not a crash.

The crash rule of §88 lives here too: if a `SUBMIT` worker dies without reporting,
the dispatcher returns `STATE_UNKNOWN`, never `FAILED` — a submission that may have
left the platform must not look retryable, because a blind retry could double-submit.
A `PREPARE` worker that dies is simply `FAILED`, because preparation is reversible and
safe to redo.
"""
import asyncio
import contextlib
import fcntl
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from enum import StrEnum
from pathlib import Path
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from backend.app.domain.application_channel import HumanRequiredReason
from backend.app.domain.base import HttpUrlStr, NonEmptyStr
from backend.app.domain.identifiers import ApplicationId

# The exact lock file V1's `pipeline.apply_one` uses, and the child-env scrubber the
# rest of the platform spawns workers with, imported rather than re-derived so the
# two can never drift from what V1 relies on.
from pipeline import paths
from server._env import child_env

BROWSER_LOCK_PATH: Path = paths.DATA_DIR / "browser_state" / ".lock"
_BROWSER_WORKER_MODULE = "backend.app.application_engine.browser_worker"


class DispatcherValue(BaseModel):
    """Frozen base for the dispatcher's typed task and result."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class BrowserTaskKind(StrEnum):
    """Which phase of an application a browser task performs.

    The same split the adapter contract draws: `PREPARE` is reversible — it reads
    the form and reports its structure — while `SUBMIT` is the irreversible act and
    is dispatched at most once per attempt, only after the gate authorized it.
    """

    PREPARE = "PREPARE"
    SUBMIT = "SUBMIT"


class BrowserTask(DispatcherValue):
    """One unit of browser work, described without naming a browser.

    Deliberately thin: it names the application, the phase and the target, and
    carries a `correlation_id` so the worker's own log lines thread onto the same
    audit story (§43). It carries no answers or documents inline — the worker reads
    those from the persisted application it is given the id of, so a task never puts
    candidate content on a command line where it could leak into a process list.
    """

    application_id: ApplicationId
    kind: BrowserTaskKind
    target_url: HttpUrlStr | None = None
    correlation_id: NonEmptyStr | None = None


class BrowserTaskOutcome(StrEnum):
    """How a browser task ended, in the vocabulary the adapter maps upward.

    Mirrors `SubmissionOutcome` on purpose, so a browser adapter can translate a
    dispatcher result into a `SubmissionResult` without inventing a mapping: a
    completed submit becomes SUBMITTED, a blocked one becomes the matching human
    hand-off, an ambiguous one becomes STATE_UNKNOWN.
    """

    COMPLETED = "COMPLETED"
    REQUIRES_HUMAN = "REQUIRES_HUMAN"
    FAILED = "FAILED"
    STATE_UNKNOWN = "STATE_UNKNOWN"


class BrowserTaskResult(DispatcherValue):
    """What a browser task reports back — typed, and free of any page's raw text.

    `detail` is a composed, secret-free sentence (§83): a page's own error text can
    echo a credential or dump a full-page HTML blob, so the worker classifies and
    composes rather than forwarding. `human_required_reason` is required when the
    outcome is `REQUIRES_HUMAN` and forbidden otherwise, so an adapter can trust the
    shape. `form_fingerprint` is what a `PREPARE` task captured, for the submit-time
    re-check (§34).
    """

    outcome: BrowserTaskOutcome
    detail: NonEmptyStr | None = None
    human_required_reason: HumanRequiredReason | None = None
    form_fingerprint: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _human_reason_matches_outcome(self) -> Self:
        if self.outcome is BrowserTaskOutcome.REQUIRES_HUMAN \
                and self.human_required_reason is None:
            raise ValueError(
                "a REQUIRES_HUMAN browser result must name a human_required_reason")
        if self.outcome is not BrowserTaskOutcome.REQUIRES_HUMAN \
                and self.human_required_reason is not None:
            raise ValueError(
                "only a REQUIRES_HUMAN browser result may carry a human_required_reason")
        return self


class BrowserBusy(Exception):
    """The shared browser lock is held, so this task cannot run right now.

    Not a failure of the application — a scheduling fact. A V1 run or another V2
    submission holds the profile; the adapter turns this into a retry or a
    `REQUIRES_HUMAN` hand-off ("browser in use"), never a lost application.
    """


@contextlib.contextmanager
def browser_lock(*, lock_path: Path | None = None) -> Iterator[None]:
    """Hold the exclusive browser-profile lock for the duration of the block.

    The same advisory `flock` V1 uses, on the same file, taken non-blocking: if the
    lock is already held the manager raises `BrowserBusy` immediately rather than
    queueing, because a browser task that blocked indefinitely would be worse than
    one that reports "try again". The lock is always released, even on error.
    """
    path = lock_path if lock_path is not None else BROWSER_LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("w")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BrowserBusy(
                "the shared browser profile is in use by another run") from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


@runtime_checkable
class TaskDispatcher(Protocol):
    """Runs a browser task and returns its typed result (§44).

    The whole surface is `run`. A concrete dispatcher runs the task wherever it runs
    it — an isolated subprocess in production, in-process for a test — while the
    adapter above stays indifferent. An implementation never raises an
    adapter-specific exception across this boundary: it returns a typed result, and
    the one exception it may raise is `BrowserBusy`, which is a scheduling fact rather
    than a failure of the application.
    """

    async def run(self, task: BrowserTask) -> BrowserTaskResult:
        ...


def _crash_outcome(kind: BrowserTaskKind) -> BrowserTaskResult:
    """The result a worker crash implies, per the §88 safety rule.

    A `SUBMIT` that crashed may have left the platform, so its state is unknown and
    it must never look retryable; a `PREPARE` that crashed changed nothing, so it is
    a plain, safe-to-retry failure.
    """
    if kind is BrowserTaskKind.SUBMIT:
        return BrowserTaskResult(
            outcome=BrowserTaskOutcome.STATE_UNKNOWN,
            detail="the submission worker exited without reporting an outcome")
    return BrowserTaskResult(
        outcome=BrowserTaskOutcome.FAILED,
        detail="the preparation worker exited without reporting an outcome")


# How a subprocess dispatcher starts a worker: given the task and the two file paths
# (the request to read, the result to write), start it and resolve to its exit code.
# Injectable so a test can simulate a clean run, a crash or a lock contention without
# ever spawning a process. The default implementation spawns the real worker module.
WorkerSpawner = Callable[[BrowserTask, Path, Path], Awaitable[int]]


async def _default_spawn(task: BrowserTask, request_path: Path,
                         result_path: Path) -> int:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", _BROWSER_WORKER_MODULE,
        "--request", str(request_path), "--result", str(result_path),
        cwd=str(paths.ROOT), env=child_env())
    return await proc.wait()


class SubprocessTaskDispatcher:
    """Runs a browser task in an isolated worker subprocess (§44-48).

    The production dispatcher. It writes the task to a request file, spawns the
    worker with the Anthropic namespace scrubbed (`child_env`, the same guard V1 and
    the LLM layer use), waits for it, and reads a typed result file back — candidate
    content and outcomes travel through files, never a command line. The worker takes
    the browser lock itself, exactly as V1's does, so V1 and V2 serialize.

    A worker that exits without writing a result is a crash, and `_crash_outcome`
    turns it into the safe outcome for the phase: `STATE_UNKNOWN` for a submission,
    `FAILED` for a preparation. `spawn` is injectable so this whole flow — including
    the crash path — is testable without a real process or browser.
    """

    def __init__(self, *, spawn: WorkerSpawner | None = None) -> None:
        self._spawn: WorkerSpawner = spawn or _default_spawn

    async def run(self, task: BrowserTask) -> BrowserTaskResult:
        with tempfile.TemporaryDirectory(prefix="v2-browser-task-") as tmp:
            request_path = Path(tmp) / "request.json"
            result_path = Path(tmp) / "result.json"
            request_path.write_text(task.model_dump_json(), encoding="utf-8")
            exit_code = await self._spawn(task, request_path, result_path)
            if exit_code != 0 or not result_path.exists():
                return _crash_outcome(task.kind)
            raw = result_path.read_text(encoding="utf-8")
            try:
                return BrowserTaskResult.model_validate_json(raw)
            except ValueError:
                # A result file that will not parse is indistinguishable, for safety,
                # from no result at all: the worker's outcome is unknown.
                return _crash_outcome(task.kind)


class FakeTaskDispatcher:
    """An in-process dispatcher that returns scripted results (test double).

    The default dispatcher in every test: it never touches a browser, a lock or a
    subprocess. It answers each `run` from a queue of results (or a single default),
    and records the tasks it was asked to run so a test can assert a submit was
    dispatched exactly once — the §37 idempotency and §5 gate regressions rely on
    counting, not on a real browser.
    """

    def __init__(self, *, results: tuple[BrowserTaskResult, ...] = (),
                 default: BrowserTaskResult | None = None) -> None:
        self._results = list(results)
        self._default = default or BrowserTaskResult(
            outcome=BrowserTaskOutcome.COMPLETED)
        self.calls: list[BrowserTask] = []

    async def run(self, task: BrowserTask) -> BrowserTaskResult:
        self.calls.append(task)
        if self._results:
            return self._results.pop(0)
        return self._default


def load_browser_task(request_path: Path) -> BrowserTask:
    """Read a worker's request file back into a typed task (for the worker entrypoint)."""
    return BrowserTask.model_validate_json(request_path.read_text(encoding="utf-8"))


def write_browser_result(result_path: Path, result: BrowserTaskResult) -> None:
    """Write a worker's typed result out (for the worker entrypoint).

    A small helper so the worker and the dispatcher share one serialization and one
    file contract, rather than each hand-rolling `json.dumps` and drifting.
    """
    result_path.write_text(result.model_dump_json(), encoding="utf-8")


__all__ = [
    "BROWSER_LOCK_PATH", "BrowserBusy", "BrowserTask", "BrowserTaskKind",
    "BrowserTaskOutcome", "BrowserTaskResult", "FakeTaskDispatcher",
    "SubprocessTaskDispatcher", "TaskDispatcher", "WorkerSpawner", "browser_lock",
    "load_browser_task", "write_browser_result",
]
