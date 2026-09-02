"""
Open a headed Chromium window at the WTJ login page using the persistent
session profile (data/browser_state/wtj). Log in manually — the script
detects when you're logged in (URL leaves /signin) and saves the session.

Usage:
    python scripts/wtj_login.py
"""
import asyncio
import re

from pipeline.apply.wtj import SESSION_DIR

WTJ_LOGIN_URL = "https://www.welcometothejungle.com/fr/signin"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    " AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/124.0.0.0 Safari/537.36"
)
# Same values as pipeline/applier.py: the session saved here is replayed by the
# applier, so the two browsers must not disagree about who is browsing.
_LOCALE = "fr-CH"
_TIMEZONE = "Europe/Zurich"


async def main() -> None:
    from playwright.async_api import async_playwright

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Session will be saved to: {SESSION_DIR}")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(SESSION_DIR),
            headless=False,
            args=["--no-sandbox"],
            user_agent=_USER_AGENT,
            locale=_LOCALE,
            timezone_id=_TIMEZONE,
        )
        page = await context.new_page()

        # If already logged in, skip to dashboard
        await page.goto(WTJ_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        print("\nBrowser open — log in with your WTJ credentials.")
        print("Waiting for you to complete login (watching for redirect away from /signin)...")

        # Wait up to 3 minutes for the URL to be a WTJ page that isn't the signin/login page.
        # Check the actual hostname to avoid matching WTJ appearing in OAuth redirect query strings.
        from urllib.parse import urlparse

        def _is_wtj_logged_in(url: str) -> bool:
            parsed = urlparse(url)
            is_wtj_domain = parsed.netloc in (
                "www.welcometothejungle.com", "welcometothejungle.com"
            )
            is_auth_page = any(k in parsed.path for k in ("signin", "login", "signup"))
            return is_wtj_domain and not is_auth_page

        try:
            await page.wait_for_url(_is_wtj_logged_in, timeout=180_000)
        except Exception:
            pass

        current_url = page.url
        print(f"\nDetected navigation to: {current_url}")

        # Save the full storage state (cookies + localStorage)
        await context.storage_state(path=str(SESSION_DIR / "storage_state.json"))
        print("Session saved.")
        await context.close()

    print("\nDone. WTJ session is ready — the applier will reuse it automatically.")


if __name__ == "__main__":
    asyncio.run(main())
