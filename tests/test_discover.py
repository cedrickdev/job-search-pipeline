from pipeline.discover import lookback_days, record_run, run_discovery
from pipeline.http_fetch import FetchError
from pipeline.sources._common import normalize

SEARCHES = {
    "queries": ["vendeur"],
    "locations": ["Yverdon-les-Bains, Suisse"],
    "title_keywords": ["vendeur", "serveur"],
    "exclude_keywords": ["intern"],
}
COMPANIES = {"greenhouse": [{"token": "acme", "company": "Acme"}]}


def _fake_ats(token, company):
    return [
        normalize(source="greenhouse", company=company,
                  title="Vendeur polyvalent", url="https://x.test/1",
                  description="Serve customers."),
        normalize(source="greenhouse", company=company,
                  title="Office Manager", url="https://x.test/2"),
    ]


def _fake_query(query, location, lookback_days=3):
    return [normalize(source="wtj", company="Globex",
                      title="Serveur extra", url="https://x.test/3")]


def _broken_query(query, location, lookback_days=3):
    raise FetchError("boom")


def test_run_discovery_inserts_filters_and_reports_health(conn, monkeypatch):
    import pipeline.discover as discover
    monkeypatch.setattr(discover, "ATS_SOURCES", {"greenhouse": _fake_ats})
    monkeypatch.setattr(discover, "QUERY_SOURCES",
                        {"wtj": _fake_query, "linkedin": _broken_query})
    monkeypatch.setattr(discover, "DESCRIPTION_FETCHERS", {})

    summary = run_discovery(conn, SEARCHES, COMPANIES, lookback=3)

    assert summary["new_jobs"] == 2  # office manager filtered out
    assert summary["health"]["greenhouse"]["ok"] is True
    assert summary["health"]["greenhouse"]["new"] == 1
    assert summary["health"]["wtj"]["ok"] is True
    assert summary["health"]["linkedin"]["ok"] is False
    assert "boom" in summary["health"]["linkedin"]["errors"][0]

    n_apps = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status = 'Discovered'"
    ).fetchone()[0]
    assert n_apps == 2
    n_runs = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE kind = 'discovery'").fetchone()[0]
    assert n_runs == 1


def test_run_discovery_is_idempotent(conn, monkeypatch):
    import pipeline.discover as discover
    monkeypatch.setattr(discover, "ATS_SOURCES", {"greenhouse": _fake_ats})
    monkeypatch.setattr(discover, "QUERY_SOURCES", {})
    monkeypatch.setattr(discover, "DESCRIPTION_FETCHERS", {})
    run_discovery(conn, SEARCHES, COMPANIES, lookback=3)
    second = run_discovery(conn, SEARCHES, COMPANIES, lookback=3)
    assert second["new_jobs"] == 0
    n_apps = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    assert n_apps == 1


def test_lookback_defaults_and_caps(conn):
    assert lookback_days(conn) == 3  # no prior run
    record_run(conn, "discovery", "2026-06-01T08:00:00", "{}")
    assert lookback_days(conn) <= 7  # capped even after a long gap
