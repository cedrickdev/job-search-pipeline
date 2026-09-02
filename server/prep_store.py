"""Persistence for the prep workspace: notes, cached prep content, interview log."""
import json
from datetime import datetime


def get_prep(conn, application_id: int) -> dict:
    notes_row = conn.execute(
        "SELECT notes_md FROM prep_notes WHERE application_id=?", (application_id,)).fetchone()
    cache_row = conn.execute(
        "SELECT likely_questions_json, company_research_json, talking_points_json, generated_at "
        "FROM prep_cache WHERE application_id=?", (application_id,)).fetchone()

    def _load(value):
        return json.loads(value) if value else None

    return {
        "notes_md": notes_row["notes_md"] if notes_row else "",
        "likely_questions": _load(cache_row["likely_questions_json"]) if cache_row else None,
        "company_research": _load(cache_row["company_research_json"]) if cache_row else None,
        "talking_points": _load(cache_row["talking_points_json"]) if cache_row else None,
        "generated_at": cache_row["generated_at"] if cache_row else None,
        "interviews": list_interviews(conn, application_id),
    }


def upsert_notes(conn, application_id: int, notes_md: str, *, now: datetime) -> None:
    conn.execute(
        "INSERT INTO prep_notes (application_id, notes_md, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(application_id) DO UPDATE SET notes_md=excluded.notes_md, updated_at=excluded.updated_at",
        (application_id, notes_md, now.isoformat()))
    conn.commit()


def set_prep_cache(conn, application_id: int, *, likely_questions, company_research,
                   talking_points, now: datetime) -> None:
    conn.execute(
        "INSERT INTO prep_cache (application_id, likely_questions_json, company_research_json, "
        "talking_points_json, generated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(application_id) DO UPDATE SET "
        "likely_questions_json=excluded.likely_questions_json, "
        "company_research_json=excluded.company_research_json, "
        "talking_points_json=excluded.talking_points_json, generated_at=excluded.generated_at",
        (application_id, json.dumps(likely_questions), json.dumps(company_research),
         json.dumps(talking_points), now.isoformat()))
    conn.commit()


def add_interview(conn, application_id: int, *, round_label: str, scheduled_for, notes,
                  now: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO interview_log (application_id, round_label, scheduled_for, outcome, notes, created_at) "
        "VALUES (?, ?, ?, NULL, ?, ?)",
        (application_id, round_label, scheduled_for, notes, now.isoformat()))
    conn.commit()
    return cur.lastrowid


def update_interview(conn, interview_id: int, **fields) -> None:
    allowed = {"round_label", "scheduled_for", "outcome", "notes"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    assignments = ", ".join(f"{k}=?" for k in sets)
    conn.execute(
        f"UPDATE interview_log SET {assignments} WHERE id=?",
        (*sets.values(), interview_id))
    conn.commit()


def list_interviews(conn, application_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, round_label, scheduled_for, outcome, notes, created_at "
        "FROM interview_log WHERE application_id=? ORDER BY COALESCE(scheduled_for, created_at)",
        (application_id,)).fetchall()
    return [dict(r) for r in rows]


def upcoming_interviews(conn, *, now: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT il.id, il.round_label, il.scheduled_for, j.company, j.title, a.job_id "
        "FROM interview_log il "
        "JOIN applications a ON a.id = il.application_id "
        "JOIN jobs j ON j.id = a.job_id "
        "WHERE il.outcome IS NULL AND il.scheduled_for IS NOT NULL AND il.scheduled_for >= ? "
        "ORDER BY il.scheduled_for",
        (now.isoformat(),)).fetchall()
    return [dict(r) for r in rows]
