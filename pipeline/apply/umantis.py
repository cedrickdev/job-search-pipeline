"""Umantis (Haufe umantis) recruiting platform — used by many Swiss employers
(Migros among them), each on their own `recruitingapp-<tenant>.umantis.com`
instance with a custom application form. Field labels vary by employer/locale
(often German by default even when the surrounding site is browsed in
French), so fields are matched by bilingual label patterns rather than by
the numeric `input_NNNN` ids, which are specific to each employer's form and
won't generalize.

Applying creates an account on that employer's Umantis tenant (email +
generated password) as part of the same submission — no separate email
verification step observed, unlike jobup.ch."""
from __future__ import annotations

import re

from pipeline.apply._common import (
    ApplyResult, cv_path_for_job, cover_letter_path_for_job,
    fill_field_by_label, fill_screening_questions, screenshot_path, upload_cv,
)
from pipeline.site_credentials import get_or_create

TIMEOUT = 8000

_TENANT_RE = re.compile(r"recruitingapp-(\d+)\.umantis\.com")

FIELD_LABELS = [
    ("Vorname|Prénom|First ?name", "first_name"),
    ("Nachname|Nom de famille|^Nom$|Last ?name", "last_name"),
    ("E-Mail|E-mail|Email", "email"),
    ("Telefon|Téléphone|Phone", "phone"),
    ("LinkedIn", "linkedin_url"),
]


def _tenant_key(url: str) -> str:
    m = _TENANT_RE.search(url)
    return f"umantis-{m.group(1)}" if m else "umantis-unknown"


async def _select_radio(page, label_pattern: str, timeout: int = 3000) -> bool:
    """Umantis renders custom-styled radio buttons where a decorative <span>
    sits on top of the actual (visually hidden) input and intercepts plain
    clicks — force=True bypasses that actionability check safely since we've
    already confirmed via get_by_label that this is the right input."""
    try:
        locator = page.get_by_label(re.compile(label_pattern, re.IGNORECASE)).first
        await locator.check(timeout=timeout, force=True)
        return True
    except Exception:
        return False


