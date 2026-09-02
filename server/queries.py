"""Read-side aggregations for the command-center API. Read-only.

Time-windowed metrics take an explicit `now` (injectable clock, spec §4.2).
"""
import sqlite3
from datetime import datetime, timedelta

from dashboard import queries as dq
from pipeline import apply_requests, gap_analysis, paths, regen
from server import followups, prep_store

PHONE_SCREEN_TARGET = 90
VELOCITY_GOAL = 5
VELOCITY_WINDOW_DAYS = 7

# Current-status models for the rate KPIs (single-user tracker; documented).
APPLIED_OR_BEYOND = ("Applied", "Needs you", "Recruiter reply",
                     "Interview scheduled", "Offer", "Rejected",
                     "Ghosted", "Withdrawn")
REPLIED = ("Recruiter reply", "Interview scheduled", "Offer")
ACTIVE = ("Approved", "Applied", "Needs you", "Recruiter reply",
          "Interview scheduled")
FUNNEL_STAGES = ("Discovered", "Ready to apply", "Approved", "Applied",
                 "Recruiter reply", "Interview scheduled", "Offer")


def _count_status_in(conn, statuses) -> int:
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        f"SELECT COUNT(*) FROM applications WHERE status IN ({marks})",
        statuses).fetchone()[0]


def phone_screen_readiness(conn) -> float | None:
    """Mean of the latest cv_version per job, excluding null pct (§8.2)."""
    rows = conn.execute(
        "SELECT cv.phone_screen_pct AS pct FROM cv_versions cv"
        " JOIN (SELECT job_id, MAX(id) AS max_id FROM cv_versions"
        "       GROUP BY job_id) latest ON latest.max_id = cv.id"
        " WHERE cv.phone_screen_pct IS NOT NULL").fetchall()
    pcts = [r["pct"] for r in rows]
    return round(sum(pcts) / len(pcts), 1) if pcts else None


def response_rate(conn) -> float | None:
    applied = _count_status_in(conn, APPLIED_OR_BEYOND)
    if applied == 0:
        return None
    replied = _count_status_in(conn, REPLIED)
    return round(replied / applied, 3)


def velocity(conn, *, now: datetime) -> int:
    cutoff = (now - timedelta(days=VELOCITY_WINDOW_DAYS)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT COUNT(*) FROM applications WHERE submitted_at IS NOT NULL"
        " AND submitted_at >= ?", (cutoff,)).fetchone()[0]


def funnel(conn) -> list[dict]:
    counts = {s: _count_status_in(conn, (s,)) for s in FUNNEL_STAGES}
    top = counts[FUNNEL_STAGES[0]]
    out = []
    for stage in FUNNEL_STAGES:
        c = counts[stage]
        pct = round(c / top, 3) if top else None
        out.append({"stage": stage, "count": c, "pct": pct})
    return out


def status_breakdown(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM applications"
        " GROUP BY status ORDER BY count DESC").fetchall()
    return [dict(r) for r in rows]


def source_health(conn) -> list[dict]:
    health = dq.digest_header(conn).get("health", {})
    out = []
    for source, value in health.items():
        if isinstance(value, dict):
            out.append({"source": source, **value})
        else:
            out.append({"source": source, "value": value})
    return out


def replies_cards(conn) -> list[dict]:
    rows = conn.execute(
        dq.CARD_SELECT + " WHERE a.status = 'Recruiter reply'"
        " ORDER BY a.id DESC").fetchall()
    return [dict(r) for r in rows]


JOBS_SORTS = {
    "score": "score DESC",
    "recent": "a.id DESC",
    "company": "j.company ASC",
}


