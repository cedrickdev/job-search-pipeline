"""Phase 4 Playwright applier.

Polls /api/approved, applies to each job, updates the DB.

Usage:
    python -m pipeline.applier [--headless] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sqlite3
import time
from datetime import datetime

from pipeline import paths
from pipeline.apply._common import detect_platform, load_answers
from pipeline.apply import _get_handler, ApplyResult
from pipeline.db import connect, init_db
from pipeline.statuses import set_status, log_event

SOURCE = "applier"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _approved_jobs(conn: sqlite3.Connection) -> list[dict]:
    """Return all applications currently in Approved status with CV paths."""
    rows = conn.execute(
        "SELECT a.id AS application_id, j.id AS job_id, j.company, j.title,"
        " j.url, j.language"
        " FROM applications a JOIN jobs j ON j.id = a.job_id"
        " WHERE a.status = 'Approved' ORDER BY a.id"
    ).fetchall()
    result = []
    for row in rows:
        job = dict(row)
        cv_pdf: dict[str, str | None] = {}
        for lang in ("en", "fr"):
            cv = conn.execute(
                "SELECT pdf_path FROM cv_versions WHERE job_id = ?"
                " AND language = ? ORDER BY id DESC LIMIT 1",
                (job["job_id"], lang),
            ).fetchone()
            cv_pdf[lang] = cv["pdf_path"] if cv else None
        job["cv_pdf"] = cv_pdf
        cover_letter: dict[str, str | None] = {}
        for lang in ("en", "fr"):
            cl = paths.COVER_LETTERS_DIR / f"{job['job_id']}_{lang}.md"
            cover_letter[lang] = str(cl) if cl.exists() else None
        job["cover_letter"] = cover_letter
        result.append(job)
    return result


def _record_result(conn: sqlite3.Connection, job: dict, result: ApplyResult) -> None:
    app_id = job["application_id"]
    set_status(conn, app_id, result.status, source=SOURCE, detail=result.detail)
    if result.status == "Applied":
        conn.execute(
            "UPDATE applications SET submitted_at = ?, channel = ?,"
            " confirmation_screenshot = ? WHERE id = ?",
            (_now(), result.channel, result.screenshot_path, app_id),
        )
    elif result.screenshot_path:
        conn.execute(
            "UPDATE applications SET confirmation_screenshot = ? WHERE id = ?",
            (result.screenshot_path, app_id),
        )
    log_event(conn, "apply_attempt", SOURCE,
              detail=result.detail, application_id=app_id, job_id=job["job_id"])
    conn.commit()


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    " AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/136.0.0.0 Safari/537.36"
)
_VIEWPORT = {"width": 1440, "height": 900}
_LAUNCH_ARGS = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
# The browser has to look like the candidate's own: a Swiss portal (jobup.ch,
# Umantis) seeing a French locale on a Swiss connection is an inconsistency.
# Same UTC offset as Europe/Paris, so nothing about timing changes.
_LOCALE = "fr-CH"
_TIMEZONE = "Europe/Zurich"


async def _apply_one(job: dict, answers: dict, headless: bool) -> ApplyResult:
    from playwright.async_api import async_playwright
    from pipeline.apply._common import screenshot_path
    platform = detect_platform(job["url"] or "")
    handler = _get_handler(platform)

    async with async_playwright() as p:
        if platform in ("linkedin", "wtj", "jobup"):
            # Persistent context preserves login session (LinkedIn, WTJ, jobup)
            if platform == "linkedin":
                from pipeline.apply.linkedin import SESSION_DIR
                SESSION_DIR.mkdir(parents=True, exist_ok=True)
                context = await p.chromium.launch_persistent_context(
                    str(SESSION_DIR),
                    headless=headless,
                    args=_LAUNCH_ARGS,
                    user_agent=_USER_AGENT,
                    viewport=_VIEWPORT,
                    locale=_LOCALE,
                    timezone_id=_TIMEZONE,
                )
            else:
                # WTJ/jobup: load session from storage_state.json saved by
                # scripts/wtj_login.py or scripts/jobup_login.py
                if platform == "wtj":
                    from pipeline.apply.wtj import SESSION_DIR
                else:
                    from pipeline.apply.jobup import SESSION_DIR
                storage = SESSION_DIR / "storage_state.json"
                browser = await p.chromium.launch(headless=headless, args=_LAUNCH_ARGS)
                context = await browser.new_context(
                    storage_state=str(storage) if storage.exists() else None,
                    user_agent=_USER_AGENT,
                    viewport=_VIEWPORT,
                    locale=_LOCALE,
                    timezone_id=_TIMEZONE,
                )
            page = await context.new_page()
            try:
                result = await handler.apply(page, job, answers)
            except Exception as exc:
                shot = screenshot_path(job["job_id"], f"{platform}_exception")
                try:
                    await page.screenshot(path=str(shot))
                except Exception:
                    pass
                result = ApplyResult("Needs you", f"Unhandled exception: {exc}", str(shot))
            finally:
                await context.close()
        else:
            browser = await p.chromium.launch(headless=headless, args=_LAUNCH_ARGS)
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport=_VIEWPORT,
                locale=_LOCALE,
                timezone_id=_TIMEZONE,
            )
            page = await context.new_page()
            try:
                result = await handler.apply(page, job, answers)
            except Exception as exc:
                shot = screenshot_path(job["job_id"], f"{platform}_exception")
                try:
                    await page.screenshot(path=str(shot))
                except Exception:
                    pass
                result = ApplyResult("Needs you", f"Unhandled exception: {exc}", str(shot))
            finally:
                await browser.close()
    return result


def run(headless: bool = False, dry_run: bool = False) -> list[dict]:
    """Apply to all Approved jobs. Returns list of result dicts."""
    answers = load_answers()
    conn = connect(paths.DB_PATH)
    init_db(conn)
    jobs = _approved_jobs(conn)
    if not jobs:
        print("No jobs in Approved status.")
        conn.close()
        return []

    results = []
    for job in jobs:
        platform = detect_platform(job["url"] or "")
        print(f"[{job['job_id']}] {job['company']} — {job['title']} ({platform})")
        if dry_run:
            print("  [DRY RUN] skipping")
            results.append({"job_id": job["job_id"], "status": "dry-run"})
            continue
        result = asyncio.run(_apply_one(job, answers, headless=headless))
        print(f"  -> {result.status}: {result.detail}")
        _record_result(conn, job, result)
        results.append({
            "job_id": job["job_id"],
            "company": job["company"],
            "status": result.status,
            "detail": result.detail,
            "screenshot": result.screenshot_path,
        })
        if job is not jobs[-1]:
            cooldown = random.uniform(90, 300)
            print(f"  [cooldown {cooldown:.0f}s before next application]")
            time.sleep(cooldown)

    conn.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply to all Approved jobs")
    parser.add_argument("--headless", action="store_true",
                        help="Run Chromium headlessly (no visible window)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List approved jobs without submitting")
    args = parser.parse_args()
    outcomes = run(headless=args.headless, dry_run=args.dry_run)
    applied = sum(1 for r in outcomes if r.get("status") == "Applied")
    needs_you = sum(1 for r in outcomes if r.get("status") == "Needs you")
    print(f"\nDone: {applied} applied, {needs_you} need manual action.")


if __name__ == "__main__":
    main()
