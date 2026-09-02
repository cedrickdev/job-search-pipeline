"""Gmail recruiter follow-up loop (SMTP + App Password).

Setup:
    export GMAIL_USER=you@gmail.com
    export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"

Usage:
    python -m pipeline.gmail_recruiter          # check + send pending follow-ups
    python -m pipeline.gmail_recruiter --list   # list pending, no send
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sqlite3
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from pipeline import paths
from pipeline.apply._common import load_answers
from pipeline.db import connect, init_db
from pipeline.statuses import log_event

SOURCE = "gmail-recruiter"

FOLLOWUP_EN = """\
Dear Hiring Team,

I wanted to follow up on my application for the {title} position at {company}\
{submitted_clause}.

I remain very interested in this opportunity and would love to discuss how my \
background could contribute to your team.

Please let me know if you need any additional information. I look forward to hearing \
from you.

Best regards,
{name}
{phone} | {email}
"""

FOLLOWUP_FR = """\
Madame, Monsieur,

Je me permets de revenir vers vous concernant ma candidature au poste de {title} \
chez {company}{submitted_clause}.

Je reste très intéressé(e) par cette opportunité et serais ravi(e) d'échanger sur \
la façon dont mon expérience pourrait bénéficier à votre équipe.

N'hésitez pas à me contacter si vous souhaitez des informations complémentaires.

Cordialement,
{name}
{phone} | {email}
"""


def submitted_clause(submitted_date: str, lang: str) -> str:
    """Optional submission-date clause for the follow-up templates.

    Returns an empty string when no date is known (e.g. imported applications),
    so the sentence stays grammatical instead of trailing 'submitted on .'.
    """
    if not submitted_date:
        return ""
    return (f", déposée le {submitted_date}" if lang == "fr"
            else f", submitted on {submitted_date}")


def _get_credentials(answers: dict) -> tuple[str, str]:
    cfg = answers.get("gmail", {})
    user = os.environ.get(cfg.get("user_env", "GMAIL_USER"), "")
    password = os.environ.get(cfg.get("password_env", "GMAIL_APP_PASSWORD"), "")
    if not user or not password:
        raise EnvironmentError(
            "Set GMAIL_USER and GMAIL_APP_PASSWORD environment variables before running."
        )
    return user, password


def send_email(to: str, subject: str, body: str, answers: dict) -> None:
    user, password = _get_credentials(answers)
    msg = MIMEMultipart("alternative")
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(user, password)
        server.send_message(msg)


def _pending_followups(conn: sqlite3.Connection, delay_days: int,
                       max_followups: int) -> list[dict]:
    """Return Applied jobs with recruiter_email set, past delay, under follow-up cap."""
    cutoff = (datetime.now() - timedelta(days=delay_days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT a.id AS application_id, j.id AS job_id, j.company, j.title,"
        " j.language, a.submitted_at, a.recruiter_email"
        " FROM applications a JOIN jobs j ON j.id = a.job_id"
        " WHERE a.status = 'Applied'"
        "   AND a.recruiter_email IS NOT NULL AND a.recruiter_email != ''"
        "   AND a.submitted_at < ?"
        " ORDER BY a.submitted_at",
        (cutoff,),
    ).fetchall()
    pending = []
    for row in rows:
        app_id = row["application_id"]
        followup_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE application_id = ?"
            " AND event_type = 'followup_sent' AND source = ?",
            (app_id, SOURCE),
        ).fetchone()[0]
        # Also skip if recruiter replied
        replied = conn.execute(
            "SELECT COUNT(*) FROM events WHERE application_id = ?"
            " AND event_type = 'status_change'"
            " AND detail LIKE '%Recruiter reply%'",
            (app_id,),
        ).fetchone()[0]
        if followup_count < max_followups and not replied:
            pending.append(dict(row))
    return pending


def run(dry_run: bool = False) -> list[dict]:
    answers = load_answers()
    cfg = answers.get("gmail", {})
    delay_days = int(cfg.get("followup_delay_days", 7))
    max_followups = int(cfg.get("max_followups", 2))
    p = answers["personal"]

    conn = connect(paths.DB_PATH)
    init_db(conn)
    pending = _pending_followups(conn, delay_days, max_followups)

    if not pending:
        print("No follow-ups pending.")
        conn.close()
        return []

    sent = []
    for job in pending:
        lang = job.get("language") or "fr"
        template = FOLLOWUP_FR if lang == "fr" else FOLLOWUP_EN
        submitted_date = (job["submitted_at"] or "")[:10]
        body = template.format(
            name=p["name"],
            title=job["title"],
            company=job["company"],
            submitted_clause=submitted_clause(submitted_date, lang),
            phone=p["phone"],
            email=p["email"],
        )
        subject = (
            f"Suivi candidature — {job['title']} ({job['company']})"
            if lang == "fr"
            else f"Follow-up: {job['title']} application at {job['company']}"
        )
        to = job["recruiter_email"]
        print(f"[{job['job_id']}] Follow-up to {to}: {subject}")
        if dry_run:
            print("  [DRY RUN] not sent")
            sent.append({"job_id": job["job_id"], "status": "dry-run", "to": to})
            continue
        try:
            send_email(to, subject, body, answers)
            log_event(conn, "followup_sent", SOURCE,
                      detail=f"Sent to {to}", application_id=job["application_id"],
                      job_id=job["job_id"])
            conn.commit()
            print("  Sent.")
            sent.append({"job_id": job["job_id"], "status": "sent", "to": to})
        except Exception as exc:
            print(f"  ERROR: {exc}")
            sent.append({"job_id": job["job_id"], "status": "error", "error": str(exc)})

    conn.close()
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description="Send recruiter follow-ups")
    parser.add_argument("--list", action="store_true",
                        help="List pending follow-ups without sending")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print emails without sending (same as --list + preview)")
    args = parser.parse_args()
    results = run(dry_run=args.list or args.dry_run)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