def jobs_list(conn, *, status=None, q=None, sort="recent", source=None) -> list[dict]:
    """Filterable flat list for the table. Uses dashboard CARD_SELECT.

    NOTE: CARD_SELECT must alias LATEST_SCORE AS score for ORDER BY score to
    work; verify the alias in dashboard/queries.py before relying on it.
    """
    where, params = [], []
    if status:
        where.append("a.status = ?")
        params.append(status)
    if source:
        where.append("j.source = ?")
        params.append(source)
    if q:
        where.append("(j.company LIKE ? OR j.title LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    order = JOBS_SORTS.get(sort, JOBS_SORTS["recent"])
    rows = conn.execute(dq.CARD_SELECT + clause + f" ORDER BY {order}", params).fetchall()
    return [dict(r) for r in rows]


def _date_strs(now: datetime, days: int) -> list[str]:
    """Ascending list of ISO dates ending at now.date(), length `days`."""
    end = now.date()
    return [(end - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def applications_per_day(conn, *, now: datetime, days: int = 30) -> list[dict]:
    """Submitted applications per day, zero-filled across the window.

    Buckets on applications.submitted_at — the only reliable application
    timestamp (events for migrated apps are 'imported', not status_change)."""
    dates = _date_strs(now, days)
    start, end = dates[0], dates[-1]
    rows = conn.execute(
        "SELECT substr(submitted_at, 1, 10) AS d, COUNT(*) AS c FROM applications"
        " WHERE submitted_at IS NOT NULL"
        " AND substr(submitted_at, 1, 10) BETWEEN ? AND ?"
        " GROUP BY d", (start, end)).fetchall()
    counts = {r["d"]: r["c"] for r in rows}
    return [{"date": d, "count": counts.get(d, 0)} for d in dates]


def replies_per_day(conn, *, now: datetime, days: int = 30) -> list[dict]:
    """Recruiter-reply transitions per day, zero-filled across the window.

    Counts status_change events whose detail is '-> Recruiter reply' — the
    canonical transition marker written by pipeline.statuses.set_status."""
    dates = _date_strs(now, days)
    start, end = dates[0], dates[-1]
    rows = conn.execute(
        "SELECT substr(created_at, 1, 10) AS d, COUNT(*) AS c FROM events"
        " WHERE event_type = 'status_change' AND detail = '-> Recruiter reply'"
        " AND substr(created_at, 1, 10) BETWEEN ? AND ?"
        " GROUP BY d", (start, end)).fetchall()
    counts = {r["d"]: r["c"] for r in rows}
    return [{"date": d, "count": counts.get(d, 0)} for d in dates]


def phone_screen_trend(conn, *, now: datetime, days: int = 30) -> dict:
    """Daily mean phone_screen_pct across cv_versions in the window.

    Sparse: only days with at least one scored CV produce a point. Each CV
    version is a data point at its created_at — distinct from the snapshot
    phone_screen_readiness (latest-per-job)."""
    dates = _date_strs(now, days)
    start, end = dates[0], dates[-1]
    rows = conn.execute(
        "SELECT substr(created_at, 1, 10) AS d, AVG(phone_screen_pct) AS v,"
        " COUNT(*) AS n FROM cv_versions"
        " WHERE phone_screen_pct IS NOT NULL"
        " AND substr(created_at, 1, 10) BETWEEN ? AND ?"
        " GROUP BY d ORDER BY d", (start, end)).fetchall()
    return {
        "target": PHONE_SCREEN_TARGET,
        "points": [
            {"date": r["d"], "value": round(r["v"], 1), "n": r["n"]} for r in rows
        ],
    }


def analytics(conn, *, now: datetime, days: int = 30) -> dict:
    """Trends view: snapshot KPIs reused from overview plus honest per-day
    series built only from reliably timestamped data (§ truth gate)."""
    return {
        "days": days,
        "kpis": {
            "phone_screen_readiness": {
                "value": phone_screen_readiness(conn),
                "target": PHONE_SCREEN_TARGET,
            },
            "response_rate": response_rate(conn),
            "velocity": {
                "value": velocity(conn, now=now),
                "goal": VELOCITY_GOAL,
                "window_days": VELOCITY_WINDOW_DAYS,
            },
        },
        "funnel": funnel(conn),
        "status_breakdown": status_breakdown(conn),
        "applications_per_day": applications_per_day(conn, now=now, days=days),
        "replies_per_day": replies_per_day(conn, now=now, days=days),
        "phone_screen_trend": phone_screen_trend(conn, now=now, days=days),
    }


def overview(conn, *, now: datetime) -> dict:
    return {
        "kpis": {
            "phone_screen_readiness": {
                "value": phone_screen_readiness(conn),
                "target": PHONE_SCREEN_TARGET,
            },
            "response_rate": response_rate(conn),
            "velocity": {"value": velocity(conn, now=now), "goal": VELOCITY_GOAL},
            "in_flight": _count_status_in(conn, ACTIVE),
            "replies_to_action": _count_status_in(conn, ("Recruiter reply",)),
        },
        "today": dq.queue_cards(conn),
        "borderline": dq.borderline_cards(conn),
        "auto_approved_today": dq.auto_approved_today(conn),
        "followups_due": followups.due_followups(conn, now=now),
        "replies_to_action": replies_cards(conn),
        "upcoming_interviews": prep_store.upcoming_interviews(conn, now=now),
        "funnel": funnel(conn),
        "status_breakdown": status_breakdown(conn),
        "source_health": source_health(conn),
    }


def _cv_with_url(cv: dict | None) -> dict | None:
    if not cv:
        return None
    return {**cv, "pdf_url": f"/api/files/cv/{cv['id']}"}


def _cover_letter(job_id: int, lang: str) -> str | None:
    path = paths.COVER_LETTERS_DIR / f"{job_id}_{lang}.md"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _fit(conn, job_id: int) -> dict:
    report = gap_analysis.analyse_job(conn, job_id)
    if report is None:
        return {"available": False}            # §8.3
    return {
        "available": True,
        "coverage_score": report.coverage_score,
        "risk_tier": report.risk_tier,
        "matched_keywords": report.matched_keywords,
        "missing_keywords": report.missing_keywords,
    }


def job_events(conn, job_id: int, *, limit: int = 50) -> list[dict]:
    """Activity timeline for the drawer. Picks up events keyed to the job
    directly OR to its application (status changes log against the latter)."""
    rows = conn.execute(
        "SELECT e.id, e.event_type, e.detail, e.source, e.created_at "
        "FROM events e "
        "WHERE e.job_id = ? "
        "   OR e.application_id IN (SELECT id FROM applications WHERE job_id = ?) "
        "ORDER BY e.created_at DESC, e.id DESC LIMIT ?",
        (job_id, job_id, limit)).fetchall()
    return [dict(r) for r in rows]


def _pending_regen(conn, job_id: int) -> dict | None:
    """The job's queued (not-yet-rendered) CV regeneration, if any. The webapp
    only writes regen requests; a background run renders and resolves them, so
    the drawer needs this to show a 'queued' state and clear it once rendered."""
    row = conn.execute(
        "SELECT id, notes, creativity, created_at FROM regen_requests"
        " WHERE job_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (job_id,)).fetchone()
    if row is None:
        return None
    return {"request_id": row["id"], "notes": row["notes"],
            "creativity": row["creativity"], "created_at": row["created_at"]}


def _last_apply(conn, job_id) -> dict | None:
    """The job's most recent apply request of any status, or None. Drives the
    drawer Apply-now lifecycle banner. Mirrors _last_regen."""
    row = apply_requests.latest_request(conn, job_id)
    if row is None:
        return None
    return {
        "request_id": row["id"],
        "status": row["status"],
        "channel": row["channel"],
        "detail": row["detail"],
        "screenshot_path": row["screenshot_path"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
    }


def _last_regen(conn, job_id: int) -> dict | None:
    """The job's most recent regeneration request of ANY status (or None).

    `pending_regen` (above) drives the queued/running chip and the Run-now
    affordance; this sibling drives the failed banner and the done confirmation,
    so the drawer can tell a finished/failed regen apart from a still-queued one."""
    row = regen.latest_request(conn, job_id)
    if row is None:
        return None
    return {"request_id": row["id"], "status": row["status"],
            "notes": row["notes"], "creativity": row["creativity"],
            "created_at": row["created_at"], "resolved_at": row["resolved_at"],
            "detail": row["detail"]}


def job_detail(conn, job_id: int) -> dict | None:
    base = dq.job_detail(conn, job_id)
    if base is None:
        return None
    cvs = base.get("cv_versions") or {}
    base["cv_versions"] = {
        "en": _cv_with_url(cvs.get("en")),
        "fr": _cv_with_url(cvs.get("fr")),
    }
    base["fit"] = _fit(conn, job_id)
    base["cover_letter"] = {
        "en": _cover_letter(job_id, "en"),
        "fr": _cover_letter(job_id, "fr"),
    }
    base["events"] = job_events(conn, job_id)    # §8 drawer Activity tab
    base["pending_regen"] = _pending_regen(conn, job_id)
    base["last_regen"] = _last_regen(conn, job_id)
    base["last_apply"] = _last_apply(conn, job_id)
    return base                                  # base["score"] is None when no score (§8.4)
