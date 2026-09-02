"""
Open a headed Chromium window at the Indeed signup/login page. Complete the
whole flow yourself (email, email code, phone, SMS code, and any Cloudflare
captcha) — this script never attempts to solve a captcha automatically.
Session is saved on completion so pipeline/apply/indeed.py can reuse it in
headless runs.

Usage:
    python scripts/indeed_login_manual.py
"""
import asyncio
from pathlib import Path
from urllib.parse import urlparse

from pipeline import paths

SESSION_DIR = paths.DATA_DIR / "browser_state" / "indeed"
AUTH_URL = (
    "https://secure.indeed.com/auth?hl=fr_CH&co=CH"
    "&continue=https%3A%2F%2Fch-fr.indeed.com%2F&tmpl=desktop"
)
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    " AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/136.0.0.0 Safari/537.36"
)


def _is_logged_in(url: str) -> bool:
    parsed = urlparse(url)
    is_indeed = parsed.netloc.endswith("indeed.com")
    is_auth_page = any(k in parsed.path for k in
                       ("/auth", "/account/verifyphone", "/account/verify",
                        "/onboarding", "/registerconfirmation"))
    return is_indeed and not is_auth_page


async def main() -> None:
    from playwright.async_api import async_playwright

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Session will be saved to: {SESSION_DIR}")
    print("A browser window will open at Indeed's signup page.")
    print("Email is pre-filled (cedrickfeze@ik.me) — complete the rest yourself:")
    print("  1. Click Continuer to get the email code (check your inbox)")
    print("  2. Enter the email code")
    print("  3. Add + verify your phone number (SMS code) — solve any captcha shown")
    print("Waiting for you to finish (up to 10 minutes)...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="fr-CH",
        )
        page = await context.new_page()
        await page.goto(AUTH_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1000)
        try:
            await page.locator("button:has-text('Autoriser tous les cookies')").first.click(timeout=3000)
        except Exception:
            pass
        try:
            await page.locator("input[name='__email']").first.fill("cedrickfeze@ik.me")
        except Exception:
            pass

        try:
            await page.wait_for_url(_is_logged_in, timeout=600_000)
        except Exception:
            pass

        print(f"\nDetected navigation to: {page.url}")
        await context.storage_state(path=str(SESSION_DIR / "storage_state.json"))
        print("Session saved.")
        await browser.close()

    print("\nDone. Indeed session is ready — the applier will reuse it automatically.")


if __name__ == "__main__":
    asyncio.run(main())
