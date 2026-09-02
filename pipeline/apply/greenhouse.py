"""Greenhouse ATS application handler (boards.greenhouse.io, job-boards.greenhouse.io)."""
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

    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    # Fill personal fields — support both EN and FR Greenhouse variants
    for label, value in [
        ("First Name|Prénom", p["name"].split()[0]),
        ("Last Name|^Nom$|Nom de famille", " ".join(p["name"].split()[1:])),
        ("Email|Adresse e-mail|E-mail", p["email"]),
        ("Phone|Téléphone|Mobile", p["phone"]),
        ("LinkedIn", p.get("linkedin_url", "")),
    ]:
        await fill_field_by_label(page, label, value)

    # Upload resume via file chooser (Greenhouse uses a "Joindre"/"Attach" button)
    try:
        cv = cv_path_for_job(job, answers)
        await upload_cv(page, cv, timeout=TIMEOUT)
    except Exception as exc:
        shot = screenshot_path(job_id, "greenhouse_upload_fail")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", f"CV upload failed: {exc}", str(shot))

    # Cover letter (optional — second file group)
    cl = cover_letter_path_for_job(job, answers)
    if cl:
        try:
            groups = await page.locator(
                "group, [role='group']"
            ).all()
            # Find the cover letter upload trigger (second Joindre button group)
            upload_btns = page.locator("button:has-text('Joindre'), button:has-text('Attach')")
            if await upload_btns.count() > 1:
                async with page.expect_file_chooser(timeout=5000) as fc_info:
                    await upload_btns.nth(1).click(timeout=5000)
                fc = await fc_info.value
                await fc.set_files(str(cl))
        except Exception:
            pass

    # Check required consent checkboxes (Greenhouse often requires these)
    try:
        checkboxes = page.locator("input[type='checkbox']")
        for i in range(await checkboxes.count()):
            cb = checkboxes.nth(i)
            if not await cb.is_checked():
                await cb.check(timeout=3000)
    except Exception:
        pass

    await fill_screening_questions(page, answers)

    # If reCAPTCHA is present, pre-fill is all we can do in headless mode
    page_content = await page.content()
    has_recaptcha = "g-recaptcha" in page_content or "recaptcha" in page_content.lower()
    if has_recaptcha:
        shot = screenshot_path(job_id, "greenhouse_prefilled")
        await page.screenshot(path=str(shot))
        return ApplyResult(
            "Needs you",
            "Greenhouse: form pre-filled. Complete reCAPTCHA and submit manually.",
            str(shot),
        )

    # Attempt submission
    try:
        submit = page.locator(
            "button[type='submit'], input[type='submit'],"
            "button:has-text('Envoyer ma candidature'),"
            "button:has-text('Submit application'),"
            "button:has-text('Submit')"
        ).first
        await asyncio.sleep(random.uniform(3.0, 8.0))
        await submit.click(timeout=TIMEOUT)
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception as exc:
        shot = screenshot_path(job_id, "greenhouse_submit_fail")
        await page.screenshot(path=str(shot))
        return ApplyResult(
            "Needs you",
            f"Greenhouse: submit click failed. Check screenshot: {exc}",
            str(shot),
        )

    shot = screenshot_path(job_id, "greenhouse_done")
    await page.screenshot(path=str(shot))

    url_after = page.url
    content = await page.content()
    if any(kw in url_after for kw in ("confirmation", "thank", "success", "submitted")):
        return ApplyResult("Applied", "Greenhouse: submitted", str(shot), "greenhouse")
    if any(kw in content.lower() for kw in ("merci", "thank you", "candidature envoyée", "submitted")):
        return ApplyResult("Applied", "Greenhouse: submitted", str(shot), "greenhouse")
    # Detect form validation error (required field not filled)
    if any(kw in content.lower() for kw in ("champ est obligatoire", "field is required", "required field")):
        return ApplyResult(
            "Needs you",
            "Greenhouse: required field not filled. Check screenshot, fill manually.",
            str(shot),
        )
    return ApplyResult("Needs you", "Greenhouse: submitted but confirmation unclear", str(shot))
