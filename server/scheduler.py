"""In-process daily scheduler. A 60-second asyncio tick decides — via the pure
is_due() rule — whether the agentic full run is due, then asks the RunManager
to launch it. The clock is injected for testing.

The tick loop sleeps BEFORE its first tick, so a short-lived TestClient never
fires a run during a test, and catch-up after real downtime is delayed by at
most one interval. Each tick is exception-wrapped: a bad tick never kills the
loop. Times are local (the machine's timezone)."""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from pipeline import settings as settings_store
from pipeline.db import connect
from server.runs import RunBusyError


def is_due(now: datetime, settings: dict, last_morning: Optional[datetime]) -> bool:
    """True if a full run should fire at `now`. Pure decision — no I/O."""
    if not settings.get("schedule_enabled", True):
        return False
    if settings.get("schedule_cadence", "daily") == "weekdays" and now.weekday() >= 5:
        return False  # Sat=5, Sun=6
    hh, mm = (int(part) for part in settings.get("schedule_time", "08:00").split(":"))
    scheduled = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < scheduled:
        return False
    # Already ran today (or, defensively, a future-dated row from clock skew).
    if last_morning is not None and last_morning.date() >= now.date():
        return False
    return True


def _last_morning(db_path: str | Path) -> Optional[datetime]:
    """The most recent 'morning' run's start time, as a local-aware datetime."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT started_at FROM runs WHERE kind='morning' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    dt = datetime.fromisoformat(row[0])
    if dt.tzinfo is not None:
        dt = dt.astimezone()  # normalize to local so .date() matches local now
    return dt


async def _tick(app, now: Callable[[], datetime]) -> None:
    settings = settings_store.load(app.state.settings_path)
    last = _last_morning(app.state.db_path)
    if is_due(now(), settings, last):
        try:
            await app.state.run_manager.trigger("full")
        except RunBusyError:
            pass  # a run is already active; nothing to do


async def scheduler_loop(app, *, now: Optional[Callable[[], datetime]] = None,
                         interval: float = 60.0,
                         sleep: Callable = asyncio.sleep) -> None:
    now_fn = now or (lambda: datetime.now().astimezone())
    while True:
        try:
            await sleep(interval)   # sleep first: no tick during a brief test
            await _tick(app, now_fn)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue  # a bad tick must never kill the loop
