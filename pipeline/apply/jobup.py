"""jobup.ch application handler.

jobup listings use the platform's own "postuler" flow (quickApply/easyApply),
which requires a logged-in jobup account. The applier loads a storage_state
JSON into the browser context so the session survives across runs — the
account is logged in once via scripts/jobup_login.py (jobup.ch requires a
fresh 6-digit email code on any new browser/device fingerprint, but not on
every run once the session is saved), mirroring the existing wtj.py pattern.
"""
from __future__ import annotations

import random

from pipeline import paths
from pipeline.apply._common import (
    ApplyResult, cv_path_for_job, cover_letter_path_for_job,
    fill_field_by_label, fill_screening_questions, screenshot_path, upload_cv,
)

SESSION_DIR = paths.DATA_DIR / "browser_state" / "jobup"
TIMEOUT = 8000


async def _dismiss_cookie(page) -> None:
    try:
        btn = page.get_by_text("Accepter tout", exact=False).first
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

    apply_btn = page.locator(
        "button:has-text('Postuler'), a:has-text('Postuler'),"
        "button:has-text('Postuler maintenant'), button:has-text('Candidature rapide')"
    ).first
    if not await apply_btn.count():
        shot = screenshot_path(job_id, "jobup_no_apply_btn")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", "jobup: no Postuler button found", str(shot))

    await apply_btn.click(timeout=TIMEOUT)
    await page.wait_for_timeout(1500)

    # jobup.ch shows a "Créez un compte pour postuler plus rapidement" modal on
    # click; "se connecter"/"mot de passe" strings are present in the page's
    # hidden auth widgets even when actually logged in, so they are NOT a
    # reliable signal here (unlike wtj.py) — don't use them to detect a login
    # wall. Dismiss the modal via the guest path instead.
    guest_continue = page.get_by_text("Continuer sans compte", exact=False).first
    if await guest_continue.count():
        await guest_continue.click(timeout=TIMEOUT)
        await page.wait_for_timeout(1500)

    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    try:
        await page.wait_for_selector(
            "form, input[type='file'], input[name*='email'], input[name*='phone']",
            timeout=8000,
        )
    except Exception:
        shot = screenshot_path(job_id, "jobup_form_not_found")
        await page.screenshot(path=str(shot))
        return ApplyResult(
            "Needs you",
            "jobup: no application form appeared after Postuler — this "
            "listing's apply flow needs manual verification (may redirect "
            "off-site or require closer investigation of jobup's SPA).",
            str(shot),
        )

    for label, value in [
        ("Prénom", p["name"].split()[0]),
        ("^Nom$", " ".join(p["name"].split()[1:])),
        ("Email|E-mail", p["email"]),
        ("Téléphone", p["phone"]),
    ]:
        await fill_field_by_label(page, label, value)

    lang = job.get("language", "fr")
    pool = answers.get("motivation_pool", {}).get(lang)
    motivation = random.choice(pool) if pool and isinstance(pool, list) else \
        answers.get("motivations", {}).get(lang, "")
    if motivation:
        await fill_field_by_label(page, "Lettre de motivation|Message|Motivation", motivation)

    try:
        cv = cv_path_for_job(job, answers)
        await upload_cv(page, cv, timeout=TIMEOUT)
    except Exception as exc:
        shot = screenshot_path(job_id, "jobup_upload_fail")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", f"CV upload failed: {exc}", str(shot))

    await fill_screening_questions(page, answers)

    shot_pre = screenshot_path(job_id, "jobup_presubmit")
    await page.screenshot(path=str(shot_pre), full_page=True)

    try:
        submit = page.locator(
            "button[type='submit'], input[type='submit'],"
            "button:has-text('Envoyer'), button:has-text('Postuler')"
        ).first
        await submit.click(timeout=TIMEOUT)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
    except Exception as exc:
        shot = screenshot_path(job_id, "jobup_submit_fail")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", f"Submit failed: {exc}", str(shot))

    shot = screenshot_path(job_id, "jobup_done")
    await page.screenshot(path=str(shot))

    content = await page.content()
    if any(kw in content.lower() for kw in ("merci", "candidature envoyée", "candidature a été envoyée")):
        return ApplyResult("Applied", "jobup: submitted", str(shot), "jobup")
    return ApplyResult("Needs you", "jobup: submitted but confirmation unclear", str(shot))
