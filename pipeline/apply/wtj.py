"""Welcome to the Jungle application handler.

WTJ native forms require authentication. The applier uses a storage_state
JSON loaded into the browser context so the session cookie survives across
runs. You log in once via scripts/wtj_login.py.

WTJ SPA flow:
  - External ATS jobs: "Postuler" link has target="_blank" or an external
    href → read href, navigate directly, delegate to the right ATS handler.
  - WTJ-native jobs: clicking "Postuler" opens an application MODAL on the
    same page (not a new URL). We must CLICK the button and wait for the
    modal to render, then fill the form inside it.
"""
from __future__ import annotations

import asyncio
import random
from pathlib import Path

from pipeline import paths
from pipeline.apply._common import (
    ApplyResult, cv_path_for_job, cover_letter_path_for_job,
    fill_field_by_label, fill_screening_questions, screenshot_path, upload_cv,
    detect_platform,
)

SESSION_DIR = paths.DATA_DIR / "browser_state" / "wtj"
TIMEOUT = 8000
WTJ_BASE = "https://www.welcometothejungle.com"


async def _dismiss_cookie(page) -> None:
    try:
        btn = page.locator(
            "button:has-text('Non merci'), button:has-text('OK pour moi'),"
            "button:has-text('Accepter'), button:has-text('Tout accepter')"
        ).first
        if await btn.count():
            await btn.click(timeout=3000)
            await page.wait_for_timeout(400)
    except Exception:
        pass


async def apply(page, job: dict, answers: dict) -> ApplyResult:
    p = answers["personal"]
    job_id = job["job_id"]

    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await _dismiss_cookie(page)

    # Locate the primary "Postuler" / "Apply" control
    apply_link = page.locator(
        "[data-role='job:apply'], [data-testid='job_header-button-apply'],"
        "a:has-text('Postuler'), button:has-text('Postuler'),"
        "a:has-text('Apply'), button:has-text('Apply')"
    ).first

    if not await apply_link.count():
        shot = screenshot_path(job_id, "wtj_no_apply_btn")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", "WTJ: no Postuler button found", str(shot))

    href = (await apply_link.get_attribute("href")) or ""
    target = (await apply_link.get_attribute("target")) or ""

    # ── External ATS (target="_blank" or non-WTJ absolute URL) ──────────────
    if target == "_blank" or (href.startswith("http") and "welcometothejungle" not in href):
        await page.goto(href, wait_until="domcontentloaded", timeout=30000)
        platform = detect_platform(page.url)
        if platform != "wtj":
            from pipeline.apply import _get_handler
            handler = _get_handler(platform)
            job_redirected = {**job, "url": page.url}
            return await handler.apply(page, job_redirected, answers)

    # ── WTJ-hosted form ──────────────────────────────────────────────────────
    # For WTJ-native jobs: click the button — the SPA renders the form as a
    # modal overlay on the same page. Navigating to the href directly skips
    # the SPA initialisation and lands back on the listing.
    # For WTJ /apply sub-pages: clicking also works (causes navigation).
    pre_url = page.url
    await apply_link.click(timeout=TIMEOUT)
    # Give the SPA time to render the modal or trigger navigation
    await page.wait_for_timeout(2000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await _dismiss_cookie(page)

    # Detect login wall after the click/navigation
    url_now = page.url
    if any(k in url_now for k in ("sign_in", "connexion", "login", "signin")):
        shot = screenshot_path(job_id, "wtj_login_required")
        await page.screenshot(path=str(shot))
        return ApplyResult(
            "Needs you",
            "WTJ: login required. Run scripts/wtj_login.py to refresh session.",
            str(shot),
        )
    content_now = await page.content()
    # "s'inscrire" can appear on the login page; the job listing also uses it in
    # a different context. Only treat it as a login wall if the URL also looks wrong.
    if "se connecter" in content_now.lower() and url_now == pre_url:
        # URL didn't change and page says "se connecter" → modal not opened / login gate
        shot = screenshot_path(job_id, "wtj_login_required")
        await page.screenshot(path=str(shot))
        return ApplyResult(
            "Needs you",
            "WTJ: login required. Run scripts/wtj_login.py to refresh session.",
            str(shot),
        )

    # Wait for the application form to appear (modal or new page)
    # Look for a form, an upload button, or personal-info fields
    try:
        await page.wait_for_selector(
            "form, input[type='file'], [data-testid*='upload'], "
            "input[name*='first'], input[name*='email']",
            timeout=8000,
        )
    except Exception:
        shot = screenshot_path(job_id, "wtj_form_not_found")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", "WTJ: application form did not appear after clicking Postuler", str(shot))

    # Fill personal fields
    for label, value in [
        ("Prénom", p["name"].split()[0]),
        ("^Nom$", " ".join(p["name"].split()[1:])),  # anchored to avoid matching "Prénom"
        ("Email", p["email"]),
        ("Téléphone", p["phone"]),
    ]:
        await fill_field_by_label(page, label, value)

    lang = job.get("language", "fr")
    pool = answers.get("motivation_pool", {}).get(lang)
    if pool and isinstance(pool, list):
        motivation = random.choice(pool)
    else:
        motivation = answers.get("motivations", {}).get(lang, "")
    if motivation:
        await fill_field_by_label(page, "Lettre de motivation|Message|Motivation", motivation)

    # Upload CV
    try:
        cv = cv_path_for_job(job, answers)
        await upload_cv(page, cv, timeout=TIMEOUT)
    except Exception as exc:
        shot = screenshot_path(job_id, "wtj_upload_fail")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", f"CV upload failed: {exc}", str(shot))

    await fill_screening_questions(page, answers)

    # Take a pre-submit screenshot to inspect the form state
    shot_pre = screenshot_path(job_id, "wtj_presubmit")
    await page.screenshot(path=str(shot_pre), full_page=True)

    # Submit — use form-scoped submit button to avoid matching the header "Postuler" button.
    # The WTJ application modal renders inside a portal; the form's submit button is
    # type="submit" while the header "Postuler" button is type="button".
    try:
        submit = page.locator("form button[type='submit'], form input[type='submit']").first
        if not await submit.count():
            submit = page.locator(
                "button[type='submit'], input[type='submit'],"
                "button:has-text('Envoyer'), button:has-text('Soumettre')"
            ).first
        await asyncio.sleep(random.uniform(3.0, 8.0))
        await submit.click(timeout=TIMEOUT)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
    except Exception as exc:
        shot = screenshot_path(job_id, "wtj_submit_fail")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", f"Submit failed: {exc}", str(shot))

    shot = screenshot_path(job_id, "wtj_done")
    await page.screenshot(path=str(shot))

    content = await page.content()
    if any(kw in content.lower() for kw in ("merci", "thank", "candidature", "envoyée", "submitted")):
        return ApplyResult("Applied", "WTJ: submitted", str(shot), "wtj")
    return ApplyResult("Needs you", "WTJ: submitted but confirmation unclear", str(shot))
