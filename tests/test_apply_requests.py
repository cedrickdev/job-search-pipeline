import pytest

from pipeline import apply_requests as ar
from tests.helpers import seed_job


def test_create_request_returns_id(conn):
    job_id, _ = seed_job(conn)
    rid = ar.create_request(conn, job_id)
    assert isinstance(rid, int)
    row = conn.execute(
        "SELECT status FROM apply_requests WHERE id = ?", (rid,)
    ).fetchone()
    assert row["status"] == "pending"


def test_create_request_is_idempotent_while_non_terminal(conn):
    job_id, _ = seed_job(conn)
    rid1 = ar.create_request(conn, job_id)
    rid2 = ar.create_request(conn, job_id)
    assert rid1 == rid2
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM apply_requests WHERE job_id = ?", (job_id,)
    ).fetchone()["n"]
    assert count == 1


def test_create_request_unknown_job_raises(conn):
    with pytest.raises(ValueError):
        ar.create_request(conn, 999999)


def test_create_request_new_after_terminal(conn):
    job_id, _ = seed_job(conn)
    rid1 = ar.create_request(conn, job_id)
    ar.resolve_request(conn, rid1, "failed", "boom")
    rid2 = ar.create_request(conn, job_id)
    assert rid2 != rid1


def test_create_or_get_request_reports_created_flag(conn):
    job_id, _ = seed_job(conn)
    rid1, created1 = ar.create_or_get_request(conn, job_id)
    assert created1 is True
    # A second call while the first is still non-terminal returns the SAME id
    # with created=False, so the caller does not dispatch a second worker.
    rid2, created2 = ar.create_or_get_request(conn, job_id)
    assert rid2 == rid1
    assert created2 is False
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM apply_requests WHERE job_id = ?", (job_id,)
    ).fetchone()["n"]
    assert count == 1
    # Once the in-flight request is terminal, the next call creates a fresh row.
    ar.resolve_request(conn, rid1, "failed", "x")
    rid3, created3 = ar.create_or_get_request(conn, job_id)
    assert rid3 != rid1
    assert created3 is True


def test_claim_transitions_and_logs_event(conn):
    job_id, _ = seed_job(conn)
    rid = ar.create_request(conn, job_id)
    ar.claim_request(conn, rid, "linkedin")
    row = conn.execute(
        "SELECT status, channel FROM apply_requests WHERE id = ?", (rid,)
    ).fetchone()
    assert row["status"] == "in_progress"
    assert row["channel"] == "linkedin"
    events = [
        e["event_type"]
        for e in conn.execute(
            "SELECT event_type FROM events WHERE job_id = ?", (job_id,)
        ).fetchall()
    ]
    assert "apply_started" in events


def test_claim_non_pending_raises(conn):
    job_id, _ = seed_job(conn)
    rid = ar.create_request(conn, job_id)
    ar.claim_request(conn, rid, "linkedin")
    with pytest.raises(ValueError):
        ar.claim_request(conn, rid, "linkedin")


@pytest.mark.parametrize(
    "status,event",
    [
        ("applied", "apply_applied"),
        ("needs_you", "apply_needs_you"),
        ("failed", "apply_failed"),
    ],
)
def test_resolve_sets_terminal_and_logs(conn, status, event):
    job_id, _ = seed_job(conn)
    rid = ar.create_request(conn, job_id)
    ar.claim_request(conn, rid, "wtj")
    ar.resolve_request(conn, rid, status, "a reason", "/tmp/shot.png")
    row = conn.execute(
        "SELECT status, detail, screenshot_path, resolved_at"
        " FROM apply_requests WHERE id = ?",
        (rid,),
    ).fetchone()
    assert row["status"] == status
    assert row["detail"] == "a reason"
    assert row["screenshot_path"] == "/tmp/shot.png"
    assert row["resolved_at"] is not None
    events = [
        e["event_type"]
        for e in conn.execute(
            "SELECT event_type FROM events WHERE job_id = ?", (job_id,)
        ).fetchall()
    ]
    assert event in events


def test_resolve_accepts_pending_without_claim(conn):
    # The lock-busy path resolves needs_you while still pending.
    job_id, _ = seed_job(conn)
    rid = ar.create_request(conn, job_id)
    ar.resolve_request(conn, rid, "needs_you", "Browser in use.")
    row = conn.execute(
        "SELECT status FROM apply_requests WHERE id = ?", (rid,)
    ).fetchone()
    assert row["status"] == "needs_you"


def test_resolve_bad_target_status_raises(conn):
    job_id, _ = seed_job(conn)
    rid = ar.create_request(conn, job_id)
    with pytest.raises(ValueError):
        ar.resolve_request(conn, rid, "in_progress")


def test_resolve_unknown_id_raises(conn):
    with pytest.raises(ValueError):
        ar.resolve_request(conn, 999999, "applied")


def test_resolve_already_terminal_raises(conn):
    job_id, _ = seed_job(conn)
    rid = ar.create_request(conn, job_id)
    ar.resolve_request(conn, rid, "applied")
    with pytest.raises(ValueError):
        ar.resolve_request(conn, rid, "failed")


def test_latest_request_returns_newest(conn):
    job_id, _ = seed_job(conn)
    rid1 = ar.create_request(conn, job_id)
    ar.resolve_request(conn, rid1, "failed", "first")
    rid2 = ar.create_request(conn, job_id)
    latest = ar.latest_request(conn, job_id)
    assert latest["id"] == rid2
    assert latest["status"] == "pending"


def test_latest_request_none_when_absent(conn):
    job_id, _ = seed_job(conn)
    assert ar.latest_request(conn, job_id) is None


def test_pending_requests_returns_only_pending(conn):
    j1, _ = seed_job(conn)
    j2, _ = seed_job(conn)
    j3, _ = seed_job(conn)
    r1 = ar.create_request(conn, j1)            # stays pending
    r2 = ar.create_request(conn, j2)
    ar.claim_request(conn, r2, "lever")         # in_progress -> excluded
    r3 = ar.create_request(conn, j3)
    ar.resolve_request(conn, r3, "failed", "x")  # terminal -> excluded
    pending = ar.pending_requests(conn)
    assert [p["id"] for p in pending] == [r1]
    assert {"id", "job_id", "created_at", "company", "title"} <= set(pending[0].keys())
