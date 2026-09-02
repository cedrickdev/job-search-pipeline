# tests/test_overview.py
"""GET /api/overview: KPIs, lists, edge cases (§8.1, §8.2)."""
from datetime import datetime

from tests.helpers import seed_job, seed_cv_version


def test_overview_empty_db_is_200_with_null_rates(api_client):
    r = api_client.get("/api/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["kpis"]["response_rate"] is None          # 0 denominator -> null
    assert body["kpis"]["phone_screen_readiness"]["value"] is None
    assert body["kpis"]["phone_screen_readiness"]["target"] == 90
    assert body["kpis"]["in_flight"] == 0
    assert body["today"] == []
    assert body["followups_due"] == []                    # nothing due yet
    assert body["replies_to_action"] == []                # no recruiter replies


def test_phone_screen_readiness_excludes_nulls(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="A", phone_screen_pct=80)
    seed_job(conn, company="B", phone_screen_pct=100)
    seed_job(conn, company="C", phone_screen_pct=None)    # excluded
    r = api_client.get("/api/overview")
    assert r.json()["kpis"]["phone_screen_readiness"]["value"] == 90.0


def test_response_rate_and_in_flight(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="A", status="Applied")
    seed_job(conn, company="B", status="Recruiter reply")
    seed_job(conn, company="C", status="Interview scheduled")
    r = api_client.get("/api/overview").json()
    # applied_or_beyond = 3 ; replied = 2  -> 0.667
    assert r["kpis"]["response_rate"] == 0.667
    # active = Applied + Recruiter reply + Interview scheduled = 3
    assert r["kpis"]["in_flight"] == 3


def test_velocity_counts_recent_submits(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="Fresh", status="Applied",
             applied_at="2026-06-15T09:00:00")
    seed_job(conn, company="Stale", status="Applied",
             applied_at="2026-05-01T09:00:00")
    r = api_client.get("/api/overview?now=2026-06-16T08:00:00").json()
    assert r["kpis"]["velocity"]["value"] == 1
    assert r["kpis"]["velocity"]["goal"] == 5


def test_today_lists_ready_to_apply(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="ReadyCo", status="Ready to apply", score=91)
    r = api_client.get("/api/overview").json()
    assert [c["company"] for c in r["today"]] == ["ReadyCo"]


def test_overview_populates_followups_due_and_replies(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="StaleCo", status="Applied",
             applied_at="2026-06-05T09:00:00")           # 11 days -> due
    seed_job(conn, company="ReplyCo", status="Recruiter reply",
             created_at="2026-06-15T09:00:00")           # fresh reply -> action
    r = api_client.get("/api/overview?now=2026-06-16T08:00:00").json()
    due = r["followups_due"]
    assert [d["company"] for d in due] == ["StaleCo"]
    assert due[0]["kind"] == "applied_no_reply"
    assert [c["company"] for c in r["replies_to_action"]] == ["ReplyCo"]


def _conn(api_client):
    from pipeline.db import connect
    c = connect(api_client.db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    return c
