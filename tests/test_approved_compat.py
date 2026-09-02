# tests/test_approved_compat.py
"""The applier reads tracker.db directly; assert the DB invariant + /api/approved shape."""
from tests.helpers import seed_job


def test_approved_shape(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="ApprovedCo", status="Approved")
    r = api_client.get("/api/approved")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    for key in ("application_id", "job_id", "company", "title", "url",
                "language", "approved_at"):
        assert key in row


def test_applier_db_invariant(api_client):
    # The binding contract: applier._approved_jobs sees rows whose status='Approved'.
    conn = _conn(api_client)
    seed_job(conn, company="Yes", status="Approved")
    seed_job(conn, company="No", status="Ready to apply")
    from pipeline.applier import _approved_jobs
    approved = _approved_jobs(conn)
    companies = {j["company"] for j in approved}
    assert "Yes" in companies and "No" not in companies


def _conn(api_client):
    from pipeline.db import connect
    c = connect(api_client.db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    return c
