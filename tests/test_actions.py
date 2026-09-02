"""Action endpoints + transition guards (spec §5.2, §5.3, §4.1)."""
from tests.helpers import seed_job, seed_cv_version


def _conn(api_client):
    from pipeline.db import connect
    c = connect(api_client.db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def _status(conn, app_id):
    return conn.execute("SELECT status FROM applications WHERE id=?", (app_id,)).fetchone()["status"]


def test_go_ready_to_approved(api_client):
    conn = _conn(api_client)
    job_id, app_id = seed_job(conn, status="Ready to apply")
    r = api_client.post(f"/api/jobs/{job_id}/go")
    assert r.status_code == 200
    assert r.json()["status"] == "Approved"
    assert _status(_conn(api_client), app_id) == "Approved"


def test_go_from_wrong_status_409(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, status="Applied")
    r = api_client.post(f"/api/jobs/{job_id}/go")
    assert r.status_code == 409
    assert r.json()["detail"]["current"] == "Applied"
    assert r.json()["detail"]["target"] == "Approved"


def test_applied_from_approved(api_client):
    conn = _conn(api_client)
    job_id, app_id = seed_job(conn, status="Approved")
    r = api_client.post(f"/api/jobs/{job_id}/applied")
    assert r.status_code == 200
    assert r.json()["status"] == "Applied"
    # mark_applied stamps submitted_at
    assert _conn(api_client).execute(
        "SELECT submitted_at FROM applications WHERE id=?", (app_id,)
    ).fetchone()["submitted_at"] is not None


def test_applied_from_wrong_status_409(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, status="Ready to apply")
    r = api_client.post(f"/api/jobs/{job_id}/applied")
    assert r.status_code == 409


def test_skip_to_archived(api_client):
    conn = _conn(api_client)
    job_id, app_id = seed_job(conn, status="Ready to apply")
    r = api_client.post(f"/api/jobs/{job_id}/skip")
    assert r.status_code == 200
    assert _status(_conn(api_client), app_id) == "Archived"


def test_manual_status_valid(api_client):
    conn = _conn(api_client)
    job_id, app_id = seed_job(conn, status="Applied")
    r = api_client.post(f"/api/jobs/{job_id}/status", json={"status": "Recruiter reply"})
    assert r.status_code == 200
    assert _status(_conn(api_client), app_id) == "Recruiter reply"


def test_manual_status_logs_detail(api_client):
    conn = _conn(api_client)
    job_id, app_id = seed_job(conn, status="Applied")
    r = api_client.post(
        f"/api/jobs/{job_id}/status",
        json={"status": "Recruiter reply", "detail": "call Tuesday 3pm"})
    assert r.status_code == 200
    row = _conn(api_client).execute(
        "SELECT detail FROM events WHERE application_id = ? ORDER BY id DESC LIMIT 1",
        (app_id,)).fetchone()
    assert row["detail"] == "call Tuesday 3pm"


def test_manual_status_invalid_409(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, status="Applied")
    r = api_client.post(f"/api/jobs/{job_id}/status", json={"status": "Bogus"})
    assert r.status_code == 409


def test_regen_creates_request(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, status="Borderline")
    r = api_client.post(f"/api/jobs/{job_id}/regen", json={"notes": "emphasize NLP work"})
    assert r.status_code == 200
    assert "request_id" in r.json()


def test_regen_empty_notes_404(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    r = api_client.post(f"/api/jobs/{job_id}/regen", json={"notes": "   "})
    assert r.status_code == 404


def test_regen_accepts_creativity(api_client):
    # The copilot can fire a CV regen with a boldness level; it must reach the
    # queued request so the morning run can honor it.
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, status="Borderline")
    r = api_client.post(f"/api/jobs/{job_id}/regen",
                        json={"notes": "max JD match", "creativity": "bold"})
    assert r.status_code == 200
    pr = api_client.get(f"/api/jobs/{job_id}").json()["pending_regen"]
    assert pr["creativity"] == "bold"


def test_regen_creativity_defaults_to_balanced(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, status="Borderline")
    api_client.post(f"/api/jobs/{job_id}/regen", json={"notes": "standard"})
    pr = api_client.get(f"/api/jobs/{job_id}").json()["pending_regen"]
    assert pr["creativity"] == "balanced"


def test_action_on_unknown_job_404(api_client):
    r = api_client.post("/api/jobs/999999/applied")
    assert r.status_code == 404


class _FakeDispatcher:
    def __init__(self):
        self.calls = []

    async def dispatch(self, request_id):
        self.calls.append(request_id)


def test_apply_now_creates_request_and_dispatches(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, status="Ready to apply")
    seed_cv_version(conn, job_id, language="en", pdf_path="/tmp/cv.pdf")
    fake = _FakeDispatcher()
    api_client.app.state.apply_dispatcher = fake
    r = api_client.post(f"/api/jobs/{job_id}/apply-now")
    assert r.status_code == 202
    rid = r.json()["request_id"]
    assert fake.calls == [rid]


def test_apply_now_wrong_status_409(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, status="Applied")
    seed_cv_version(conn, job_id, language="en", pdf_path="/tmp/cv.pdf")
    api_client.app.state.apply_dispatcher = _FakeDispatcher()
    r = api_client.post(f"/api/jobs/{job_id}/apply-now")
    assert r.status_code == 409


def test_apply_now_no_cv_409(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, status="Ready to apply")  # no cv_version
    api_client.app.state.apply_dispatcher = _FakeDispatcher()
    r = api_client.post(f"/api/jobs/{job_id}/apply-now")
    assert r.status_code == 409


def test_apply_now_unknown_job_404(api_client):
    api_client.app.state.apply_dispatcher = _FakeDispatcher()
    r = api_client.post("/api/jobs/999999/apply-now")
    assert r.status_code == 404


def test_apply_now_idempotent_returns_same_request(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, status="Ready to apply")
    seed_cv_version(conn, job_id, language="en", pdf_path="/tmp/cv.pdf")
    api_client.app.state.apply_dispatcher = _FakeDispatcher()
    r1 = api_client.post(f"/api/jobs/{job_id}/apply-now")
    r2 = api_client.post(f"/api/jobs/{job_id}/apply-now")
    assert r1.json()["request_id"] == r2.json()["request_id"]


def test_apply_now_redispatch_while_pending_does_not_double_dispatch(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, status="Ready to apply")
    seed_cv_version(conn, job_id, language="en", pdf_path="/tmp/cv.pdf")
    fake = _FakeDispatcher()
    api_client.app.state.apply_dispatcher = fake
    r1 = api_client.post(f"/api/jobs/{job_id}/apply-now")
    r2 = api_client.post(f"/api/jobs/{job_id}/apply-now")
    assert r1.json()["request_id"] == r2.json()["request_id"]
    # The second POST reuses the in-flight request, so it must NOT spawn a
    # second worker for it.
    assert fake.calls == [r1.json()["request_id"]]


def test_job_detail_surfaces_last_apply(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn, status="Ready to apply")
    seed_cv_version(conn, job_id, language="en", pdf_path="/tmp/cv.pdf")
    from pipeline.apply_requests import create_request, resolve_request

    rid = create_request(conn, job_id)
    resolve_request(conn, rid, "needs_you", "Browser in use. Close the live session and retry.")
    la = api_client.get(f"/api/jobs/{job_id}").json()["last_apply"]
    assert la["request_id"] == rid
    assert la["status"] == "needs_you"
    assert "Browser in use" in la["detail"]
