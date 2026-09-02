"""Lever ATS application handler (jobs.lever.co)."""
from __future__ import annotations

import asyncio
import random

from pipeline.apply._common import (
    ApplyResult, cv_path_for_job, cover_letter_path_for_job,
    fill_field_by_label, fill_screening_questions, screenshot_path, upload_cv,
)

TIMEOUT = 8000


async def apply(page, job: dict, answers: dict) -> ApplyResult:
    p = answers["personal"]
    job_id = job["job_id"]

    # Lever job pages show description only; the form lives at {url}/apply
    base_url = job["url"].rstrip("/")
    apply_url = base_url if base_url.endswith("/apply") else base_url + "/apply"
    await page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)

    for label, value in [
        ("Full name", p["name"]),
        ("Email", p["email"]),
        ("Phone", p["phone"]),
        ("LinkedIn URL", p.get("linkedin_url", "")),
        ("Current company", p.get("current_company", "")),
        ("Current location", p["location"]),
    ]:
        await fill_field_by_label(page, label, value)

    # Upload resume
    try:
        cv = cv_path_for_job(job, answers)
        await upload_cv(page, cv, timeout=TIMEOUT)
    except Exception as exc:
        shot = screenshot_path(job_id, "lever_upload_fail")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", f"CV upload failed: {exc}", str(shot))

    await fill_screening_questions(page, answers)

    # Detect CAPTCHA (Arkose/FunCaptcha or hCaptcha) before attempting submit
    page_content = await page.content()
    has_captcha = any(kw in page_content.lower() for kw in (
        "arkoselabs", "funcaptcha", "arkose", "hcaptcha", "h-captcha",
    )) or await page.locator(
        "iframe[src*='arkoselabs'], iframe[src*='funcaptcha'],"
        "div[id*='arkose'], div[id*='funcaptcha']"
    ).count() > 0
    if has_captcha:
        shot = screenshot_path(job_id, "lever_prefilled")
        await page.screenshot(path=str(shot))
        return ApplyResult(
            "Needs you",
            "Lever: form pre-filled. Complete CAPTCHA and submit manually.",
            str(shot),
        )

    # Submit
    try:
        submit = page.locator("button:has-text('Submit application'),"
                              "button:has-text('Submit Application'),"
                              "button:has-text('Envoyer'),"
                              "input[type='submit']").first
        await asyncio.sleep(random.uniform(3.0, 8.0))
        await submit.click(timeout=TIMEOUT)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
    except Exception as exc:
        shot = screenshot_path(job_id, "lever_submit_fail")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", f"Submit failed: {exc}", str(shot))

    # Lever sometimes shows an EEO/demographic follow-up page after main submission.
    # Detect it by the presence of "Prefer not to respond" radio buttons, then submit that too.
    content = await page.content()
    eeo_indicators = ("prefer not to respond", "gender identity", "ethnicity",
                      "veteran status", "disability", "race")
    if any(kw in content.lower() for kw in eeo_indicators):
        # Select "Prefer not to respond" for every unselected radio group
        try:
            radio_groups = page.locator("input[type='radio'][value*='decline'], "
                                        "input[type='radio'][value*='prefer'], "
                                        "input[type='radio'][value*='not_answered'],"
                                        "label:has-text('Prefer not to respond') input[type='radio'],"
                                        "label:has-text('Prefer not to respond')")
            count = await radio_groups.count()
            for i in range(count):
                try:
                    el = radio_groups.nth(i)
                    tag = await el.evaluate("e => e.tagName")
                    if tag == "LABEL":
                        await el.click(timeout=2000)
                    else:
                        await el.check(timeout=2000)
                except Exception:
                    pass
        except Exception:
            pass
        # Submit the EEO form
        try:
            eeo_submit = page.locator("button[type='submit'], input[type='submit'],"
                                      "button:has-text('Submit')").first
            await eeo_submit.click(timeout=TIMEOUT)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
        except Exception:
            pass

    shot = screenshot_path(job_id, "lever_done")
    await page.screenshot(path=str(shot))

    content = await page.content()
    url_after = page.url
    success_kw = ("thank you", "thanks", "merci", "submitted", "received",
                  "application received", "you're all set", "all set",
                  "we'll be in touch", "we'll review")
    if any(kw in url_after.lower() for kw in ("confirmation", "thank", "success")):
        return ApplyResult("Applied", "Lever: submitted", str(shot), "lever")
    if any(kw in content.lower() for kw in success_kw):
        return ApplyResult("Applied", "Lever: submitted", str(shot), "lever")
    # Check for validation errors
    if any(kw in content.lower() for kw in ("required", "obligatoire", "field is required")):
        return ApplyResult("Needs you", "Lever: validation error, required field missing (check screenshot)", str(shot))
    return ApplyResult("Needs you", "Lever: submitted but confirmation unclear", str(shot))
