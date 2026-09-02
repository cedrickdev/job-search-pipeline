"""One-time import of the legacy Gmail-derived Excel tracker into SQLite.

Usage: .venv/bin/python -m pipeline.migrate_tracker
"""
import json
import re
from datetime import datetime
from pathlib import Path

import openpyxl

from pipeline import paths
from pipeline.db import connect, init_db
from pipeline.jobs import insert_job
from pipeline.statuses import create_application, log_event

STATUS_MAP = {
    "Recruiter Outreach": "Recruiter reply",
    "Interview Scheduled": "Interview scheduled",
    "Interview": "Interview scheduled",
    "Applied": "Applied",
    "Rejected": "Rejected",
    "Offer": "Offer",
    "Ghosted": "Ghosted",
    "Withdrawn": "Withdrawn",
}
FALLBACK_STATUS = "Recruiter reply"


def _cell(v) -> str:
    if isinstance(v, datetime):
        return v.date().isoformat()
    return "" if v is None else str(v).strip()


def _norm_header(h: str) -> str:
    """'Role / Position' and 'Role/Position' must map to the same key."""
    return re.sub(r"\s*/\s*", "/", h)


def migrate(conn, xlsx_path: str | Path) -> dict:
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["Tracker"]
    rows = list(ws.iter_rows(values_only=True))
    header_idx = next(i for i, r in enumerate(rows) if _cell(r[0]) == "Date Received")
    header = [_norm_header(_cell(c)) for c in rows[header_idx]]
    imported, skipped, unmapped = 0, 0, []
    for raw in rows[header_idx + 1:]:
        rec = dict(zip(header, [_cell(c) for c in raw]))
        if not rec.get("Company"):
            continue
        job_id, created = insert_job(conn, {
            "company": rec["Company"],
            "title": rec.get("Role/Position", ""),
            "source": "gmail-import",
            "location": rec.get("Location") or None,
            "salary": rec.get("Salary/Comp") or None,
            "discovered_date": rec.get("Date Received") or None,
        })
        if not created:
            skipped += 1
            continue
        original = rec.get("Status", "")
        status = STATUS_MAP.get(original)
        if status is None:
            status = FALLBACK_STATUS
            unmapped.append(original)
        app_id = create_application(conn, job_id, source="migration", status=status)
        log_event(conn, "imported", "migration", json.dumps(rec, ensure_ascii=False),
                  application_id=app_id, job_id=job_id)
        conn.commit()
        imported += 1
    return {"imported": imported, "skipped": skipped, "unmapped": sorted(set(unmapped))}


if __name__ == "__main__":
    conn = connect(paths.DB_PATH)
    init_db(conn)
    report = migrate(conn, paths.EXPORT_PATH)
    print(json.dumps(report, indent=2, ensure_ascii=False))
