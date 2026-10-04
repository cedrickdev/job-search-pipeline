"""The Ashby driver (jobs.ashbyhq.com) — §28.

Ashby is a single-page app whose posting shows an "Overview"/"Application" split; the form appears
once the Application tab is active, so this driver overrides navigation to click into it before
inspecting — the V1 knowledge from `pipeline/apply/ashby.py`. Because it is a SPA, `networkidle` may
never fire, so navigation waits on the application form appearing rather than the network settling.
Its selectors are its own (§27); an unsupported custom widget fails closed as an unknown field and
becomes a human hand-off rather than a guessed answer (§28).
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


class AshbyDriver(StructuredFormDriver):
    DRIVER_KEY = "ashby/1"
    PLATFORM = "ashby"
    ALLOWED_HOSTS = ("jobs.ashbyhq.com", "ashbyhq.com")
    URL_PATTERNS = (r"jobs\.ashbyhq\.com", r"ashbyhq\.com/[^/]+/[0-9a-f-]{8,}")
    FORM_SELECTOR = "form.ashby-application-form, form[class*='application'], form"
    RECOGNITION_SELECTORS = (
        "form.ashby-application-form",
        "form[class*='application']",
        "div[class*='ashby-application']",
        "div.ashby-job-posting-right-pane",
    )
    SUBMIT_SELECTORS = (
        "button:has-text('Submit Application')",
        "button:has-text('Submit application')",
        "button:has-text('Submit')",
        "button:has-text('Envoyer')",
        "button[type='submit']",
    )
    CONFIRM_URL_KEYWORDS = ("confirmation", "thank", "success", "submitted")
    CONFIRM_TEXT_KEYWORDS = (
        "thank you", "merci", "submitted", "received", "application received",
        "your application has been submitted")
    CONFIRM_SELECTORS = ("div[class*='confirmation']", "div[class*='Confirmation']")

    async def _navigate(self, page: Page, url: str) -> None:
        """Open the posting, then click into the Application tab so the form mounts (SPA)."""
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        try:
            tab = page.locator(
                "a:has-text('Application'), button:has-text('Application')").first
            if await tab.count() > 0:
                await tab.click(timeout=ACTION_TIMEOUT_MS)
        except Exception:  # the form may already be inline; recognition decides
            pass
        try:
            await page.wait_for_selector(
                "form, .ashby-application-form", timeout=ACTION_TIMEOUT_MS)
        except Exception:  # recognition in prepare handles a missing form
            pass
