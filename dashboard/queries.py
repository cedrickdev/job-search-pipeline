"""Core read queries shared by the FastAPI routes: cards, board, digest.

CARD_SELECT is the single source of truth for a "job card" row (used by the
queue, board, replies, and jobs-table endpoints). It aliases the latest score
and latest cv_version onto each application/job pair so every consumer sees
the same columns without repeating the correlated subqueries.
"""
import json
import sqlite3

from pipeline.statuses import STATUSES

CARD_SELECT = """
SELECT
    a.id AS application_id,
    j.id AS job_id,
    j.company AS company,
    j.title AS title,
    j.source AS source,
    j.track AS track,
    a.status AS status,
    j.url AS url,
    j.language AS language,
    ls.score AS score,
    ls.red_flags AS red_flags,
    cv.phone_screen_pct AS phone_screen_pct
FROM applications a
JOIN jobs j ON j.id = a.job_id
LEFT JOIN (
    SELECT s.job_id, s.score, s.red_flags
    FROM scores s
    JOIN (SELECT job_id, MAX(id) AS max_id FROM scores GROUP BY job_id) latest
        ON latest.max_id = s.id
) ls ON ls.job_id = j.id
LEFT JOIN (
    SELECT c.job_id, c.phone_screen_pct
    FROM cv_versions c
    JOIN (SELECT job_id, MAX(id) AS max_id FROM cv_versions GROUP BY job_id) latest
        ON latest.max_id = c.id
) cv ON cv.job_id = j.id
"""


def queue_cards(conn: sqlite3.Connection) -> list[dict]:
    """Jobs ready to apply, highest score first."""
    rows = conn.execute(
        CARD_SELECT + " WHERE a.status = 'Ready to apply' ORDER BY score DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def borderline_cards(conn: sqlite3.Connection) -> list[dict]:
    """Jobs scored 50-69, awaiting a manual call."""
    rows = conn.execute(
        CARD_SELECT + " WHERE a.status = 'Borderline' ORDER BY score DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def auto_approved_today(conn: sqlite3.Connection) -> list[dict]:
    """Applications the auto-approver moved to Approved today.

    `events.created_at` is written in naive local time (pipeline.statuses._now),
    so "today" must also be local: bare `date('now')` is UTC and would drop
    every approval made between local midnight and the UTC day rollover.
    Matches pipeline.auto_approve.approved_today_count, which uses date.today().
    """
    rows = conn.execute(
        CARD_SELECT
        + " JOIN events e ON e.application_id = a.id"
        " WHERE a.status = 'Approved'"
        "   AND e.event_type = 'status_change'"
        "   AND e.source = 'auto-approve'"
        "   AND e.detail LIKE '-> Approved%'"
        "   AND date(e.created_at) = date('now', 'localtime')"
        " GROUP BY a.id"
        " ORDER BY a.id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def board(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Cards grouped by status, in pipeline order, empty columns omitted."""
    rows = [dict(r) for r in conn.execute(CARD_SELECT + " ORDER BY a.id DESC")]
    columns: dict[str, list[dict]] = {}
    for status in STATUSES:
        matching = [row for row in rows if row["status"] == status]
        if matching:
            columns[status] = matching
    return columns


def digest_header(conn: sqlite3.Connection) -> dict:
    """Last run timestamp, per-source health, and current status counts."""
    run = conn.execute(
        "SELECT finished_at, summary FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_run = None
    health: dict = {}
    if run is not None:
        last_run = run["finished_at"]
        if run["summary"]:
            health = json.loads(run["summary"]).get("health", {})
    counts = {
        r["status"]: r["count"]
        for r in conn.execute(
            "SELECT status, COUNT(*) AS count FROM applications GROUP BY status"
        )
    }
    return {"last_run": last_run, "health": health, "counts": counts}


def job_detail(conn: sqlite3.Connection, job_id: int) -> dict | None:
    """Everything about one job: the job, its application, score, and CVs."""
    job_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job_row is None:
        return None
    application_row = conn.execute(
        "SELECT * FROM applications WHERE job_id = ?", (job_id,)
    ).fetchone()
    score_row = conn.execute(
        "SELECT * FROM scores WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()
    cv_versions: dict = {"en": None, "fr": None}
    for lang in ("en", "fr"):
        cv_row = conn.execute(
            "SELECT * FROM cv_versions WHERE job_id = ? AND language = ?"
            " ORDER BY id DESC LIMIT 1",
            (job_id, lang),
        ).fetchone()
        if cv_row is not None:
            cv_versions[lang] = dict(cv_row)
    return {
        "job": dict(job_row),
        "application": dict(application_row) if application_row else None,
        "score": dict(score_row) if score_row else None,
        "cv_versions": cv_versions,
    }
