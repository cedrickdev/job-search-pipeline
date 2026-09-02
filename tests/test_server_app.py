# tests/test_server_app.py
"""server.app.create_app factory: health, 503 handler, settings_path threading."""
import sqlite3
import pytest


def test_app_boots_and_serves_openapi(api_client):
    r = api_client.get("/openapi.json")
    assert r.status_code == 200
    assert r.json()["info"]["title"] == "Job Search Command Center"


def test_operational_error_maps_to_503(api_client):
    @api_client.app.get("/api/_boom")
    def _boom():
        raise sqlite3.OperationalError("database is locked")
    r = api_client.get("/api/_boom")
    assert r.status_code == 503
    assert r.json()["error"] == "db_busy"


def test_db_path_from_env(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from pipeline.db import connect, init_db
    db = tmp_path / "env.db"
    init_db(connect(db))  # create
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db))
    from server.app import create_app
    with TestClient(create_app()) as c:
        assert c.app.state.db_path == db


def _spa_app(tmp_path, *, with_index=True):
    """Build an app whose SPA dist lives in tmp_path so tests need no real build."""
    from fastapi.testclient import TestClient
    from pipeline.db import connect, init_db
    from server.app import create_app
    db = tmp_path / "spa.db"
    init_db(connect(db))
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    if with_index:
        (dist / "index.html").write_text(
            "<!doctype html><div id=root>SPA-SHELL</div>")
        (dist / "assets" / "app.js").write_text("console.log('app')")
    return TestClient(create_app(db_path=db, spa_dist=dist))


def test_spa_served_at_root(tmp_path):
    """create_app serves the built SPA index.html at /."""
    with _spa_app(tmp_path) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "SPA-SHELL" in r.text


def test_spa_fallback_for_client_routes(tmp_path):
    """Deep-linked client routes fall back to index.html (SPA routing)."""
    with _spa_app(tmp_path) as c:
        r = c.get("/jobs")
        assert r.status_code == 200
        assert "SPA-SHELL" in r.text


def test_spa_assets_served(tmp_path):
    """Hashed bundles under /assets are served as static files."""
    with _spa_app(tmp_path) as c:
        r = c.get("/assets/app.js")
        assert r.status_code == 200
        assert "console.log" in r.text


def test_api_namespace_not_shadowed_by_spa(tmp_path):
    """Unknown /api paths return JSON 404, never the SPA shell."""
    with _spa_app(tmp_path) as c:
        r = c.get("/api/does-not-exist")
        assert r.status_code == 404
        assert "SPA-SHELL" not in r.text


def test_api_routes_still_work_with_spa_mounted(tmp_path):
    """Real API routes keep precedence over the SPA catch-all."""
    with _spa_app(tmp_path) as c:
        assert c.get("/openapi.json").status_code == 200


def test_app_boots_without_spa_build(tmp_path):
    """When the SPA build is absent, the API still boots; / yields 404 JSON."""
    with _spa_app(tmp_path, with_index=False) as c:
        assert c.get("/openapi.json").status_code == 200
        r = c.get("/")
        assert r.status_code == 404
