# tests/test_api_jobs.py
"""GET /api/jobs: table list (filter/search/sort) + board grouping."""
from tests.helpers import seed_job


def test_jobs_table_returns_items(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="Alpha", status="Ready to apply", score=90)
    seed_job(conn, company="Beta", status="Applied", score=70)
    body = api_client.get("/api/jobs").json()
    assert "items" in body
    companies = {it["company"] for it in body["items"]}
    assert companies == {"Alpha", "Beta"}


def test_jobs_filter_by_status(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="Alpha", status="Ready to apply")
    seed_job(conn, company="Beta", status="Applied")
    body = api_client.get("/api/jobs?status=Applied").json()
    assert [it["company"] for it in body["items"]] == ["Beta"]


def test_jobs_search_matches_company_or_title(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="Acme", title="Shift Lead")
    seed_job(conn, company="Globex", title="Sales Assistant")
    body = api_client.get("/api/jobs?q=globe").json()
    assert [it["company"] for it in body["items"]] == ["Globex"]


def test_jobs_filter_by_source(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="Linky", source="linkedin")
    seed_job(conn, company="Welcomer", source="wtj")
    body = api_client.get("/api/jobs?source=linkedin").json()
    assert [it["company"] for it in body["items"]] == ["Linky"]


def test_jobs_sort_by_score(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="Low", score=60)
    seed_job(conn, company="High", score=95)
    body = api_client.get("/api/jobs?sort=score").json()
    assert [it["company"] for it in body["items"]] == ["High", "Low"]


def test_jobs_board_view_groups_by_status(api_client):
    conn = _conn(api_client)
    seed_job(conn, company="Alpha", status="Ready to apply")
    seed_job(conn, company="Beta", status="Applied")
    body = api_client.get("/api/jobs?view=board").json()
    assert "board" in body
    assert "Ready to apply" in body["board"]
    assert {c["company"] for c in body["board"]["Ready to apply"]} == {"Alpha"}


def _conn(api_client):
    from pipeline.db import connect
    c = connect(api_client.db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    return c
