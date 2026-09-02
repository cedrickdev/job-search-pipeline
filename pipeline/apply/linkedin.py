"""LinkedIn Easy Apply handler.

LinkedIn requires authentication. The applier runs headed (visible browser) so
you can log in on the first run; the session cookie persists across runs via
the user-data-dir stored at data/browser_state/linkedin.
"""
from __future__ import annotations

import asyncio
import random
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from pipeline import paths
from pipeline.apply._common import (
    ApplyResult, cv_path_for_job, detect_platform, fill_field_by_label,
    fill_screening_questions, screenshot_path,
)

SESSION_DIR = paths.DATA_DIR / "browser_state" / "linkedin"
TIMEOUT = 8000


async def _dismiss_overlays(page) -> None:
    """Cookie banner, and LinkedIn's "log in to see mutual connections" nag
    modal — shown even to logged-in sessions, unrelated to auth state, but
    both intercept clicks on the Apply button underneath if left open."""
    for pattern in (
        "button:has-text('Accepter'), button:has-text('Akzeptieren'),"
        "button:has-text('Accept')",
        "button[aria-label='Dismiss'], button[aria-label='Ignorer'],"
        "button[aria-label='Verwerfen']",
    ):
        try:
            btn = page.locator(pattern).first
            if await btn.count():
                await btn.click(timeout=2000)
                await page.wait_for_timeout(300)
        except Exception:
            pass


