import pytest

from pipeline.jobs import insert_job
from pipeline.statuses import create_application, set_status


@pytest.fixture
def job_id(conn):
    jid, _ = insert_job(conn, {"company": "Acme", "title": "Senior Sales", "source": "wtj"})
    return jid


def test_create_application_starts_discovered_and_logs_event(conn, job_id):
    app_id = create_application(conn, job_id, source="test")
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    assert app["status"] == "Discovered"
    event = conn.execute("SELECT * FROM events WHERE application_id = ?", (app_id,)).fetchone()
    assert event["event_type"] == "status_change"
    assert event["source"] == "test"


def test_set_status_updates_and_logs(conn, job_id):
    app_id = create_application(conn, job_id, source="test")
    set_status(conn, app_id, "Ready to apply", source="morning_run", detail="score 84")
    app = conn.execute("SELECT status FROM applications WHERE id = ?", (app_id,)).fetchone()
    assert app["status"] == "Ready to apply"
    count = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE application_id = ?", (app_id,)
    ).fetchone()["c"]
    assert count == 2


def test_set_status_rejects_unknown_status(conn, job_id):
    app_id = create_application(conn, job_id, source="test")
    with pytest.raises(ValueError, match="Unknown status"):
        set_status(conn, app_id, "Maybe Later", source="test")


def test_set_status_raises_on_unknown_application_id(conn, job_id):
    with pytest.raises(ValueError, match="No application with id"):
        set_status(conn, 9999, "Applied", source="test")
