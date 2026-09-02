"""Deterministic I/O around scoring. The judgment itself comes from Claude in
the morning run (headless claude -p on the subscription — never an API key)."""
import argparse
import json
from datetime import datetime

from pipeline import paths
from pipeline.db import connect, init_db
from pipeline.statuses import set_status

THRESHOLD_TAILOR = 70
THRESHOLD_BORDERLINE = 50


def jobs_needing_score(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT j.*, a.id AS application_id FROM jobs j"
        " JOIN applications a ON a.job_id = j.id"
        " WHERE a.status = 'Discovered' ORDER BY j.id").fetchall()
    return [dict(r) for r in rows]


def record_score(conn, job_id: int, score: int, reasoning: str,
                 red_flags: str | None = None,
                 scorer_version: str = "v1") -> str:
    app = conn.execute("SELECT id FROM applications WHERE job_id = ?",
                       (job_id,)).fetchone()
    if app is None:
        raise ValueError(f"no application for job {job_id}")
    created_at = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO scores (job_id, score, reasoning, red_flags, scorer_version, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, score, reasoning, red_flags, scorer_version, created_at))
    conn.commit()
    if score >= THRESHOLD_TAILOR:
        status = "Scored"
    elif score >= THRESHOLD_BORDERLINE:
        status = "Borderline"
    else:
        status = "Archived"
    set_status(conn, app["id"], status, source="scoring",
               detail=f"score={score}")
    return status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scoring I/O for the morning run.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="print jobs awaiting a score as JSON")
    rec = sub.add_parser("record", help="record a score for a job")
    rec.add_argument("--job-id", type=int, required=True)
    rec.add_argument("--score", type=int, required=True)
    rec.add_argument("--reasoning", required=True)
    rec.add_argument("--red-flags", default=None)
    args = parser.parse_args()

    connection = connect(paths.DB_PATH)
    init_db(connection)
    if args.command == "list":
        print(json.dumps(jobs_needing_score(connection), indent=2))
    else:
        status = record_score(connection, args.job_id, args.score,
                              args.reasoning, red_flags=args.red_flags)
        print(json.dumps({"job_id": args.job_id, "status": status}))
