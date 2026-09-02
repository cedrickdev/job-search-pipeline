"""
Open a headed Chromium window at the LinkedIn login page using the persistent
session profile (data/browser_state/linkedin) — the same one pipeline/apply/
linkedin.py reuses. Log in manually — the script detects when you're logged
in (URL leaves /login) and the session is saved automatically (LinkedIn uses
a full persistent profile directory, not a separate storage_state.json).

Usage:
    python scripts/linkedin_login_manual.py
"""
import asyncio

from pipeline.apply.linkedin import SESSION_DIR

LOGIN_URL = "https://www.linkedin.com/login"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    " AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/136.0.0.0 Safari/537.36"
)


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
            viewport={"width": 1440, "height": 900},
            locale="fr-FR",
        )
        page = await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        print("\nBrowser open — log in with your LinkedIn credentials.")
        print("Waiting for you to complete login (watching for redirect away from /login)...")

        from urllib.parse import urlparse

        def _is_linkedin_logged_in(url: str) -> bool:
            parsed = urlparse(url)
            is_linkedin_domain = parsed.netloc.endswith("linkedin.com")
            is_auth_page = any(k in parsed.path for k in ("login", "checkpoint", "challenge", "authwall"))
            return is_linkedin_domain and not is_auth_page

        try:
            await page.wait_for_url(_is_linkedin_logged_in, timeout=300_000)
        except Exception:
            pass

        print(f"\nDetected navigation to: {page.url}")
        await context.close()

    print("\nDone. LinkedIn session is ready — the applier will reuse it automatically.")


if __name__ == "__main__":
    asyncio.run(main())
