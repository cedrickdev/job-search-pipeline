"""In-app run orchestration. A single guarded RunManager launches either the
deterministic discovery sweep (`python -m pipeline.discover`) or the agentic
full pipeline (`scripts/morning_run.sh`). The subprocess executor is injected
so tests never spawn a real process.

Discovery writes NO runs row — pipeline.discover owns the 'discovery' row that
lookback_days() depends on, and double-writing it would corrupt the lookback
window. A full run writes exactly one 'morning' row. A failed run never wedges
the manager: it records ok=False and returns to idle."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from pipeline import paths
from pipeline.db import connect
from server._env import child_env

# kind -> argv. The venv interpreter runs the deterministic discovery module;
# the shell wrapper drives Claude Code + the morning-run skill for the full run.
COMMANDS: dict[str, list[str]] = {
    "discovery": [str(paths.ROOT / ".venv" / "bin" / "python"), "-m", "pipeline.discover"],
    "full": [str(paths.ROOT / "scripts" / "morning_run.sh")],
}

Runner = Callable[[list[str], Path], Awaitable[tuple[bool, str]]]


class RunBusyError(Exception):
    """A run is already active; the endpoint maps this to HTTP 409."""


def _tail(log_path: Path, limit: int = 500) -> str:
    try:
        return log_path.read_text(errors="replace").strip()[-limit:]
    except OSError:
        return ""


async def _default_runner(cmd: list[str], log_path: Path) -> tuple[bool, str]:
    """Launch cmd from the repo root, stream combined stdout+stderr to log_path,
    with env from child_env() (Anthropic keys stripped). Returns (ok, summary)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=log, stderr=asyncio.subprocess.STDOUT,
            cwd=str(paths.ROOT), env=child_env(),
        )
        await proc.wait()
    tail = _tail(log_path)
    summary = f"exit {proc.returncode}" + (f": {tail}" if tail else "")
    return proc.returncode == 0, summary


class RunManager:
    """Guards a single active run. trigger() returns promptly; the run proceeds
    in a background task exposed as ``_task`` for tests to await."""

    def __init__(self, db_path: str | Path, *, runner: Optional[Runner] = None,
                 log_dir: str | Path | None = None,
                 now: Optional[Callable[[], datetime]] = None) -> None:
        self._db_path = Path(db_path)
        self._runner: Runner = runner or _default_runner
        self._log_dir = Path(log_dir) if log_dir is not None else paths.LOG_DIR
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._state = "idle"
        self._current: Optional[dict] = None
        self._last_run: Optional[dict] = None
        self._task: Optional[asyncio.Task] = None

    async def trigger(self, kind: str) -> None:
        if kind not in COMMANDS:
            raise ValueError(f"unknown run kind: {kind!r}")
        if self._state == "running":
            raise RunBusyError("a run is already in progress")
        started = self._now().isoformat(timespec="seconds")
        self._state = "running"
        self._current = {"kind": kind, "started_at": started}
        self._task = asyncio.create_task(self._run(kind, started))

    async def _run(self, kind: str, started: str) -> None:
        stamp = started.replace(":", "").replace("-", "")
        log_path = self._log_dir / f"run-{kind}-{stamp}.log"
        try:
            ok, summary = await self._runner(COMMANDS[kind], log_path)
        except Exception as exc:  # a crashing runner must not wedge the manager
            ok, summary = False, f"runner error: {exc}"
        finished = self._now().isoformat(timespec="seconds")
        try:
            if kind == "full":
                self._record_morning(started, finished, ok, summary)
        except Exception as exc:  # recording must not wedge the manager either
            ok, summary = False, f"{summary} (record failed: {exc})"
        finally:
            self._last_run = {"kind": kind, "started_at": started,
                              "finished_at": finished, "ok": ok, "summary": summary}
            self._current = None
            self._state = "idle"

    def _record_morning(self, started: str, finished: str, ok: bool, summary: str) -> None:
        conn = connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO runs (kind, started_at, finished_at, summary) "
                "VALUES (?, ?, ?, ?)",
                ("morning", started, finished, json.dumps({"ok": ok, "summary": summary})),
            )
            conn.commit()
        finally:
            conn.close()

    def status(self) -> dict:
        return {
            "state": self._state,
            "kind": self._current["kind"] if self._current else None,
            "started_at": self._current["started_at"] if self._current else None,
            "last_run": self._last_run,
        }
