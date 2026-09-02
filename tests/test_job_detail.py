"""GET /api/jobs/{job_id}: fit availability (§8.3), score null (§8.4), cv links."""
from tests.helpers import seed_job, seed_cv_version
from pipeline.regen import create_request, fail_request, resolve_request


def _conn(api_client):
    from pipeline.db import connect
    c = connect(api_client.db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def test_unknown_job_404(api_client):
    assert api_client.get("/api/jobs/424242").status_code == 404


def test_detail_fit_unavailable_when_no_description(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, description="")
    fit = api_client.get(f"/api/jobs/{job_id}").json()["fit"]
    assert fit == {"available": False}


def test_detail_fit_available_with_description(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, description="We need Python, SQL and Git.")
    fit = api_client.get(f"/api/jobs/{job_id}").json()["fit"]
    assert fit["available"] is True
    assert "coverage_score" in fit and "risk_tier" in fit


def test_detail_score_null_when_no_score(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, score=None)
    assert api_client.get(f"/api/jobs/{job_id}").json()["score"] is None


def test_detail_cv_pdf_url(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, phone_screen_pct=88)  # seeds an en cv_version
    cvs = api_client.get(f"/api/jobs/{job_id}").json()["cv_versions"]
    assert cvs["en"] is not None
    assert cvs["en"]["pdf_url"].startswith("/api/files/cv/")
    assert cvs["fr"] is None


def test_detail_includes_activity_events(api_client):
    conn = _conn(api_client)
    job_id, application_id = seed_job(conn, status="Applied")
    # One event keyed directly to the job, one keyed to its application:
    # job_events must surface BOTH (the OR branch on application_id).
    conn.execute(
        "INSERT INTO events (job_id, event_type, detail, source, created_at)"
        " VALUES (?, 'discovered', 'found on wtj', 'pipeline', '2026-06-10T09:00:00')",
        (job_id,))
    conn.execute(
        "INSERT INTO events (application_id, event_type, detail, source, created_at)"
        " VALUES (?, 'note', 'called recruiter', 'manual', '2026-06-12T10:00:00')",
        (application_id,))
    conn.commit()
    events = api_client.get(f"/api/jobs/{job_id}").json()["events"]
    types = [e["event_type"] for e in events]
    assert "discovered" in types   # job-keyed event
    assert "note" in types         # application-keyed event (OR branch)
    assert all(
        {"id", "event_type", "detail", "source", "created_at"} <= set(e)
        for e in events)
    # newest-first (DESC): the 2026-06-12 note sorts before the 2026-06-10
    # discovered, independent of when seed_job's status_change events land.
    assert types.index("note") < types.index("discovered")


def test_detail_pending_regen_null_by_default(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    assert api_client.get(f"/api/jobs/{job_id}").json()["pending_regen"] is None


def test_detail_pending_regen_when_queued(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    rid = create_request(conn, job_id, "Lead on NLP and LLMs", creativity="bold")
    pr = api_client.get(f"/api/jobs/{job_id}").json()["pending_regen"]
    assert pr is not None
    assert pr["request_id"] == rid
    assert pr["notes"] == "Lead on NLP and LLMs"
    assert pr["creativity"] == "bold"
    assert "created_at" in pr


def test_detail_pending_regen_clears_after_resolve(api_client):
    # Once the background run renders the CV and resolves the request, the
    # drawer's queued indicator must clear (status -> 'done', no longer pending).
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    rid = create_request(conn, job_id, "More econometrics")
    resolve_request(conn, rid)
    assert api_client.get(f"/api/jobs/{job_id}").json()["pending_regen"] is None


def test_detail_last_regen_null_by_default(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    assert api_client.get(f"/api/jobs/{job_id}").json()["last_regen"] is None


def test_detail_last_regen_reflects_pending(api_client):
    # While queued, last_regen mirrors pending_regen so the drawer can show the
    # queued state and creativity without a second query path.
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    rid = create_request(conn, job_id, "Lead on NLP and LLMs", creativity="bold")
    lr = api_client.get(f"/api/jobs/{job_id}").json()["last_regen"]
    assert lr is not None
    assert lr["request_id"] == rid
    assert lr["status"] == "pending"
    assert lr["creativity"] == "bold"
    assert lr["detail"] is None
    assert lr["resolved_at"] is None


def test_detail_last_regen_reflects_done(api_client):
    # After a successful run, pending_regen clears but last_regen records the
    # completed request so the drawer can confirm "✓ regeneration applied".
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    rid = create_request(conn, job_id, "More econometrics")
    resolve_request(conn, rid)
    body = api_client.get(f"/api/jobs/{job_id}").json()
    assert body["pending_regen"] is None
    lr = body["last_regen"]
    assert lr["request_id"] == rid
    assert lr["status"] == "done"
    assert lr["resolved_at"] is not None


def test_detail_last_regen_reflects_failure_with_detail(api_client):
    # A failed regen must be distinguishable from a queue: pending_regen clears,
    # last_regen carries status 'failed' and the human-readable reason.
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    rid = create_request(conn, job_id, "Bold rewrite", creativity="bold")
    fail_request(conn, rid, "tailoring failed after 3 attempts")
    body = api_client.get(f"/api/jobs/{job_id}").json()
    assert body["pending_regen"] is None
    lr = body["last_regen"]
    assert lr["request_id"] == rid
    assert lr["status"] == "failed"
    assert lr["detail"] == "tailoring failed after 3 attempts"
    assert lr["resolved_at"] is not None


def test_fit_endpoint_available(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, description="We need Python, SQL and Git.")
    r = api_client.get(f"/api/jobs/{job_id}/fit")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert "coverage_score" in body and "risk_tier" in body


def test_fit_endpoint_unavailable_when_no_description(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, description="")
    r = api_client.get(f"/api/jobs/{job_id}/fit")
    assert r.status_code == 200
    assert r.json() == {"available": False}            # §8.3


def test_fit_endpoint_unknown_job_404(api_client):
    assert api_client.get("/api/jobs/424243/fit").status_code == 404
