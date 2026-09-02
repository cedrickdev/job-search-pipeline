"""Prep + interview endpoints (spec §5.4 prep)."""
import json

from pipeline.db import connect
from server import chat
from tests.helpers import seed_job


def _conn(api_client):
    c = connect(api_client.db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def _fake_turn(text, *, mandate_ok=True, flags=None):
    """Build a collect_turn stand-in that returns a canned copilot reply."""
    async def _turn(stdin_text, *, settings=None, resume=None):
        return {"text": text, "session_id": None, "actions": [],
                "mandate_ok": mandate_ok, "flags": flags or []}
    return _turn


_PREP_JSON = {
    "likely_questions": ["Why this role?", "Walk me through a hard project."],
    "company_research": ["Series B in 2024", "Remote-first culture"],
    "talking_points": ["Led the ETL rebuild"],
}


def test_get_prep_default(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    r = api_client.get(f"/api/jobs/{job_id}/prep")
    assert r.status_code == 200
    body = r.json()
    assert body["notes_md"] == "" and body["interviews"] == []


def test_put_notes(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    r = api_client.put(f"/api/jobs/{job_id}/prep/notes", json={"notes_md": "remember STAR stories"})
    assert r.status_code == 200
    assert api_client.get(f"/api/jobs/{job_id}/prep").json()["notes_md"] == "remember STAR stories"


def test_add_and_update_interview(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    add = api_client.post(f"/api/jobs/{job_id}/interviews",
                          json={"round_label": "Tech screen", "scheduled_for": "2026-06-18T15:00:00"})
    assert add.status_code == 200
    iid = add.json()["id"]
    upd = api_client.patch(f"/api/interviews/{iid}", json={"outcome": "passed"})
    assert upd.status_code == 200
    interviews = api_client.get(f"/api/jobs/{job_id}/prep").json()["interviews"]
    assert interviews[0]["outcome"] == "passed"


def test_generate_caches_compliant_prep(api_client, monkeypatch):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    reply = f"```json\n{json.dumps(_PREP_JSON)}\n```"
    monkeypatch.setattr(chat, "collect_turn", _fake_turn(reply, mandate_ok=True))

    r = api_client.post(f"/api/jobs/{job_id}/prep/generate",
                        params={"now": "2026-06-16T12:00:00"})
    assert r.status_code == 200
    body = r.json()
    assert body["likely_questions"] == _PREP_JSON["likely_questions"]
    assert body["company_research"] == _PREP_JSON["company_research"]
    assert body["talking_points"] == _PREP_JSON["talking_points"]
    assert body["mandate_ok"] is True and body["flags"] == []
    assert body["generated_at"] == "2026-06-16T12:00:00"

    # Compliant output is persisted: a fresh GET returns the cached prep.
    cached = api_client.get(f"/api/jobs/{job_id}/prep").json()
    assert cached["likely_questions"] == _PREP_JSON["likely_questions"]
    assert cached["generated_at"] == "2026-06-16T12:00:00"


def test_generate_does_not_cache_noncompliant_prep(api_client, monkeypatch):
    # Fail-closed (§6.2): an unverified draft is returned so the UI can warn,
    # but it is NOT written to prep_cache — a reload must not resurrect it as ready.
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    reply = f"```json\n{json.dumps(_PREP_JSON)}\n```"
    monkeypatch.setattr(chat, "collect_turn",
                        _fake_turn(reply, mandate_ok=False, flags=["anonymization_config_missing"]))

    r = api_client.post(f"/api/jobs/{job_id}/prep/generate",
                        params={"now": "2026-06-16T12:00:00"})
    assert r.status_code == 200
    body = r.json()
    assert body["mandate_ok"] is False
    assert body["flags"] == ["anonymization_config_missing"]
    # Draft surfaced to the caller...
    assert body["likely_questions"] == _PREP_JSON["likely_questions"]
    # ...but generated_at is null (not verified/persisted) and nothing was cached.
    assert body["generated_at"] is None
    cached = api_client.get(f"/api/jobs/{job_id}/prep").json()
    assert cached["likely_questions"] is None
    assert cached["generated_at"] is None


def test_generate_unknown_job_404(api_client, monkeypatch):
    monkeypatch.setattr(chat, "collect_turn", _fake_turn("{}"))
    r = api_client.post("/api/jobs/424242/prep/generate")
    assert r.status_code == 404


def test_upcoming_interviews_in_overview(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    api_client.post(f"/api/jobs/{job_id}/interviews",
                    json={"round_label": "Onsite", "scheduled_for": "2026-07-01T10:00:00"})
    ov = api_client.get("/api/overview", params={"now": "2026-06-11T09:00:00"}).json()
    item = next(i for i in ov["upcoming_interviews"] if i["round_label"] == "Onsite")
    # Contract guard (matches InterviewItem TS type): job_id + title for
    # drawer navigation; application_id is intentionally NOT exposed.
    assert item["job_id"] == job_id
    assert "title" in item and "company" in item
    assert "application_id" not in item
