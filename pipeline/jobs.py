"""Job records: normalized dedup and insertion."""
import hashlib
import re
import sqlite3
from datetime import datetime

JOB_COLUMNS = ["source", "company", "title", "url", "location", "remote_policy",
               "contract_type", "salary", "description", "language", "posted_date"]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def dedup_hash(company: str, title: str) -> str:
    return hashlib.sha256(f"{_norm(company)}|{_norm(title)}".encode()).hexdigest()


def insert_job(conn: sqlite3.Connection, job: dict) -> tuple[int, bool]:
    """Insert a job unless an equivalent one exists. Returns (job_id, created)."""
    h = dedup_hash(job["company"], job["title"])
    existing = conn.execute(
        "SELECT id FROM jobs WHERE dedup_hash = ?", (h,)
    ).fetchone()
    if existing:
        _backfill(conn, existing["id"], job)
        return existing["id"], False
    values = {c: job.get(c) for c in JOB_COLUMNS}
    values["dedup_hash"] = h
    # 'job' (student jobs) unless a dev-track source tags it 'travail'.
    values["track"] = job.get("track") or "job"
    # `or`, not a .get default: callers may pass an explicit None/empty date
    values["discovered_date"] = (
        job.get("discovered_date") or datetime.now().date().isoformat()
    )
    cols = ", ".join(values)
    placeholders = ", ".join(f":{c}" for c in values)
    cur = conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({placeholders})", values)
    conn.commit()
    return cur.lastrowid, True


def _backfill(conn: sqlite3.Connection, job_id: int, job: dict) -> None:
    """Fill NULL/empty columns from a rediscovered posting; never overwrite data."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    updates = {c: job[c] for c in JOB_COLUMNS if job.get(c) and not row[c]}
    if not updates:
        return
    assignments = ", ".join(f"{c} = :{c}" for c in updates)
    updates["id"] = job_id
    conn.execute(f"UPDATE jobs SET {assignments} WHERE id = :id", updates)
    conn.commit()
