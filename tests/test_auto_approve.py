from tests.helpers import seed_job
from pipeline.auto_approve import approved_today_count, eligible, run
from pipeline.statuses import set_status

ON = {"auto_apply": True, "auto_apply_min_score": 85, "auto_apply_daily_cap": 5}


def _status_of(conn, application_id):
    return conn.execute("SELECT status FROM applications WHERE id = ?",
                        (application_id,)).fetchone()["status"]


def test_eligible_filters_by_floor_and_orders_by_score(conn):
    seed_job(conn, company="Low", score=70)
    _, app_mid = seed_job(conn, company="Mid", score=88)
    _, app_top = seed_job(conn, company="Top", score=95)
    rows = eligible(conn, 85)
    assert [r["company"] for r in rows] == ["Top", "Mid"]
    assert rows[0]["application_id"] == app_top
    assert rows[1]["application_id"] == app_mid


def test_eligible_only_ready_to_apply(conn):
    seed_job(conn, company="Approved already", status="Approved", score=99)
    seed_job(conn, company="Unscored", score=None)
    assert eligible(conn, 85) == []


def test_run_disabled_changes_nothing(conn):
    _, app_id = seed_job(conn, score=99)
    assert run(conn, {**ON, "auto_apply": False}) == []
    assert _status_of(conn, app_id) == "Ready to apply"


def test_run_approves_with_source_auto_approve(conn):
    _, app_id = seed_job(conn, score=92)
    approved = run(conn, ON)
    assert len(approved) == 1
    assert _status_of(conn, app_id) == "Approved"
    event = conn.execute(
        "SELECT * FROM events WHERE application_id = ?"
        " AND event_type = 'status_change' ORDER BY id DESC LIMIT 1",
        (app_id,)).fetchone()
    assert event["source"] == "auto-approve"


def test_run_respects_cap_taking_top_scores(conn):
    seed_job(conn, company="A", score=86)
    seed_job(conn, company="B", score=97)
    seed_job(conn, company="C", score=91)
    approved = run(conn, {**ON, "auto_apply_daily_cap": 2})
    assert [r["company"] for r in approved] == ["B", "C"]


def test_cap_counts_only_todays_auto_approve_events(conn):
    _, earlier = seed_job(conn, company="Earlier", score=90)
    set_status(conn, earlier, "Approved", source="auto-approve")
    _, manual = seed_job(conn, company="Manual", score=90)
    set_status(conn, manual, "Approved", source="dashboard")
    assert approved_today_count(conn) == 1
    seed_job(conn, company="New1", score=95)
    seed_job(conn, company="New2", score=94)
    approved = run(conn, {**ON, "auto_apply_daily_cap": 2})
    assert [r["company"] for r in approved] == ["New1"]


def test_below_floor_stays_ready(conn):
    _, app_id = seed_job(conn, score=84)
    assert run(conn, ON) == []
    assert _status_of(conn, app_id) == "Ready to apply"
