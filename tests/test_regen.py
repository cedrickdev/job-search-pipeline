import sqlite3

import pytest

from tests.helpers import seed_job
from pipeline.regen import (
    CREATIVITY_LEVELS, create_request, fail_request, latest_request,
    pending_requests, resolve_request)


def test_regen_requests_table_exists(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(regen_requests)")}
    assert cols == {"id", "job_id", "notes", "status", "created_at",
                    "resolved_at", "creativity", "detail"}


def test_regen_requests_status_check(conn):
    conn.execute(
        "INSERT INTO jobs (source, company, title, discovered_date, dedup_hash)"
        " VALUES ('wtj', 'Acme', 'Sales', '2026-06-11', 'regen-check-h1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO regen_requests (job_id, notes, status, created_at)"
            " VALUES (1, 'note', 'bogus', '2026-06-11T08:00:00')")


def test_create_and_pending_round_trip(conn):
    job_id, _ = seed_job(conn)
    request_id = create_request(conn, job_id, "Emphasize the shop-floor work")
    pending = pending_requests(conn)
    assert len(pending) == 1
    assert pending[0]["id"] == request_id
    assert pending[0]["job_id"] == job_id
    assert pending[0]["notes"] == "Emphasize the shop-floor work"
    assert pending[0]["company"] == "Acme"


def test_create_logs_event(conn):
    job_id, _ = seed_job(conn)
    create_request(conn, job_id, "More NLP")
    row = conn.execute(
        "SELECT * FROM events WHERE event_type = 'regen_requested'").fetchone()
    assert row["job_id"] == job_id
    assert row["source"] == "dashboard"
    assert row["detail"] == "More NLP"


def test_create_rejects_empty_notes(conn):
    job_id, _ = seed_job(conn)
    with pytest.raises(ValueError):
        create_request(conn, job_id, "   ")


def test_create_rejects_unknown_job(conn):
    with pytest.raises(ValueError):
        create_request(conn, 999, "notes")


def test_resolve_marks_done(conn):
    job_id, _ = seed_job(conn)
    request_id = create_request(conn, job_id, "notes")
    resolve_request(conn, request_id)
    assert pending_requests(conn) == []
    row = conn.execute("SELECT * FROM regen_requests WHERE id = ?",
                       (request_id,)).fetchone()
    assert row["status"] == "done"
    assert row["resolved_at"] is not None


def test_resolve_unknown_id_raises(conn):
    with pytest.raises(ValueError):
        resolve_request(conn, 42)


def test_resolve_logs_completed_event(conn):
    # The dashboard needs a positive "done" signal, so resolving a request logs
    # a job-keyed completion event the drawer Activity timeline can surface.
    job_id, _ = seed_job(conn)
    request_id = create_request(conn, job_id, "notes")
    resolve_request(conn, request_id)
    row = conn.execute(
        "SELECT * FROM events WHERE event_type = 'regen_completed'").fetchone()
    assert row is not None
    assert row["job_id"] == job_id


def test_fail_marks_failed_with_detail(conn):
    job_id, _ = seed_job(conn)
    request_id = create_request(conn, job_id, "notes")
    fail_request(conn, request_id, "tailoring failed after 3 attempts")
    assert pending_requests(conn) == []  # no longer pending
    row = conn.execute("SELECT * FROM regen_requests WHERE id = ?",
                       (request_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["resolved_at"] is not None
    assert row["detail"] == "tailoring failed after 3 attempts"


def test_fail_logs_failed_event_with_reason(conn):
    job_id, _ = seed_job(conn)
    request_id = create_request(conn, job_id, "notes")
    fail_request(conn, request_id, "fill floor unreachable")
    row = conn.execute(
        "SELECT * FROM events WHERE event_type = 'regen_failed'").fetchone()
    assert row is not None
    assert row["job_id"] == job_id
    assert row["detail"] == "fill floor unreachable"


def test_fail_unknown_id_raises(conn):
    with pytest.raises(ValueError):
        fail_request(conn, 99, "reason")


def test_fail_rejects_empty_reason(conn):
    job_id, _ = seed_job(conn)
    request_id = create_request(conn, job_id, "notes")
    with pytest.raises(ValueError):
        fail_request(conn, request_id, "   ")


def test_fail_only_pending(conn):
    # A request already resolved cannot be re-marked failed (mirrors resolve).
    job_id, _ = seed_job(conn)
    request_id = create_request(conn, job_id, "notes")
    resolve_request(conn, request_id)
    with pytest.raises(ValueError):
        fail_request(conn, request_id, "too late")


def test_latest_request_returns_newest_of_any_status(conn):
    job_id, _ = seed_job(conn)
    first = create_request(conn, job_id, "first")
    fail_request(conn, first, "no good")
    second = create_request(conn, job_id, "second", creativity="bold")
    latest = latest_request(conn, job_id)
    assert latest["id"] == second
    assert latest["status"] == "pending"
    assert latest["creativity"] == "bold"
    assert latest["notes"] == "second"
    assert "detail" in latest and "resolved_at" in latest


def test_latest_request_none_when_no_requests(conn):
    job_id, _ = seed_job(conn)
    assert latest_request(conn, job_id) is None


def test_latest_request_surfaces_failure_detail(conn):
    job_id, _ = seed_job(conn)
    request_id = create_request(conn, job_id, "notes")
    fail_request(conn, request_id, "fill floor unreachable")
    latest = latest_request(conn, job_id)
    assert latest["id"] == request_id
    assert latest["status"] == "failed"
    assert latest["detail"] == "fill floor unreachable"


def test_fail_cli_requires_reason(monkeypatch):
    # The morning-run skill calls `--fail ID --reason "..."`; a bare `--fail`
    # must be rejected before any DB access (parser.error → SystemExit).
    import sys

    from pipeline import regen

    monkeypatch.setattr(sys, "argv", ["regen", "--fail", "5"])
    with pytest.raises(SystemExit):
        regen.main()


def test_fail_cli_marks_failed(monkeypatch, tmp_path):
    import sys

    from pipeline import paths, regen
    from pipeline.db import connect, init_db

    db = tmp_path / "cli.db"
    monkeypatch.setattr(paths, "DB_PATH", db)
    setup = connect(db)
    init_db(setup)
    job_id, _ = seed_job(setup)
    request_id = create_request(setup, job_id, "notes")
    setup.close()

    monkeypatch.setattr(
        sys, "argv",
        ["regen", "--fail", str(request_id), "--reason", "tailoring failed"])
    regen.main()

    check = connect(db)
    row = check.execute("SELECT status, detail FROM regen_requests WHERE id = ?",
                        (request_id,)).fetchone()
    check.close()
    assert row["status"] == "failed"
    assert row["detail"] == "tailoring failed"


def test_creativity_levels_are_the_three_supported(conn):
    assert CREATIVITY_LEVELS == ("conservative", "balanced", "bold")


def test_create_stores_creativity(conn):
    job_id, _ = seed_job(conn)
    create_request(conn, job_id, "Be bold for max JD match", creativity="bold")
    assert pending_requests(conn)[0]["creativity"] == "bold"


def test_create_defaults_creativity_to_balanced(conn):
    job_id, _ = seed_job(conn)
    create_request(conn, job_id, "Standard tailoring")
    assert pending_requests(conn)[0]["creativity"] == "balanced"


def test_create_normalizes_unknown_creativity_to_balanced(conn):
    # An out-of-range value (e.g. a hallucinated copilot arg) must not reach the
    # DB verbatim — it is normalized to the safe default so the run can read it.
    job_id, _ = seed_job(conn)
    create_request(conn, job_id, "notes", creativity="wildly-creative")
    assert pending_requests(conn)[0]["creativity"] == "balanced"
