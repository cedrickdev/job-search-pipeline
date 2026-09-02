import json

from pipeline import paths
from pipeline.digest import build_digest, write_digest
from pipeline.discover import record_run
from pipeline.jobs import insert_job
from pipeline.statuses import create_application, set_status


def _seed(conn):
    summary = {"lookback_days": 3, "new_jobs": 2, "health": {
        "greenhouse": {"ok": True, "found": 12, "new": 2, "errors": []},
        "linkedin": {"ok": False, "found": 0, "new": 0,
                     "errors": ["Sales @ Lausanne: 403"]},
    }}
    record_run(conn, "discovery", "2026-06-11T06:00:00+00:00",
               json.dumps(summary))
    job_id, _ = insert_job(conn, {"source": "greenhouse", "company": "Acme",
                                  "title": "Senior Sales Assistant",
                                  "url": "https://x.test/1"})
    app_id = create_application(conn, job_id, source="discovery")
    set_status(conn, app_id, "Ready to apply", source="tailoring")
    return job_id


def test_build_digest_shows_health_and_queue(conn):
    _seed(conn)
    text = build_digest(conn)
    assert "greenhouse: ok" in text
    assert "linkedin: FAILED" in text
    assert "403" in text
    assert "Ready to apply: 1" in text
    assert "Acme" in text and "Senior Sales Assistant" in text
    assert "—" not in text  # the digest passes the project's style bar


def test_build_digest_without_runs(conn):
    text = build_digest(conn)
    assert "No discovery run recorded yet" in text


def test_write_digest_creates_file(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DIGEST_PATH", tmp_path / "digest.md")
    _seed(conn)
    path = write_digest(conn)
    assert path.read_text().startswith("# Morning digest")
