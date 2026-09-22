"""The isolated browser-worker entrypoint the subprocess dispatcher spawns (§44-48).

`python -m backend.app.application_engine.browser_worker --request R --result W` is
what `SubprocessTaskDispatcher` runs: it reads a typed `BrowserTask` from `R`, does
the browser work under the shared profile lock, and writes a typed `BrowserTaskResult`
to `W`. Running here rather than in the API process is the whole point — a crashing
Chromium takes down a throwaway worker, not the server, and the §88 crash rule then
resolves a lost SUBMIT to STATE_UNKNOWN because no result file was written.

Driving a real page with Playwright is a later increment; until it lands, the worker
does the safe, honest thing rather than pretend. It takes the same `flock` V1 uses —
so it still serializes against the V1 applier even as a placeholder — and reports
`LOGIN_REQUIRED`, a `REQUIRES_HUMAN` hand-off, for any task. That keeps the contract
real (the dispatcher, the lock and the file protocol are all exercised) while the
platform never claims to have submitted something it did not.
"""
import argparse
from pathlib import Path

from backend.app.application_engine.task_dispatcher import (
    BrowserBusy,
    BrowserTaskOutcome,
    BrowserTaskResult,
    browser_lock,
    load_browser_task,
    write_browser_result,
)
from backend.app.domain.application_channel import HumanRequiredReason


def run(request_path: Path, result_path: Path) -> None:
    """Read the task, do the work under the browser lock, write the typed result.

    A `BrowserBusy` is written as a `REQUIRES_HUMAN` "browser in use" result rather
    than raised: the worker's contract is to report an outcome, never to exit on a
    scheduling fact. Any other exception is deliberately *not* caught — it exits
    non-zero with no result file, which the dispatcher reads as the crash it is (§88).
    """
    task = load_browser_task(request_path)
    try:
        with browser_lock():
            result = _drive(task)
    except BrowserBusy:
        result = BrowserTaskResult(
            outcome=BrowserTaskOutcome.REQUIRES_HUMAN,
            human_required_reason=HumanRequiredReason.LOGIN_REQUIRED,
            detail="the browser profile is in use by another run")
    write_browser_result(result_path, result)


def _drive(task: object) -> BrowserTaskResult:
    """Placeholder for the Playwright automation, safe by construction.

    Until a page driver lands, every task is an honest human hand-off — the platform
    prepared the materials, and a person completes the browser step. Never a
    COMPLETED it cannot substantiate.
    """
    return BrowserTaskResult(
        outcome=BrowserTaskOutcome.REQUIRES_HUMAN,
        human_required_reason=HumanRequiredReason.LOGIN_REQUIRED,
        detail="automated browser submission is not yet available on this channel")


def main() -> None:
    parser = argparse.ArgumentParser(description="V2 browser application worker")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    run(args.request, args.result)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    main()
