"""Detecting the hard stops a machine must hand to a human — never solve (§16-17).

CAPTCHA, MFA/OTP/WebAuthn and a login wall are *detected* here and turned into a
`HumanRequiredReason`; they are never solved, bypassed or worked around (§16). There is no anti-bot
evasion anywhere in this package: standard Playwright, no `navigator.webdriver` spoofing, no
fingerprint evasion, no solving service, no OTP interception (§17). If a site presents one of these,
the honest outcome is a human hand-off, and the application stays exactly where it was.

Detection is structural — known CAPTCHA/MFA iframes and containers, a login form on the page —
rather than scraping arbitrary page prose (§4), so a job description that merely mentions the word
"password" does not trip a false login wall. Order matters: a CAPTCHA or MFA gate is reported before
a login wall, because those are the stricter stops.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from backend.app.domain.application_channel import HumanRequiredReason

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import Page

# CAPTCHA widgets, by the containers and iframes they inject (§16). Detection only — never solved.
_CAPTCHA_SELECTORS = (
    "iframe[src*='recaptcha']",
    "div.g-recaptcha",
    "div[class*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "div.h-captcha",
    "iframe[src*='arkoselabs']",
    "iframe[src*='funcaptcha']",
    "div[id*='arkose']",
    "div[id*='funcaptcha']",
    "iframe[title*='captcha' i]",
)

# MFA / one-time-code / WebAuthn prompts (§16). Detection only — never intercepted.
_MFA_SELECTORS = (
    "input[autocomplete='one-time-code']",
    "input[name*='otp' i]",
    "input[id*='otp' i]",
    "input[name*='mfa' i]",
    "input[name*='2fa' i]",
    "input[name*='verification_code' i]",
    "[data-testid*='mfa' i]",
    "[data-testid*='webauthn' i]",
)

# A login wall: a password field, or an explicit sign-in control (§15). We support an already
# authenticated persistent profile; an unauthenticated page is a human hand-off, never a scrape.
_LOGIN_SELECTORS = (
    "input[type='password']",
    "input[name='password']",
    "button[name='signin']",
    "button[id*='login' i]",
    "a[href*='login' i][role='button']",
)


async def _any_visible(page: Page, selectors: tuple[str, ...]) -> bool:
    """Whether any of `selectors` matches at least one element currently in the DOM."""
    for selector in selectors:
        try:
            if await page.locator(selector).count() > 0:
                return True
        except Exception:  # a malformed selector must never crash detection
            continue
    return False


async def detect_challenge(page: Page) -> HumanRequiredReason | None:
    """The hard stop this page presents, or `None` if none does (§15-17).

    Reports the stricter stops first: a CAPTCHA or MFA gate before a login wall. Returns a
    `HumanRequiredReason` the worker turns into a REQUIRES_HUMAN hand-off — the platform detects and
    stops, and never attempts to clear any of these.
    """
    if await _any_visible(page, _CAPTCHA_SELECTORS):
        return HumanRequiredReason.CAPTCHA_PRESENT
    if await _any_visible(page, _MFA_SELECTORS):
        return HumanRequiredReason.MFA_REQUIRED
    if await _any_visible(page, _LOGIN_SELECTORS):
        return HumanRequiredReason.LOGIN_REQUIRED
    return None
