"""Follow-up engine (spec §9).

Surfaces applications that need a nudge and records snooze/dismiss overrides.
`due_followups` feeds the overview Follow-ups panel; `set_override` is the write
side for snooze/dismiss; `draft_followup` builds a deterministic, keyless,
mandate-gated draft from the recruiter templates (no LLM call).

All timing takes an explicit `now` (injectable clock, spec §4.2).
"""
from datetime import datetime, timedelta

from pipeline.gmail_recruiter import FOLLOWUP_EN, FOLLOWUP_FR, submitted_clause
from server import mandate

# Applied with no recruiter reply for this many days -> nudge the recruiter.
# Aligned with pipeline.gmail_recruiter's 7-day default.
APPLIED_NO_REPLY_DAYS = 7
# Recruiter replied but no outbound follow-up went out for this many days ->
# the ball is in our court; a shorter fuse than a cold application.
RECRUITER_NO_OUTBOUND_DAYS = 2

_UNSET = object()


class FollowupNotFound(Exception):
    """Raised when a draft is requested for a job with no application."""


def _days_between(ts: str, now: datetime) -> int:
    return (now.date() - datetime.fromisoformat(ts).date()).days


def _overrides(conn) -> dict:
    rows = conn.execute(
        "SELECT application_id, snooze_until, dismissed FROM followup_overrides"
    ).fetchall()
    return {r["application_id"]: r for r in rows}


def _suppressed(ov: dict, application_id: int, now: datetime) -> bool:
    row = ov.get(application_id)
    if row is None:
        return False
    if row["dismissed"]:
        return True
    snooze = row["snooze_until"]
    return bool(snooze and snooze > now.isoformat(timespec="seconds"))


def due_followups(conn, *, now: datetime) -> list[dict]:
    """Applications needing a follow-up, most overdue first.

    Two kinds, both honouring followup_overrides (dismissed hides; an unexpired
    snooze hides):
    - applied_no_reply: status Applied, submitted past APPLIED_NO_REPLY_DAYS,
      no recruiter reply yet.
    - recruiter_no_outbound: status Recruiter reply whose last reply is past
      RECRUITER_NO_OUTBOUND_DAYS with no outbound follow-up sent since.
    """
    ov = _overrides(conn)
    out: list[dict] = []

    cutoff = (now - timedelta(days=APPLIED_NO_REPLY_DAYS)).isoformat(timespec="seconds")
    for r in conn.execute(
        "SELECT a.id AS application_id, j.id AS job_id, j.company, j.title,"
        " a.submitted_at"
        " FROM applications a JOIN jobs j ON j.id = a.job_id"
        " WHERE a.status = 'Applied'"
        "   AND a.submitted_at IS NOT NULL AND a.submitted_at < ?",
        (cutoff,)).fetchall():
        if _suppressed(ov, r["application_id"], now):
            continue
        out.append({
            "kind": "applied_no_reply",
            "application_id": r["application_id"],
            "job_id": r["job_id"],
            "company": r["company"],
            "title": r["title"],
            "days": _days_between(r["submitted_at"], now),
            "since": r["submitted_at"][:10],
        })

    cutoff2 = (now - timedelta(days=RECRUITER_NO_OUTBOUND_DAYS)).isoformat(timespec="seconds")
    for r in conn.execute(
        "SELECT a.id AS application_id, j.id AS job_id, j.company, j.title,"
        " MAX(e.created_at) AS reply_at"
        " FROM applications a JOIN jobs j ON j.id = a.job_id"
        " JOIN events e ON e.application_id = a.id"
        " WHERE a.status = 'Recruiter reply'"
        "   AND e.event_type = 'status_change'"
        "   AND e.detail = '-> Recruiter reply'"
        " GROUP BY a.id HAVING reply_at < ?",
        (cutoff2,)).fetchall():
        app_id = r["application_id"]
        if _suppressed(ov, app_id, now):
            continue
        sent_since = conn.execute(
            "SELECT COUNT(*) FROM events WHERE application_id = ?"
            "   AND event_type = 'followup_sent' AND created_at > ?",
            (app_id, r["reply_at"])).fetchone()[0]
        if sent_since:
            continue
        out.append({
            "kind": "recruiter_no_outbound",
            "application_id": app_id,
            "job_id": r["job_id"],
            "company": r["company"],
            "title": r["title"],
            "days": _days_between(r["reply_at"], now),
            "since": r["reply_at"][:10],
        })

    out.sort(key=lambda f: f["days"], reverse=True)
    return out


def set_override(conn, application_id: int, *, snooze_until=_UNSET,
                 dismissed=_UNSET, now: datetime) -> None:
    """Upsert a snooze/dismiss override. Only the fields passed change; omitted
    fields keep their stored value (sentinel-guarded partial update)."""
    existing = conn.execute(
        "SELECT snooze_until, dismissed FROM followup_overrides"
        " WHERE application_id = ?", (application_id,)).fetchone()
    cur_snooze = existing["snooze_until"] if existing else None
    cur_dismissed = existing["dismissed"] if existing else 0
    new_snooze = cur_snooze if snooze_until is _UNSET else snooze_until
    new_dismissed = cur_dismissed if dismissed is _UNSET else int(dismissed)
    conn.execute(
        "INSERT INTO followup_overrides"
        " (application_id, snooze_until, dismissed, updated_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(application_id) DO UPDATE SET"
        " snooze_until = excluded.snooze_until,"
        " dismissed = excluded.dismissed,"
        " updated_at = excluded.updated_at",
        (application_id, new_snooze, new_dismissed,
         now.isoformat(timespec="seconds")))
    conn.commit()


def draft_followup(conn, job_id: int, *, name: str = "", phone: str = "", email: str = "") -> dict:
    """Deterministic, keyless follow-up draft for a job, run through the mandate
    gate. Returns subject/body/language plus the gate verdict (mandate_ok,
    flags). No LLM call — the body is a filled recruiter template."""
    row = conn.execute(
        "SELECT a.submitted_at, j.title, j.company, j.language"
        " FROM jobs j JOIN applications a ON a.job_id = j.id"
        " WHERE j.id = ?", (job_id,)).fetchone()
    if row is None:
        raise FollowupNotFound(job_id)
    lang = (row["language"] or "fr").lower()
    template = FOLLOWUP_FR if lang == "fr" else FOLLOWUP_EN
    submitted_date = (row["submitted_at"] or "")[:10]
    raw = template.format(
        name=name, title=row["title"], company=row["company"],
        submitted_clause=submitted_clause(submitted_date, lang),
        phone=phone, email=email)
    subject = (
        f"Suivi candidature — {row['title']} ({row['company']})"
        if lang == "fr"
        else f"Follow-up: {row['title']} application at {row['company']}")
    safe = mandate.apply_fixes(raw)
    verdict = mandate.check_prose(safe)
    return {
        "subject": subject,
        "body": safe,
        "language": lang,
        "mandate_ok": verdict.ok,
        "flags": verdict.violations,
    }
