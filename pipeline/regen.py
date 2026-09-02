"""Regeneration request queue: the dashboard writes, the morning run consumes."""
import argparse
import json
import sqlite3
from datetime import datetime

from pipeline import paths
from pipeline.db import connect, init_db
from pipeline.statuses import log_event


# How aggressively the CV should be re-tailored. The copilot infers the level
# from the user's wording ("be bold" / "play it safe"); the morning run reads it
# back from the queued request to set the tailoring tone (spec §6.1).
CREATIVITY_LEVELS = ("conservative", "balanced", "bold")
_DEFAULT_CREATIVITY = "balanced"


def normalize_creativity(value: str | None) -> str:
    """Clamp any creativity value to a supported level. An unknown value (e.g. a
    hallucinated copilot arg) falls back to the safe default rather than reaching
    the DB verbatim."""
    return value if value in CREATIVITY_LEVELS else _DEFAULT_CREATIVITY


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_request(conn: sqlite3.Connection, job_id: int, notes: str,
                   creativity: str = _DEFAULT_CREATIVITY) -> int:
    notes = notes.strip()
    if not notes:
        raise ValueError("notes must not be empty")
    if conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise ValueError(f"No job with id={job_id}")
    cur = conn.execute(
        "INSERT INTO regen_requests (job_id, notes, status, created_at, creativity)"
        " VALUES (?, ?, 'pending', ?, ?)",
        (job_id, notes, _now(), normalize_creativity(creativity)))
    log_event(conn, "regen_requested", "dashboard", notes, job_id=job_id)
    conn.commit()
    return cur.lastrowid


def pending_requests(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT r.id, r.job_id, r.notes, r.creativity, r.created_at, j.company, j.title"
        " FROM regen_requests r JOIN jobs j ON j.id = r.job_id"
        " WHERE r.status = 'pending' ORDER BY r.id").fetchall()
    return [dict(r) for r in rows]


def resolve_request(conn: sqlite3.Connection, request_id: int) -> None:
    """Mark a pending request done and log a job-keyed completion event so the
    dashboard has a positive "regeneration applied" signal (not just a vanishing
    chip)."""
    job_id = _pending_job_id(conn, request_id)
    conn.execute(
        "UPDATE regen_requests SET status = 'done', resolved_at = ?"
        " WHERE id = ?", (_now(), request_id))
    log_event(conn, "regen_completed", "run", job_id=job_id)
    conn.commit()


def fail_request(conn: sqlite3.Connection, request_id: int, reason: str) -> None:
    """Mark a pending request failed, recording the reason so the dashboard can
    distinguish a genuine failure from a still-queued request. Logs a job-keyed
    ``regen_failed`` event carrying the reason."""
    reason = reason.strip()
    if not reason:
        raise ValueError("reason must not be empty")
    job_id = _pending_job_id(conn, request_id)
    conn.execute(
        "UPDATE regen_requests SET status = 'failed', resolved_at = ?, detail = ?"
        " WHERE id = ?", (_now(), reason, request_id))
    log_event(conn, "regen_failed", "run", reason, job_id=job_id)
    conn.commit()


def latest_request(conn: sqlite3.Connection, job_id: int) -> dict | None:
    """The most recent request for a job, of any status (or None). Drives the
    drawer's failed banner and done confirmation; `pending_requests` still drives
    the queued/running affordance."""
    row = conn.execute(
        "SELECT id, status, notes, creativity, created_at, resolved_at, detail"
        " FROM regen_requests WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,)).fetchone()
    return dict(row) if row is not None else None


def _pending_job_id(conn: sqlite3.Connection, request_id: int) -> int:
    """Return the job_id of a pending request, raising if it is unknown or has
    already been resolved/failed (the transition only applies once)."""
    row = conn.execute(
        "SELECT job_id FROM regen_requests WHERE id = ? AND status = 'pending'",
        (request_id,)).fetchone()
    if row is None:
        raise ValueError(f"No pending request with id={request_id}")
    return row["job_id"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Regeneration request queue")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pending", action="store_true",
                       help="print pending requests as JSON")
    group.add_argument("--resolve", type=int, metavar="ID",
                       help="mark a request done")
    group.add_argument("--fail", type=int, metavar="ID",
                       help="mark a request failed (requires --reason)")
    parser.add_argument("--reason", type=str,
                        help="failure reason, required with --fail")
    args = parser.parse_args()
    if args.fail is not None and not (args.reason and args.reason.strip()):
        parser.error("--fail requires a non-empty --reason")
    conn = connect(paths.DB_PATH)
    init_db(conn)
    if args.pending:
        print(json.dumps(pending_requests(conn), indent=2))
    elif args.fail is not None:
        fail_request(conn, args.fail, args.reason)
        print(json.dumps({"failed": args.fail, "reason": args.reason.strip()}))
    else:
        resolve_request(conn, args.resolve)
        print(json.dumps({"resolved": args.resolve}))


if __name__ == "__main__":
    main()
