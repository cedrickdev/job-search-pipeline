"""Shared seed helpers for dashboard, server, and pipeline tests."""
from pipeline.statuses import create_application, set_status

_COUNTER = {"n": 0}


def _next() -> int:
    _COUNTER["n"] += 1
    return _COUNTER["n"]


def seed_cv_version(conn, job_id, *, language="en", phone_screen_pct=None,
                    pdf_path=None, created_at="2026-06-11T07:00:00",
                    content_hash=None):
    """Insert a cv_versions row. Returns its id."""
    n = _next()
    pdf_path = pdf_path or f"/tmp/cv_{n}.pdf"
    content_hash = content_hash or f"hash-{n}"
    cur = conn.execute(
        "INSERT INTO cv_versions (job_id, language, pdf_path, content_hash,"
        " phone_screen_pct, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, language, pdf_path, content_hash, phone_screen_pct, created_at))
    conn.commit()
    return cur.lastrowid


def seed_job(conn, company="Acme", title="Senior Sales Assistant",
             status="Ready to apply", score=88, url="https://example.com/job",
             language="en", description="Build models for the growth team.",
             created_at=None, phone_screen_pct=None, applied_at=None,
             source="wtj"):
    """Insert a job + application (+ optional score / cv_version).

    Returns (job_id, application_id).

    created_at: when given, overrides the timestamps on the application's
      status_change events and the score row (trends bucket on these).
    phone_screen_pct: when given, seeds an 'en' cv_version carrying it.
    applied_at: when given, stamps applications.submitted_at (velocity/in-flight).
    source: job board the row is attributed to (defaults to 'wtj').
    """
    n = _next()
    cur = conn.execute(
        "INSERT INTO jobs (source, company, title, url, location, language,"
        " description, discovered_date, dedup_hash)"
        " VALUES (?, ?, ?, ?, 'Lausanne', ?, ?, '2026-06-11', ?)",
        (source, company, title, url, language, description,
         f"seed-{n}-{company}-{title}"))
    job_id = cur.lastrowid
    application_id = create_application(conn, job_id, source="test")
    if status != "Discovered":
        set_status(conn, application_id, status, source="test")
    if score is not None:
        conn.execute(
            "INSERT INTO scores (job_id, score, reasoning, scorer_version,"
            " created_at) VALUES (?, ?, 'seed reasoning', 'v1', ?)",
            (job_id, score, created_at or "2026-06-11T07:00:00"))
        conn.commit()
    if created_at is not None:
        conn.execute("UPDATE events SET created_at=? WHERE application_id=?",
                     (created_at, application_id))
        conn.commit()
    if applied_at is not None:
        conn.execute("UPDATE applications SET submitted_at=? WHERE id=?",
                     (applied_at, application_id))
        conn.commit()
    if phone_screen_pct is not None:
        seed_cv_version(conn, job_id, language="en",
                        phone_screen_pct=phone_screen_pct,
                        created_at=created_at or "2026-06-11T07:00:00")
    return job_id, application_id