async def apply(page, job: dict, answers: dict) -> ApplyResult:
    p = answers["personal"]
    job_id = job["job_id"]
    creds = get_or_create(_tenant_key(job["url"]), p["email"])

    if page.url != job["url"]:
        await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    field_values = {
        "first_name": p["name"].split()[0],
        "last_name": " ".join(p["name"].split()[1:]),
        "email": creds["email"],
        "phone": p["phone"],
        "linkedin_url": p.get("linkedin_url", ""),
    }
    filled = 0
    for label_pattern, field_key in FIELD_LABELS:
        if await fill_field_by_label(page, label_pattern, field_values[field_key]):
            filled += 1

    await _select_radio(page, "Herr|Monsieur|^M\\.$|^Mr$")
    await _select_radio(page, "Französisch|Français|French")
    # Required data-usage consent — pick the narrower option (data used only
    # for this specific application, not retained for future outreach).
    await _select_radio(
        page,
        "uniquement pour ce processus|nur für dieses Bewerbungsverfahren|"
        "only.*this application",
    )

    password_filled = await fill_field_by_label(page, "Passwort|Mot de passe|Password", creds["password"])

    uploaded_cv = False
    try:
        cv = cv_path_for_job(job, answers)
        cv_input = page.locator(
            "input[type='file'][id*='2622'], input[type='file'][name*='cv' i],"
            "input[type='file']"
        ).nth(1)  # heuristic: 2nd file input is usually the CV (1st is cover letter)
        if await cv_input.count():
            await cv_input.set_input_files(str(cv), timeout=TIMEOUT)
            uploaded_cv = True
    except Exception:
        pass

    try:
        cl = cover_letter_path_for_job(job, answers)
        if cl:
            cl_input = page.locator("input[type='file']").first
            if await cl_input.count():
                await cl_input.set_input_files(str(cl), timeout=TIMEOUT)
    except Exception:
        pass

    # Required privacy-policy consent checkbox — without it the form cannot submit.
    try:
        consent = page.get_by_label(
            re.compile(
                "Datenschutzerklärung|protection des données|"
                "politique de confidentialité|privacy policy",
                re.IGNORECASE,
            )
        ).first
        await consent.check(timeout=3000, force=True)
    except Exception:
        pass

    shot_pre = screenshot_path(job_id, "umantis_presubmit")
    await page.screenshot(path=str(shot_pre), full_page=True)

    if filled == 0 and not password_filled and not uploaded_cv:
        return ApplyResult(
            "Needs you",
            "umantis: could not fill any recognized fields — form layout may differ for this employer.",
            str(shot_pre),
        )

    # Umantis applications are multi-step ("Suivant"/"Weiter"/"Next" advances
    # a page at a time before a final "Envoyer"/"Absenden" submit). Walk
    # forward until a final-submit-looking button appears, a confirmation
    # shows up, or nothing advances anymore.
    advanced_steps = 0
    max_steps = 6
    for _ in range(max_steps):
        # A validation error on this step (e.g. the pre-existing-email notice)
        # re-renders the page and wipes the password field even though the
        # value we typed was fine — re-fill it every pass rather than once
        # up front, or the retry fails on password complexity instead of
        # the original error.
        try:
            pwd_input = page.get_by_label(
                re.compile("Passwort|Mot de passe|Password", re.IGNORECASE)
            ).first
            if await pwd_input.count() and not await pwd_input.input_value():
                await pwd_input.fill(creds["password"])
        except Exception:
            pass

        # Employers add their own custom questions beyond the fixed fields
        # above (e.g. "Have you worked for us before?"). Try the canned
        # answers.yaml matches first, then fall back to a safe generic
        # answer for any still-empty REQUIRED text field so it doesn't
        # block every subsequent step.
        await fill_screening_questions(page, answers)
        try:
            page_text = (await page.content()).lower()
            fallback = "Non" if "mot de passe" in page_text else "Nein"
            for textarea in await page.locator("textarea").all():
                if await textarea.input_value():
                    continue
                is_required = await textarea.evaluate(
                    "el => el.required || el.closest('[class*=mustfield]') != null"
                )
                if is_required:
                    await textarea.fill(fallback)
        except Exception:
            pass

        final_submit = page.locator(
            "button:has-text('Absenden'), button:has-text('Envoyer'),"
            "button:has-text('Bewerbung senden'), button:has-text('Soumettre la candidature'),"
            "button:has-text('Bewerbung abschicken')"
        ).first
        next_btn = page.locator(
            "button:has-text('Suivant'), button:has-text('Weiter'), button:has-text('Next')"
        ).first

        target = final_submit if await final_submit.count() else next_btn
        if not await target.count():
            break
        is_final = target is final_submit
        try:
            await target.click(timeout=TIMEOUT)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(800)
        except Exception as exc:
            shot = screenshot_path(job_id, "umantis_submit_fail")
            await page.screenshot(path=str(shot))
            return ApplyResult(
                "Needs you",
                f"umantis: pre-filled {filled} fields + password + CV={uploaded_cv}, "
                f"advanced {advanced_steps} step(s), but the {'submit' if is_final else 'next'} "
                f"click failed: {exc}",
                str(shot),
            )
        advanced_steps += 1
        if is_final:
            break

    shot = screenshot_path(job_id, "umantis_done")
    await page.screenshot(path=str(shot))
    content = (await page.content()).lower()

    # Single common words like "merci"/"danke" are not reliable — they show up
    # in ordinary form labels/questions too (a real false positive: a Migros
    # custom question literally contained "...si oui, merci de préciser...").
    # Require an actual multi-word confirmation phrase, AND the absence of a
    # visible required-field error (proof this step didn't actually validate).
    confirmation_phrases = (
        "vielen dank für ihre bewerbung", "ihre bewerbung wurde", "bewerbung eingegangen",
        "merci pour votre candidature", "votre candidature a été", "candidature a bien été",
        "thank you for your application", "your application has been",
    )
    error_markers = (
        "une réponse doit être apportée", "dieses feld muss ausgefüllt",
        "ce champ est obligatoire", "this field is required", "muss beantwortet werden",
    )
    has_confirmation = any(kw in content for kw in confirmation_phrases)
    has_blocking_error = any(kw in content for kw in error_markers)
    if has_confirmation and not has_blocking_error:
        return ApplyResult("Applied", "umantis: submitted", str(shot), "umantis")
    return ApplyResult(
        "Needs you",
        f"umantis: pre-filled {filled} fields + password + CV={uploaded_cv}; "
        f"advanced {advanced_steps} step(s) but no confirmation phrase detected"
        f"{' (a required-field error is blocking this step)' if has_blocking_error else ''} "
        "— verify manually.",
        str(shot),
    )
