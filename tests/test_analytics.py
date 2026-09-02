# tests/test_analytics.py
"""GET /api/analytics: snapshot KPIs + per-day trends (Phase 2).

All series come from reliably timestamped data only: submitted_at,
status_change events, and cv_versions.created_at. No ratios are
reconstructed from incomplete event history.
"""
from pipeline.db import connect
from tests.helpers import seed_job


def _conn(api_client):
    c = connect(api_client.db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def test_analytics_empty_db(api_client):
    r = api_client.get("/api/analytics?now=2026-06-16T08:00:00&days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["kpis"]["response_rate"] is None
    assert body["kpis"]["phone_screen_readiness"]["target"] == 90
    assert body["kpis"]["velocity"]["window_days"] == 7
    apd = body["applications_per_day"]
    assert len(apd) == 7                       # zero-filled, exactly `days` points
    assert all(p["count"] == 0 for p in apd)
    assert apd[0]["date"] == "2026-06-10"      # ascending, oldest first
    assert apd[-1]["date"] == "2026-06-16"     # newest is `now`
    assert body["replies_per_day"][-1]["date"] == "2026-06-16"
    assert body["phone_screen_trend"]["target"] == 90
    assert body["phone_screen_trend"]["points"] == []


def test_applications_per_day_buckets_by_submitted_at(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="A", status="Applied", applied_at="2026-06-15T09:00:00")
    seed_job(conn, company="B", status="Applied", applied_at="2026-06-15T18:00:00")
    seed_job(conn, company="C", status="Applied", applied_at="2026-06-14T10:00:00")
    seed_job(conn, company="Old", status="Applied", applied_at="2026-05-01T10:00:00")
    body = api_client.get("/api/analytics?now=2026-06-16T08:00:00&days=7").json()
    by_date = {p["date"]: p["count"] for p in body["applications_per_day"]}
    assert by_date["2026-06-15"] == 2
    assert by_date["2026-06-14"] == 1
    assert by_date["2026-06-16"] == 0
    assert "2026-05-01" not in by_date          # outside the window


def test_replies_per_day_counts_recruiter_reply_events(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="R", status="Recruiter reply",
             created_at="2026-06-15T09:00:00")
    seed_job(conn, company="App", status="Applied",
             created_at="2026-06-15T09:00:00")   # not a reply -> not counted
    body = api_client.get("/api/analytics?now=2026-06-16T08:00:00&days=7").json()
    by_date = {p["date"]: p["count"] for p in body["replies_per_day"]}
    assert by_date["2026-06-15"] == 1
    assert by_date["2026-06-16"] == 0


def test_phone_screen_trend_means_by_day(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="A", phone_screen_pct=80, created_at="2026-06-15T07:00:00")
    seed_job(conn, company="B", phone_screen_pct=100, created_at="2026-06-15T08:00:00")
    seed_job(conn, company="C", phone_screen_pct=90, created_at="2026-06-14T08:00:00")
    body = api_client.get("/api/analytics?now=2026-06-16T08:00:00&days=7").json()
    pts = {p["date"]: p for p in body["phone_screen_trend"]["points"]}
    assert pts["2026-06-15"]["value"] == 90.0   # mean(80,100)
    assert pts["2026-06-15"]["n"] == 2
    assert pts["2026-06-14"]["value"] == 90.0
    assert pts["2026-06-14"]["n"] == 1


def test_analytics_reuses_funnel_and_status_breakdown(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="A", status="Applied")
    body = api_client.get("/api/analytics?now=2026-06-16T08:00:00").json()
    assert "Applied" in [f["stage"] for f in body["funnel"]]
    assert any(s["status"] == "Applied" for s in body["status_breakdown"])