async def apply(page, job: dict, answers: dict) -> ApplyResult:
    job_id = job["job_id"]
    p = answers["personal"]

    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1000)
    await _dismiss_overlays(page)

    # Check if not logged in (redirect or authwall in URL, or sign-in prompt in page)
    if "login" in page.url or "authwall" in page.url:
        shot = screenshot_path(job_id, "linkedin_auth_required")
        await page.screenshot(path=str(shot))
        return ApplyResult(
            "Needs you",
            "LinkedIn: not logged in. Open the browser and log in manually, then retry.",
            str(shot),
        )
    # Also catch the case where LinkedIn shows the job but with a "Sign in" CTA
    try:
        sign_in_btn = page.locator("a:has-text('Sign in'), button:has-text('Sign in')").first
        if await sign_in_btn.count():
            shot = screenshot_path(job_id, "linkedin_auth_required")
            await page.screenshot(path=str(shot))
            return ApplyResult(
                "Needs you",
                "LinkedIn: not logged in. Log in once in the headed browser, then retry.",
                str(shot),
            )
    except Exception:
        pass

    # The mutual-connections nag can appear a beat after initial load — dismiss
    # again right before interacting with the apply button underneath it.
    await page.wait_for_timeout(1500)
    await _dismiss_overlays(page)

    # LinkedIn uses the SAME "jobs-apply-button" class for both native "Easy
    # Apply" (opens an in-page modal) and external "Apply"/"Bewerben" (opens
    # the employer's own site in a new tab) — the only way to tell them apart
    # is what actually happens after the click, not the button itself.
    # The external "Postuler/Bewerben/Apply" control is sometimes an <a>
    # (external links are semantically anchors), not a <button> — match both.
    apply_btn = page.locator(
        "button:has-text('Easy Apply'), a:has-text('Easy Apply'),"
        "button:has-text('Postuler'), a:has-text('Postuler'),"
        "button:has-text('Bewerben'), a:has-text('Bewerben'),"
        "button:has-text('Apply'), a:has-text('Apply'),"
        "button.jobs-apply-button, a.jobs-apply-button"
    ).first
    try:
        await apply_btn.wait_for(state="visible", timeout=8000)
    except Exception:
        shot = screenshot_path(job_id, "linkedin_no_apply_btn")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", "LinkedIn: no Apply/Easy Apply button found", str(shot))

    pre_url = page.url
    try:
        async with page.context.expect_page(timeout=4000) as popup_info:
            await apply_btn.click(timeout=TIMEOUT)
        external_page = await popup_info.value
        await external_page.wait_for_load_state("domcontentloaded", timeout=15000)
        platform = detect_platform(external_page.url)
        if platform == "linkedin":
            # Rare: popup is still linkedin.com (e.g. an interstitial) — treat as Easy Apply.
            page = external_page
        else:
            from pipeline.apply import _get_handler
            handler = _get_handler(platform)
            job_redirected = {**job, "url": external_page.url}
            return await handler.apply(external_page, job_redirected, answers)
    except PlaywrightTimeoutError:
        pass  # no new tab/window opened — check whether THIS tab navigated instead
    except Exception as exc:
        shot = screenshot_path(job_id, "linkedin_apply_click_failed")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", f"LinkedIn: apply click failed: {exc}", str(shot))

    if page.url != pre_url:
        platform = detect_platform(page.url)
        if platform != "linkedin":
            from pipeline.apply import _get_handler
            handler = _get_handler(platform)
            job_redirected = {**job, "url": page.url}
            return await handler.apply(page, job_redirected, answers)

    try:
        await page.wait_for_selector(".jobs-easy-apply-modal", timeout=10000)
    except Exception as exc:
        shot = screenshot_path(job_id, "linkedin_no_easy_apply")
        await page.screenshot(path=str(shot))
        return ApplyResult(
            "Needs you",
            f"LinkedIn: apply button clicked but no Easy Apply modal and no new "
            f"tab appeared (may redirect in the same tab): {exc}",
            str(shot),
        )

    # Multi-step Easy Apply form — iterate through pages
    max_steps = 10
    for step in range(max_steps):
        # Fill standard fields on this step
        for label, value in [
            ("Email address", p["email"]),
            ("Phone country code", "Switzerland (+41)"),
            ("Mobile phone number", p["phone"].replace("+41 ", "").replace(" ", "")),
        ]:
            await fill_field_by_label(page, label, value)

        await fill_screening_questions(page, answers)

        # Upload CV if file input visible
        try:
            file_input = page.locator(
                ".jobs-document-upload-redesign-card__upload-button input[type='file'],"
                "input[type='file'][id*='resume']"
            ).first
            if await file_input.count():
                cv = cv_path_for_job(job, answers)
                await file_input.set_input_files(str(cv), timeout=TIMEOUT)
        except Exception:
            pass

        # Check for Next / Review / Submit buttons
        next_btn = page.locator("button[aria-label='Continue to next step']").first
        review_btn = page.locator("button[aria-label='Review your application']").first
        submit_btn = page.locator("button[aria-label='Submit application']").first

        if await submit_btn.count():
            try:
                await asyncio.sleep(random.uniform(2.0, 6.0))
                await submit_btn.click(timeout=TIMEOUT)
                await page.wait_for_load_state("networkidle", timeout=10000)
                shot = screenshot_path(job_id, "linkedin_done")
                await page.screenshot(path=str(shot))
                return ApplyResult("Applied", "LinkedIn Easy Apply: submitted", str(shot), "linkedin")
            except Exception as exc:
                shot = screenshot_path(job_id, "linkedin_submit_fail")
                await page.screenshot(path=str(shot))
                return ApplyResult("Needs you", f"LinkedIn submit failed: {exc}", str(shot))

        elif await review_btn.count():
            await review_btn.click(timeout=TIMEOUT)
            await asyncio.sleep(random.uniform(2.0, 6.0))
            await page.wait_for_load_state("domcontentloaded", timeout=5000)

        elif await next_btn.count():
            await next_btn.click(timeout=TIMEOUT)
            await asyncio.sleep(random.uniform(2.0, 6.0))
            await page.wait_for_load_state("domcontentloaded", timeout=5000)

        else:
            # Unknown form state
            shot = screenshot_path(job_id, f"linkedin_step{step}_stuck")
            await page.screenshot(path=str(shot))
            return ApplyResult("Needs you", f"LinkedIn: stuck at step {step}", str(shot))

    shot = screenshot_path(job_id, "linkedin_maxsteps")
    await page.screenshot(path=str(shot))
    return ApplyResult("Needs you", "LinkedIn: exceeded max form steps", str(shot))
