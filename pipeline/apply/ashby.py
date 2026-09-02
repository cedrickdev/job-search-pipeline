"""Ashby ATS application handler (jobs.ashbyhq.com)."""
from __future__ import annotations

import asyncio
import random

from pipeline.apply._common import (
    ApplyResult, cv_path_for_job, fill_field_by_label,
    fill_screening_questions, screenshot_path, upload_cv,
)

TIMEOUT = 8000


async def apply(page, job: dict, answers: dict) -> ApplyResult:
    p = answers["personal"]
    job_id = job["job_id"]

    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    # Ashby job pages have "Overview" / "Application" tabs — click Application tab
    try:
        app_tab = page.locator(
            "a:has-text('Application'), button:has-text('Application')"
        ).first
        if await app_tab.count():
            await app_tab.click(timeout=5000)
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass

    for label, value in [
        ("First Name", p["name"].split()[0]),
        ("Last Name", " ".join(p["name"].split()[1:])),
        ("Email", p["email"]),
        ("Phone", p["phone"]),
        ("LinkedIn", p.get("linkedin_url", "")),
    ]:
        await fill_field_by_label(page, label, value)

    # Upload resume
    try:
        cv = cv_path_for_job(job, answers)
        await upload_cv(page, cv, timeout=TIMEOUT)
    except Exception as exc:
        shot = screenshot_path(job_id, "ashby_upload_fail")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", f"CV upload failed: {exc}", str(shot))

    await fill_screening_questions(page, answers)

    # Submit
    try:
        submit = page.locator("button[type='submit'],"
                              "button:has-text('Submit'),"
                              "button:has-text('Envoyer')").first
        await asyncio.sleep(random.uniform(3.0, 8.0))
        await submit.click(timeout=TIMEOUT)
        # Ashby is a SPA — networkidle may never fire; use domcontentloaded
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
    except Exception as exc:
        shot = screenshot_path(job_id, "ashby_submit_fail")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", f"Submit failed: {exc}", str(shot))

    shot = screenshot_path(job_id, "ashby_done")
    await page.screenshot(path=str(shot))

    content = await page.content()
    if any(kw in content.lower() for kw in ("thank", "merci", "submitted", "received", "confirmation")):
        return ApplyResult("Applied", "Ashby: submitted", str(shot), "ashby")
    return ApplyResult("Needs you", "Ashby: submitted but confirmation unclear", str(shot))
