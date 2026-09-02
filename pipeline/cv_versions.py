"""Traceability: every generated CV is registered and linkable to applications."""
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path


def content_hash(content: dict) -> str:
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def register_cv_version(conn: sqlite3.Connection, job_id: int | None, language: str,
                        pdf_path: str | Path, content: dict,
                        diff_summary: str | None = None,
                        phone_screen_pct: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO cv_versions (job_id, language, pdf_path, content_hash,"
        " diff_summary, created_at, phone_screen_pct) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job_id, language, str(pdf_path), content_hash(content), diff_summary,
         datetime.now().isoformat(timespec="seconds"), phone_screen_pct),
    )
    conn.commit()
    return cur.lastrowid
