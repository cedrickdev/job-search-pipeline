"""Morning digest: one markdown file summarizing the run for the candidate.
Plain prose with colons, never em-dashes (it has to pass the project's own
style bar, cv/style_rules.yaml)."""
import json
from datetime import date
from pathlib import Path

from pipeline import paths
from pipeline.db import connect, init_db

QUEUE_STATUSES = ["Ready to apply", "Borderline", "Discovered",
                  "Approved", "Needs you"]


def build_digest(conn) -> str:
    lines = ["# Morning digest", ""]

    run = conn.execute(
        "SELECT * FROM runs WHERE kind = 'discovery'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    if run is None:
        lines.append("No discovery run recorded yet.")
        return "\n".join(lines)

    summary = json.loads(run["summary"] or "{}")
    lines.append(f"Last discovery: {run['finished_at']}"
                 f" (lookback {summary.get('lookback_days', '?')} days,"
                 f" {summary.get('new_jobs', 0)} new jobs)")
    lines.append("")
    lines.append("## Source health")
    for source, report in summary.get("health", {}).items():
        if report["ok"]:
            lines.append(f"- {source}: ok, {report['found']} found,"
                         f" {report['new']} new")
        else:
            errors = "; ".join(report["errors"])
            lines.append(f"- {source}: FAILED ({errors})")
    lines.append("")

    lines.append("## Queue")
    for status in QUEUE_STATUSES:
        count = conn.execute(
            "SELECT COUNT(*) FROM applications WHERE status = ?",
            (status,)).fetchone()[0]
        lines.append(f"- {status}: {count}")
    lines.append("")

    awaiting = conn.execute(
        "SELECT j.company, j.title, j.url FROM applications a"
        " JOIN jobs j ON j.id = a.job_id"
        " WHERE a.status = 'Ready to apply' ORDER BY a.id").fetchall()
    lines.append("## Awaiting your GO")
    if awaiting:
        for row in awaiting:
            lines.append(f"- {row['company']}: {row['title']} ({row['url']})")
    else:
        lines.append("Nothing ready today.")

    today = date.today().isoformat()
    auto = conn.execute(
        "SELECT j.company, j.title FROM events e"
        " JOIN applications a ON a.id = e.application_id"
        " JOIN jobs j ON j.id = a.job_id"
        " WHERE e.event_type = 'status_change' AND e.source = 'auto-approve'"
        " AND e.created_at LIKE ? ORDER BY e.id", (f"{today}%",)).fetchall()
    if auto:
        lines.append("")
        lines.append("## Auto-approved today")
        for row in auto:
            lines.append(f"- {row['company']}: {row['title']}")

    return "\n".join(lines)


def write_digest(conn) -> Path:
    paths.DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    paths.DIGEST_PATH.write_text(build_digest(conn))
    return paths.DIGEST_PATH


if __name__ == "__main__":
    connection = connect(paths.DB_PATH)
    init_db(connection)
    path = write_digest(connection)
    print(path)
    print()
    print(path.read_text())
