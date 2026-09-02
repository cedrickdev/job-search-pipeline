"""RunManager: state transitions, the morning-row write, and the single-run
guard. The subprocess runner is injected, so no real process is spawned. Async
behavior is driven with asyncio.run(scenario()) — no pytest-asyncio needed."""
import asyncio

import pytest

from pipeline.db import connect, init_db
from server.runs import RunBusyError, RunManager


def _make_db(tmp_path):
    p = tmp_path / "runs.db"
    c = connect(p)
    init_db(c)
    c.close()
    return p


def test_status_transitions_and_last_run(tmp_path):
    db = _make_db(tmp_path)

    async def ok_runner(cmd, log_path):
        return True, "all good"

    async def scenario():
        mgr = RunManager(db, runner=ok_runner)
        assert mgr.status()["state"] == "idle"
        await mgr.trigger("discovery")
        assert mgr.status()["state"] == "running"
        await mgr._task
        return mgr.status()

    st = asyncio.run(scenario())
    assert st["state"] == "idle"
    assert st["last_run"]["kind"] == "discovery"
    assert st["last_run"]["ok"] is True


def test_full_run_writes_one_morning_row(tmp_path):
    db = _make_db(tmp_path)

    async def ok_runner(cmd, log_path):
        return True, "exit 0"

    async def scenario():
        mgr = RunManager(db, runner=ok_runner)
        await mgr.trigger("full")
        await mgr._task

    asyncio.run(scenario())
    conn = connect(db)
    rows = conn.execute(
        "SELECT kind, finished_at FROM runs WHERE kind='morning'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["finished_at"] is not None


def test_discovery_run_writes_no_row(tmp_path):
    db = _make_db(tmp_path)

    async def ok_runner(cmd, log_path):
        return True, "exit 0"

    async def scenario():
        mgr = RunManager(db, runner=ok_runner)
        await mgr.trigger("discovery")
        await mgr._task

    asyncio.run(scenario())
    conn = connect(db)
    n = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
    conn.close()
    assert n == 0


def test_second_trigger_while_running_raises_busy(tmp_path):
    db = _make_db(tmp_path)

    async def scenario():
        gate = asyncio.Event()

        async def slow_runner(cmd, log_path):
            await gate.wait()
            return True, "exit 0"

        mgr = RunManager(db, runner=slow_runner)
        await mgr.trigger("full")
        assert mgr.status()["state"] == "running"
        with pytest.raises(RunBusyError):
            await mgr.trigger("full")
        gate.set()
        await mgr._task
        assert mgr.status()["state"] == "idle"

    asyncio.run(scenario())


def test_failed_run_sets_ok_false_and_returns_idle(tmp_path):
    db = _make_db(tmp_path)

    async def bad_runner(cmd, log_path):
        return False, "exit 1: boom"

    async def scenario():
        mgr = RunManager(db, runner=bad_runner)
        await mgr.trigger("full")
        await mgr._task
        return mgr.status()

    st = asyncio.run(scenario())
    assert st["state"] == "idle"
    assert st["last_run"]["ok"] is False
    assert "boom" in st["last_run"]["summary"]


def test_unknown_kind_raises(tmp_path):
    db = _make_db(tmp_path)

    async def scenario():
        mgr = RunManager(db)
        with pytest.raises(ValueError):
            await mgr.trigger("bogus")

    asyncio.run(scenario())


def test_record_morning_failure_does_not_wedge_manager(tmp_path):
    """If _record_morning raises (e.g. database is locked), the manager must
    still return to idle so the next trigger() is not permanently blocked."""
    db = _make_db(tmp_path)

    async def ok_runner(cmd, log_path):
        return True, "exit 0"

    def _raise_db_locked(*args, **kwargs):
        raise RuntimeError("database is locked")

    async def scenario():
        mgr = RunManager(db, runner=ok_runner)
        mgr._record_morning = _raise_db_locked  # force the DB write to fail
        await mgr.trigger("full")
        await mgr._task
        # Manager must be idle despite the recording failure
        assert mgr.status()["state"] == "idle"
        # A subsequent trigger must NOT raise RunBusyError
        await mgr.trigger("full")
        await mgr._task
        return mgr.status()

    st = asyncio.run(scenario())
    assert st["state"] == "idle"
    assert st["last_run"] is not None
