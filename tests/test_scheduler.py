"""Scheduler: the pure is_due() decision and the DB-backed tick. Catch-up is
inherent to is_due — the first tick after downtime sees now past the scheduled
time with no morning row today and returns True (a single missed fire, never a
backlog). Weekday anchors: 2026-06-17 Wed, 06-19 Fri, 06-20 Sat, 06-16 Tue."""
import asyncio
from datetime import datetime

import pytest

from pipeline.db import connect, init_db
from server.runs import RunBusyError
from server.scheduler import _last_morning, _tick, is_due, scheduler_loop

BASE = {"schedule_enabled": True, "schedule_time": "08:00", "schedule_cadence": "daily"}


def _dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm)


def test_due_when_past_time_and_not_run_today():
    assert is_due(_dt(2026, 6, 17, 8, 1), BASE, None) is True


def test_not_due_before_time():
    assert is_due(_dt(2026, 6, 17, 7, 59), BASE, None) is False


def test_not_due_when_disabled():
    assert is_due(_dt(2026, 6, 17, 9, 0), {**BASE, "schedule_enabled": False}, None) is False


def test_not_due_when_already_ran_today():
    assert is_due(_dt(2026, 6, 17, 9, 0), BASE, _dt(2026, 6, 17, 8, 0)) is False


def test_due_when_last_run_was_yesterday():
    # Catch-up: server was down at 08:00, comes up at 09:00, last run was the
    # day before -> fire once.
    assert is_due(_dt(2026, 6, 17, 9, 0), BASE, _dt(2026, 6, 16, 8, 0)) is True


def test_weekdays_cadence_skips_saturday():
    assert is_due(_dt(2026, 6, 20, 9, 0), {**BASE, "schedule_cadence": "weekdays"}, None) is False


def test_weekdays_cadence_runs_on_friday():
    assert is_due(_dt(2026, 6, 19, 9, 0), {**BASE, "schedule_cadence": "weekdays"}, None) is True


class _FakeManager:
    def __init__(self):
        self.calls = []

    async def trigger(self, kind):
        self.calls.append(kind)


class _BusyManager:
    async def trigger(self, kind):
        raise RunBusyError("busy")


class _State:
    pass


class _FakeApp:
    def __init__(self, db_path, settings_path, manager=None):
        self.state = _State()
        self.state.db_path = db_path
        self.state.settings_path = settings_path
        self.state.run_manager = manager or _FakeManager()


def _make_db(tmp_path):
    p = tmp_path / "sched.db"
    c = connect(p)
    init_db(c)
    c.close()
    return p


def test_last_morning_none_when_empty(tmp_path):
    assert _last_morning(_make_db(tmp_path)) is None


def test_last_morning_reads_latest(tmp_path):
    db = _make_db(tmp_path)
    conn = connect(db)
    conn.execute(
        "INSERT INTO runs (kind, started_at, finished_at, summary) VALUES (?,?,?,?)",
        ("morning", "2026-06-16T12:00:00+00:00", "2026-06-16T12:05:00+00:00", "{}"))
    conn.commit()
    conn.close()
    dt = _last_morning(db)
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2026, 6, 16)


def test_tick_triggers_full_when_due(tmp_path):
    db = _make_db(tmp_path)
    sp = tmp_path / "settings.json"  # missing -> defaults (enabled, 08:00, daily)
    app = _FakeApp(db, sp)
    asyncio.run(_tick(app, now=lambda: _dt(2026, 6, 17, 8, 1)))
    assert app.state.run_manager.calls == ["full"]


def test_tick_does_not_trigger_when_disabled(tmp_path):
    from pipeline import settings as ss
    db = _make_db(tmp_path)
    sp = tmp_path / "settings.json"
    ss.save({**ss.DEFAULTS, "schedule_enabled": False}, sp)
    app = _FakeApp(db, sp)
    asyncio.run(_tick(app, now=lambda: _dt(2026, 6, 17, 9, 0)))
    assert app.state.run_manager.calls == []


def test_tick_swallows_busy(tmp_path):
    db = _make_db(tmp_path)
    sp = tmp_path / "settings.json"  # defaults -> enabled
    app = _FakeApp(db, sp, manager=_BusyManager())
    asyncio.run(_tick(app, now=lambda: _dt(2026, 6, 17, 9, 0)))  # must not raise


def test_loop_ticks_then_stops_on_cancel(tmp_path):
    db = _make_db(tmp_path)
    sp = tmp_path / "settings.json"  # defaults -> enabled
    app = _FakeApp(db, sp)
    seen = {"sleeps": 0}

    async def fake_sleep(_):
        seen["sleeps"] += 1
        if seen["sleeps"] >= 2:
            raise asyncio.CancelledError

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await scheduler_loop(app, now=lambda: _dt(2026, 6, 17, 9, 0),
                                 interval=0, sleep=fake_sleep)

    asyncio.run(scenario())
    assert app.state.run_manager.calls == ["full"]  # exactly one tick before cancel
