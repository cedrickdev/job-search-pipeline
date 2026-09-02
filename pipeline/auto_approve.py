"""Deterministic auto-approval. The morning run calls this after tailoring;
with auto_apply off (the default) it is a no-op."""
import argparse
import json
import sqlite3
from datetime import date

from pipeline import paths
from pipeline.db import connect, init_db
from pipeline.settings import load
from pipeline.statuses import set_status

SOURCE = "auto-approve"


def approved_today_count(conn: sqlite3.Connection) -> int:
    today = date.today().isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'status_change'"
        " AND source = ? AND created_at LIKE ?",
        (SOURCE, f"{today}%")).fetchone()[0]


def eligible(conn: sqlite3.Connection, min_score: int) -> list[dict]:
    rows = conn.execute(
        "SELECT a.id AS application_id, j.id AS job_id, j.company, j.title,"
        " (SELECT s.score FROM scores s WHERE s.job_id = j.id"
        "  ORDER BY s.id DESC LIMIT 1) AS score"
        " FROM applications a JOIN jobs j ON j.id = a.job_id"
        " WHERE a.status = 'Ready to apply'").fetchall()
    matches = [dict(r) for r in rows
               if r["score"] is not None and r["score"] >= min_score]
    matches.sort(key=lambda r: r["score"], reverse=True)
    return matches


def run(conn: sqlite3.Connection, settings: dict | None = None) -> list[dict]:
    settings = settings if settings is not None else load()
    if not settings["auto_apply"]:
        return []
    remaining = settings["auto_apply_daily_cap"] - approved_today_count(conn)
    if remaining <= 0:
        return []
    approved = []
    for row in eligible(conn, settings["auto_apply_min_score"])[:remaining]:
        set_status(conn, row["application_id"], "Approved", source=SOURCE,
                   detail=f"auto-approved at score {row['score']}")
        approved.append(row)
    return approved


def main() -> None:
    argparse.ArgumentParser(description="Auto-approve per settings").parse_args()
    conn = connect(paths.DB_PATH)
    init_db(conn)
    print(json.dumps(run(conn), indent=2))


if __name__ == "__main__":
    main()
