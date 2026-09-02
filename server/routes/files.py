"""Serve registered CV PDFs, guarding against path traversal (must live under CV_VERSIONS_DIR)."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from pipeline import paths
from server.deps import get_conn

router = APIRouter(prefix="/api/files")


@router.get("/cv/{cv_id}")
def get_cv_pdf(cv_id: int, conn=Depends(get_conn)):
    row = conn.execute(
        "SELECT pdf_path FROM cv_versions WHERE id=?", (cv_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "not found")
    resolved = Path(row["pdf_path"]).resolve()
    base = paths.CV_VERSIONS_DIR.resolve()
    if not (resolved.is_relative_to(base) and resolved.is_file()):
        raise HTTPException(404, "not found")
    return FileResponse(resolved, media_type="application/pdf")
