"""Preserved /api/approved (ad-hoc/script compat; not the applier's real dep)."""
from fastapi import APIRouter, Depends

from pipeline import paths
from server.deps import get_conn

router = APIRouter(prefix="/api")


@router.get("/approved")
def get_approved(conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT a.id AS application_id, a.job_id,"
        " j.company, j.title, j.url, j.language AS job_language"
        " FROM applications a JOIN jobs j ON j.id = a.job_id"
        " WHERE a.status = 'Approved' ORDER BY a.id DESC").fetchall()
    out = []
    for r in rows:
        job_id = r["job_id"]
        language = r["job_language"] or "en"
        approved_at = conn.execute(
            "SELECT created_at FROM events WHERE application_id=?"
            " AND event_type='status_change' ORDER BY id DESC LIMIT 1",
            (r["application_id"],)).fetchone()
        cv_pdf = {}
        for lang in ("en", "fr"):
            cv = conn.execute(
                "SELECT id FROM cv_versions WHERE job_id=? AND language=?"
                " ORDER BY id DESC LIMIT 1", (job_id, lang)).fetchone()
            if cv:
                cv_pdf[lang] = f"/api/files/cv/{cv['id']}"
        cover = {}
        for lang in ("en", "fr"):
            path = paths.COVER_LETTERS_DIR / f"{job_id}_{lang}.md"
            if path.is_file():
                cover[lang] = str(path)
        out.append({
            "application_id": r["application_id"],
            "job_id": job_id,
            "company": r["company"],
            "title": r["title"],
            "url": r["url"],
            "language": language,
            "cv_pdf": cv_pdf,
            "cover_letter": cover,
            "approved_at": approved_at["created_at"] if approved_at else None,
        })
    return out
