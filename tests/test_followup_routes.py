"""Follow-up action routes: snooze, dismiss, draft (spec §9)."""
from tests.helpers import seed_job

NOW = "2026-06-16T08:00:00"


def test_dismiss_hides_followup_from_overview(api_client, db_conn):
    job_id, _ = seed_job(db_conn, company="StaleCo", status="Applied",
                         applied_at="2026-06-05T09:00:00")
    before = api_client.get(f"/api/overview?now={NOW}").json()
    assert [d["company"] for d in before["followups_due"]] == ["StaleCo"]

    resp = api_client.post(f"/api/jobs/{job_id}/followup/dismiss?now={NOW}")
    assert resp.status_code == 200

    after = api_client.get(f"/api/overview?now={NOW}").json()
    assert after["followups_due"] == []


def test_snooze_hides_until_window_expires(api_client, db_conn):
    job_id, _ = seed_job(db_conn, company="StaleCo", status="Applied",
                         applied_at="2026-06-05T09:00:00")
    resp = api_client.post(f"/api/jobs/{job_id}/followup/snooze?now={NOW}",
                           json={"days": 3})
    assert resp.status_code == 200
    assert api_client.get(f"/api/overview?now={NOW}").json()["followups_due"] == []

    later = api_client.get("/api/overview?now=2026-06-20T08:00:00").json()
    assert [d["company"] for d in later["followups_due"]] == ["StaleCo"]


def test_draft_followup_returns_filled_draft(api_client, db_conn):
    job_id, _ = seed_job(db_conn, company="Globex", title="Shift Lead",
                         language="en", status="Applied",
                         applied_at="2026-06-05T09:00:00")
    resp = api_client.post(f"/api/jobs/{job_id}/draft_followup")
    assert resp.status_code == 200
    body = resp.json()
    assert "Globex" in body["subject"]
    assert "Shift Lead" in body["subject"]
    assert "Globex" in body["body"]
    assert "Shift Lead" in body["body"]
    assert "{" not in body["body"]  # every placeholder was filled
    assert body["language"] == "en"
    assert "mandate_ok" in body and "flags" in body


def test_draft_followup_unknown_job_is_404(api_client):
    resp = api_client.post("/api/jobs/999999/draft_followup")
    assert resp.status_code == 404
