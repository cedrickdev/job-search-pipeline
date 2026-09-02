from pipeline.jobs import insert_job
from pipeline.scoring_io import jobs_needing_score, record_score
from pipeline.statuses import create_application


def _seed(conn, title="Senior Sales Assistant"):
    job_id, _ = insert_job(conn, {"source": "wtj", "company": "Acme",
                                  "title": title, "url": "https://x.test/1",
                                  "description": "Serve customers at the till."})
    app_id = create_application(conn, job_id, source="discovery")
    return job_id, app_id


def test_jobs_needing_score_lists_discovered_only(conn):
    job_id, app_id = _seed(conn)
    pending = jobs_needing_score(conn)
    assert len(pending) == 1
    assert pending[0]["id"] == job_id
    assert pending[0]["application_id"] == app_id
    assert pending[0]["description"] == "Serve customers at the till."


def test_record_score_high_transitions_to_scored(conn):
    job_id, app_id = _seed(conn)
    status = record_score(conn, job_id, 85, "Strong retail overlap.")
    assert status == "Scored"
    row = conn.execute("SELECT * FROM scores WHERE job_id = ?", (job_id,)).fetchone()
    assert row["score"] == 85
    assert row["reasoning"] == "Strong retail overlap."
    app = conn.execute("SELECT status FROM applications WHERE id = ?",
                       (app_id,)).fetchone()
    assert app["status"] == "Scored"
    assert jobs_needing_score(conn) == []


def test_record_score_thresholds(conn):
    job_id, _ = _seed(conn)
    assert record_score(conn, job_id, 60, "Partial fit.") == "Borderline"
    job_id2, _ = _seed(conn, title="Sales Assistant - Bakery")
    assert record_score(conn, job_id2, 30, "Wrong domain.",
                        red_flags="freelance") == "Archived"
