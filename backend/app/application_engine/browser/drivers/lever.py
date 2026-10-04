"""The Lever driver (jobs.lever.co) — §27.

Lever shows the job description on the posting URL and hosts the form at `{url}/apply`, so this
driver overrides navigation to land on the application page — the one piece of V1 knowledge from
`pipeline/apply/lever.py` that matters most. Its selectors are its own and are never shared with
Greenhouse (§27): Lever's submit control reads "Submit application", and its form lives in a
`.application-form` / `.section-wrapper`. An EEO/demographic follow-up Lever sometimes appends is
*not* auto-answered here — those fields are demographic and route to a human (§10); the platform
never fills "prefer not to respond" on the candidate's behalf.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from backend.app.application_engine.browser.drivers.base import (
    ACTION_TIMEOUT_MS,
    NAV_TIMEOUT_MS,
    StructuredFormDriver,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import Page


class LeverDriver(StructuredFormDriver):
    DRIVER_KEY = "lever/1"
    PLATFORM = "lever"
    ALLOWED_HOSTS = ("jobs.lever.co",)
    URL_PATTERNS = (r"jobs\.lever\.co",)
    FORM_SELECTOR = "form.application-form, .application-form, .section-wrapper, form"
    RECOGNITION_SELECTORS = (
        "form.application-form",
        ".application-form",
        "form[action*='lever']",
        ".application-question",
    )
    SUBMIT_SELECTORS = (
        "button:has-text('Submit application')",
        "button:has-text('Submit Application')",
        "button:has-text('Envoyer')",
        "input[type='submit']",
        "button[type='submit']",
    )
    CONFIRM_URL_KEYWORDS = ("confirmation", "thank", "success", "thanks")
    CONFIRM_TEXT_KEYWORDS = (
        "thank you", "thanks", "merci", "submitted", "application received",
        "received", "we'll be in touch", "you're all set")
    CONFIRM_SELECTORS = (".application-confirmation", "[data-qa='confirmation']")

    async def _navigate(self, page: Page, url: str) -> None:
        """Lever's form lives at `{url}/apply`; land there rather than on the description."""
        base = url.rstrip("/")
        apply_url = base if base.endswith("/apply") else f"{base}/apply"
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=ACTION_TIMEOUT_MS)
        except Exception:  # the form is already present; idle is best-effort
            pass
