# tests/test_cv_versions.py
from pipeline.cv_versions import register_cv_version
from pipeline.jobs import insert_job


def test_register_links_cv_to_job_with_stable_hash(conn, tmp_path):
    jid, _ = insert_job(conn, {"company": "Acme", "title": "Senior Sales", "source": "wtj"})
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-fake")
    content = {"summary": {"en": "tailored"}}
    v1 = register_cv_version(conn, jid, "en", pdf, content, "reordered service bullets")
    v2 = register_cv_version(conn, jid, "en", pdf, content, "same content again")
    rows = conn.execute("SELECT * FROM cv_versions WHERE job_id = ?", (jid,)).fetchall()
    assert len(rows) == 2
    assert rows[0]["content_hash"] == rows[1]["content_hash"]
    assert rows[0]["diff_summary"] == "reordered service bullets"
    assert v1 != v2
