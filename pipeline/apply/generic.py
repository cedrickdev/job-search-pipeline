"""Best-effort generic application handler for unrecognised platforms."""
from __future__ import annotations

from pipeline.apply._common import (
    ApplyResult, cv_path_for_job, fill_field_by_label,
    fill_screening_questions, screenshot_path,
)

TIMEOUT = 5000

# Common field label patterns to try filling
FIELD_MAP = [
    ("First Name|Prénom|first_name", "first_name"),
    ("Last Name|Nom|last_name", "last_name"),
    ("Full Name|Nom complet|full_name|name", "name"),
    ("Email|Courriel", "email"),
    ("Phone|Téléphone|Mobile", "phone"),
    ("LinkedIn", "linkedin_url"),
    ("Location|Ville|Localisation", "location"),
]


async def _dismiss_cookie_banner(page) -> None:
    try:
        btn = page.locator(
            "button:has-text('Autoriser tous les cookies'),"
            "button:has-text('Tout accepter'), button:has-text('Accepter'),"
            "button:has-text('Accept all'), button:has-text('Accept')"
        ).first
        if await btn.count():
            await btn.click(timeout=3000)
            await page.wait_for_timeout(400)
    except Exception:
        pass


async def apply(page, job: dict, answers: dict) -> ApplyResult:
    p = answers["personal"]
    job_id = job["job_id"]
    field_values = {
        "first_name": p["name"].split()[0],
        "last_name": " ".join(p["name"].split()[1:]),
        "name": p["name"],
        "email": p["email"],
        "phone": p["phone"],
        "linkedin_url": p.get("linkedin_url", ""),
        "location": p["location"],
    }

    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(800)
    await _dismiss_cookie_banner(page)

    filled = 0
    for label_pattern, field_key in FIELD_MAP:
        if await fill_field_by_label(page, label_pattern, field_values[field_key]):
            filled += 1

    # Try CV upload
    uploaded = False
    try:
        file_input = page.locator("input[type='file']").first
        if await file_input.count():
            cv = cv_path_for_job(job, answers)
            await file_input.set_input_files(str(cv), timeout=TIMEOUT)
            uploaded = True
    except Exception:
        pass

    await fill_screening_questions(page, answers)

    shot = screenshot_path(job_id, "generic_prefilled")
    await page.screenshot(path=str(shot))

    if filled == 0 and not uploaded:
        return ApplyResult(
            "Needs you",
            "Generic: could not fill any fields. Finish manually.",
            str(shot),
        )

    return ApplyResult(
        "Needs you",
        f"Generic: pre-filled {filled} fields, CV {'uploaded' if uploaded else 'not found'}."
        " Review and submit manually.",
        str(shot),
    )
