"""Apply-now request queue: the webapp enqueues, a background worker consumes.

Mirrors pipeline/regen.py. One apply request drives one background apply for a
single job through a pending -> in_progress -> applied|needs_you|failed
lifecycle. The worker (pipeline.apply_one) is the only event author.
"""
from datetime import datetime

from pipeline.statuses import log_event

_SOURCE = "applier"
_TERMINAL = ("applied", "needs_you", "failed")
_EVENT = {
    "applied": "apply_applied",
    "needs_you": "apply_needs_you",
    "failed": "apply_failed",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _insert_or_get_pending(conn, job_id):
    """Insert a pending apply request, or return the existing non-terminal one.

    Returns ``(request_id, created)`` where ``created`` is True only when this
    call inserted the row. The check-and-insert is a single
    ``INSERT ... WHERE NOT EXISTS`` statement, so two concurrent callers cannot
    both create a row for the same job: the loser inserts zero rows and falls
    through to the SELECT, which runs in the same transaction as that zero-row
    insert and so reads a consistent snapshot.
    """
    if conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise ValueError(f"No job with id={job_id}")
    cur = conn.execute(
        "INSERT INTO apply_requests (job_id, status, created_at)"
        " SELECT ?, 'pending', ?"
        " WHERE NOT EXISTS ("
        "   SELECT 1 FROM apply_requests"
        "   WHERE job_id = ? AND status IN ('pending', 'in_progress'))",
        (job_id, _now(), job_id),
    )
    created = cur.rowcount == 1
    if created:
        request_id = cur.lastrowid
    else:
        request_id = conn.execute(
            "SELECT id FROM apply_requests WHERE job_id = ?"
            " AND status IN ('pending', 'in_progress') ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()["id"]
    conn.commit()
    return request_id, created


def create_request(conn, job_id):
    """Insert a pending apply request, returning its id. Idempotent: if a
    non-terminal request already exists for the job, return that id instead."""
    request_id, _ = _insert_or_get_pending(conn, job_id)
    return request_id


def create_or_get_request(conn, job_id):
    """Like create_request, but also reports whether the row was newly created.

    Returns ``(request_id, created)``. ``created`` is True only when this call
    inserted a new pending row; False when an in-flight (pending/in_progress)
    request already existed and its id was returned. Callers that dispatch a
    worker use ``created`` to avoid spawning a second worker for a request that
    already has one, without a separate read that could race the insert.
    """
    return _insert_or_get_pending(conn, job_id)


def pending_requests(conn):
    rows = conn.execute(
        "SELECT r.id, r.job_id, r.created_at, j.company, j.title"
        " FROM apply_requests r JOIN jobs j ON j.id = r.job_id"
        " WHERE r.status = 'pending' ORDER BY r.id"
    ).fetchall()
    return [dict(r) for r in rows]


def claim_request(conn, request_id, channel):
    """pending -> in_progress, set channel, log apply_started (job-keyed)."""
    row = conn.execute(
        "SELECT job_id FROM apply_requests WHERE id = ? AND status = 'pending'",
        (request_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"No pending apply request with id={request_id}")
    conn.execute(
        "UPDATE apply_requests SET status = 'in_progress', channel = ? WHERE id = ?",
        (channel, request_id),
    )
    log_event(conn, "apply_started", _SOURCE, channel, job_id=row["job_id"])
    conn.commit()


def resolve_request(conn, request_id, status, detail=None, screenshot_path=None):
    """Non-terminal (pending or in_progress) -> applied|needs_you|failed.

    Accepts pending so the lock-busy path can resolve needs_you without
    claiming. ValueError on a bad target status, unknown id, or an
    already-terminal request.
    """
    if status not in _TERMINAL:
        raise ValueError(f"Invalid target status: {status!r}")
    row = conn.execute(
        "SELECT job_id, status FROM apply_requests WHERE id = ?", (request_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"No apply request with id={request_id}")
    if row["status"] not in ("pending", "in_progress"):
        raise ValueError(
            f"apply request {request_id} already resolved ({row['status']})"
        )
    conn.execute(
        "UPDATE apply_requests SET status = ?, detail = ?, screenshot_path = ?,"
        " resolved_at = ? WHERE id = ?",
        (status, detail, screenshot_path, _now(), request_id),
    )
    log_event(conn, _EVENT[status], _SOURCE, detail, job_id=row["job_id"])
    conn.commit()


def latest_request(conn, job_id):
    row = conn.execute(
        "SELECT id, status, channel, detail, screenshot_path, created_at, resolved_at"
        " FROM apply_requests WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return dict(row) if row is not None else None
