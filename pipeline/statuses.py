"""Application lifecycle: statuses, transitions, append-only event log."""
import sqlite3
from datetime import datetime

STATUSES = [
    "Discovered", "Scored", "Borderline", "Ready to apply", "Approved",
    "Applied", "Phone Screen", "Needs you", "Duplicate", "Recruiter reply",
    "Interview scheduled", "Offer", "Rejected", "Ghosted", "Withdrawn",
    "Archived",
]

STATUS_LEGEND = {
    "Discovered": "Found by a discovery sweep, not yet scored.",
    "Scored": "Scored by Claude; tailoring pending or below threshold.",
    "Borderline": "Score 50-69: visible for manual review, no CV generated.",
    "Ready to apply": "Strong match; tailored CV + cover letter generated, awaiting GO.",
    "Approved": "You clicked GO; queued for the applier.",
    "Applied": "Application submitted, confirmation recorded.",
    "Phone Screen": "Recruiter or hiring manager reached out for a phone/video screen.",
    "Needs you": "Applier hit an obstacle (captcha, odd question); finish manually.",
    "Duplicate": "Another opening at the same company; we applied to the best-matching role instead.",
    "Recruiter reply": "A recruiter responded or reached out (pre-screen).",
    "Interview scheduled": "Interview booked or being scheduled.",
    "Offer": "Offer received.",
    "Rejected": "Rejected by the company.",
    "Ghosted": "No response despite follow-up.",
    "Withdrawn": "You withdrew the application.",
    "Archived": "Scored below 50 or no longer relevant.",
}

# Maps status -> which applications column to stamp with the current timestamp.
# Only first-entry semantics: we never overwrite a non-NULL value.
_STATUS_TIMESTAMP_COL: dict[str, str] = {
    "Discovered":           "created_at",
    "Scored":               "scored_at",
    "Borderline":           "scored_at",
    "Archived":             "scored_at",
    "Ready to apply":       "tailored_at",
    "Approved":             "approved_at",
    "Phone Screen":         "phone_screen_at",
    "Recruiter reply":      "phone_screen_at",
    "Interview scheduled":  "interview_at",
    "Offer":                "offer_at",
    "Rejected":             "rejected_at",
    "Ghosted":              "rejected_at",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_event(conn, event_type: str, source: str, detail: str | None = None,
              application_id: int | None = None, job_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO events (application_id, job_id, event_type, detail, source, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (application_id, job_id, event_type, detail, source, _now()),
    )


def _stamp(conn: sqlite3.Connection, application_id: int, status: str,
           ts: str) -> None:
    """Write ts into the lifecycle column for status, only if still NULL."""
    col = _STATUS_TIMESTAMP_COL.get(status)
    if col:
        conn.execute(
            f"UPDATE applications SET {col} = ? WHERE id = ? AND {col} IS NULL",
            (ts, application_id),
        )


def create_application(conn: sqlite3.Connection, job_id: int, source: str,
                       status: str = "Discovered") -> int:
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status!r}")
    ts = _now()
    cur = conn.execute(
        "INSERT INTO applications (job_id, status, created_at) VALUES (?, ?, ?)",
        (job_id, status, ts),
    )
    app_id = cur.lastrowid
    _stamp(conn, app_id, status, ts)
    log_event(conn, "status_change", source, f"-> {status}",
              application_id=app_id, job_id=job_id)
    conn.commit()
    return app_id


def set_status(conn: sqlite3.Connection, application_id: int, status: str,
               source: str, detail: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status!r}")
    ts = _now()
    cur = conn.execute("UPDATE applications SET status = ? WHERE id = ?",
                       (status, application_id))
    if cur.rowcount == 0:
        raise ValueError(f"No application with id={application_id}")
    _stamp(conn, application_id, status, ts)
    log_event(conn, "status_change", source,
              detail or f"-> {status}", application_id=application_id)
    conn.commit()


def mark_applied(conn: sqlite3.Connection, application_id: int,
                 channel: str = "manual") -> None:
    ts = _now()
    set_status(conn, application_id, "Applied", source="dashboard")
    conn.execute(
        "UPDATE applications SET submitted_at = ?, channel = ? WHERE id = ?",
        (ts, channel, application_id))
    conn.commit()
