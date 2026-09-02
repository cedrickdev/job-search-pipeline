"""CV PDF serving: path-traversal guard + 404s (spec §5.4 files)."""
from pipeline import paths
from tests.helpers import seed_job, seed_cv_version


def _conn(api_client):
    from pipeline.db import connect
    c = connect(api_client.db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def test_missing_cv_404(api_client):
    assert api_client.get("/api/files/cv/999").status_code == 404


def test_cv_path_outside_versions_dir_404(api_client):
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    cv_id = seed_cv_version(conn, job_id, pdf_path="/tmp/escape.pdf")  # outside CV_VERSIONS_DIR
    assert api_client.get(f"/api/files/cv/{cv_id}").status_code == 404


def test_cv_served_when_inside_versions_dir(api_client, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CV_VERSIONS_DIR", tmp_path)
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 minimal")
    conn = _conn(api_client)
    job_id, _ = seed_job(conn)
    cv_id = seed_cv_version(conn, job_id, pdf_path=str(pdf))
    r = api_client.get(f"/api/files/cv/{cv_id}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
