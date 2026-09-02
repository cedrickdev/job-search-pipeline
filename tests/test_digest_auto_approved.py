from tests.helpers import seed_job
from pipeline.digest import build_digest
from pipeline.statuses import set_status


def _seed_run(conn):
    conn.execute(
        "INSERT INTO runs (kind, started_at, finished_at, summary)"
        " VALUES ('discovery', '2026-06-11T08:00:00', '2026-06-11T08:05:00',"
        " '{\"new_jobs\": 0, \"lookback_days\": 1, \"health\": {}}')")
    conn.commit()


def test_digest_lists_auto_approved_today(conn):
    _seed_run(conn)
    _, app_id = seed_job(conn, company="AutoCo")
    set_status(conn, app_id, "Approved", source="auto-approve")
    text = build_digest(conn)
    assert "## Auto-approved today" in text
    assert "AutoCo" in text


def test_digest_omits_section_without_auto_approvals(conn):
    _seed_run(conn)
    _, app_id = seed_job(conn, company="ManualCo")
    set_status(conn, app_id, "Approved", source="dashboard")
    assert "Auto-approved" not in build_digest(conn)
