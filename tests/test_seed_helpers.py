# tests/test_seed_helpers.py
"""seed_job/seed_cv_version extensions for KPI, trend, and follow-up tests."""
from tests.helpers import seed_job, seed_cv_version


def test_seed_job_sets_created_at_on_events(conn):
    job_id, app_id = seed_job(conn, created_at="2026-01-02T07:00:00")
    rows = conn.execute(
        "SELECT created_at FROM events WHERE application_id=?", (app_id,)).fetchall()
    assert rows, "seed_job must create at least one event"
    assert all(r["created_at"] == "2026-01-02T07:00:00" for r in rows)


def test_seed_job_sets_score_created_at(conn):
    job_id, _ = seed_job(conn, created_at="2026-01-02T07:00:00", score=77)
    row = conn.execute(
        "SELECT created_at FROM scores WHERE job_id=?", (job_id,)).fetchone()
    assert row["created_at"] == "2026-01-02T07:00:00"


def test_seed_job_applied_at_sets_submitted_at(conn):
    job_id, app_id = seed_job(conn, status="Applied",
                              applied_at="2026-06-15T09:00:00")
    row = conn.execute(
        "SELECT submitted_at FROM applications WHERE id=?", (app_id,)).fetchone()
    assert row["submitted_at"] == "2026-06-15T09:00:00"


def test_seed_job_phone_screen_pct_creates_cv_version(conn):
    job_id, _ = seed_job(conn, phone_screen_pct=92)
    row = conn.execute(
        "SELECT phone_screen_pct FROM cv_versions WHERE job_id=? AND language='en'"
        " ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    assert row["phone_screen_pct"] == 92


def test_seed_cv_version_independent(conn):
    job_id, _ = seed_job(conn, phone_screen_pct=None)
    seed_cv_version(conn, job_id, language="fr", phone_screen_pct=80)
    row = conn.execute(
        "SELECT phone_screen_pct FROM cv_versions WHERE job_id=? AND language='fr'",
        (job_id,)).fetchone()
    assert row["phone_screen_pct"] == 80


def test_seed_job_counter_keeps_dedup_unique(conn):
    a, _ = seed_job(conn, company="X")
    b, _ = seed_job(conn, company="X")
    assert a != b  # distinct dedup_hash, distinct rows
