"""
Log into LinkedIn with LINKEDIN_EMAIL/LINKEDIN_PASSWORD (.env) using the same
persistent browser profile pipeline/applier.py reuses for LinkedIn
(data/browser_state/linkedin) — once logged in here, the applier's headless
runs stay authenticated.

LinkedIn may challenge a login from a new device/location with an emailed or
texted verification code. This script cannot fetch that automatically (it
would require IMAP access to the LinkedIn account's own mailbox, not the
Bluewin one pipeline/email_inbox.py is wired to) — pass it with --code if
prompted.

Usage:
    .venv/bin/python scripts/linkedin_login.py
    .venv/bin/python scripts/linkedin_login.py --code 123456
"""
import argparse
import asyncio
import os
import re

from dotenv import load_dotenv

from pipeline.apply.linkedin import SESSION_DIR

load_dotenv()

LOGIN_URL = "https://www.linkedin.com/login"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    " AppleWebKit/537.36 (KHTML, like Gecko)"
    " Chrome/136.0.0.0 Safari/537.36"
)


async def main(code: str | None) -> None:
    from playwright.async_api import async_playwright

    email = os.environ.get("LINKEDIN_EMAIL")
    password = os.environ.get("LINKEDIN_PASSWORD")
    if not (email and password):
        print("LINKEDIN_EMAIL / LINKEDIN_PASSWORD not set in .env")
        return

    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(SESSION_DIR),
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            user_agent=_USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="fr-FR",
        )
        page = await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1000)

        if "feed" in page.url or "checkpoint" not in page.url and "login" not in page.url:
            # Already logged in from a prior run.
            print("Already logged in:", page.url)
            await context.close()
            return

        # LinkedIn's login form has no stable id/name (React-generated ids);
        # the DOM even duplicates hidden variants of the same fields, so
        # scope to the first *visible* input of each type.
        await page.locator("input[type='email']:visible").first.fill(email)
        await page.locator("input[type='password']:visible").first.fill(password)
        # LinkedIn's FR button text uses a curly apostrophe (U+2019, "S'identifier")
        # that won't match a straight-quote has-text() pattern — match on the
        # unambiguous substring "identifier" instead (also catches "Sign in").
        submit = page.locator("button:visible").filter(
            has_text=re.compile("identifier|sign in", re.IGNORECASE)
        ).first
        await submit.click(timeout=8000)
        await page.wait_for_timeout(2500)

        if "checkpoint" in page.url or "challenge" in page.url:
            content = await page.content()
            if code and await page.locator("input[name='pin']").count():
                await page.locator("input[name='pin']").first.fill(code)
                await page.locator("button[type='submit']").first.click(timeout=8000)
                await page.wait_for_timeout(2500)
            else:
                shot = SESSION_DIR / "_checkpoint.png"
                await page.screenshot(path=str(shot))
                print(f"LinkedIn is asking for a verification step (see {shot}).")
                print("If it's an emailed/texted code, re-run with --code 123456.")
                await context.close()
                return

        print("Final URL:", page.url)
        if "feed" in page.url or "linkedin.com/in/" in page.url:
            print("Logged in. Session saved to", SESSION_DIR)
        else:
            shot = SESSION_DIR / "_unknown_state.png"
            await page.screenshot(path=str(shot))
            print(f"Unexpected state after login — screenshot saved to {shot}")

        await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Log into LinkedIn and save the session")
    parser.add_argument("--code", help="Verification code, if LinkedIn challenges the login")
    args = parser.parse_args()
    asyncio.run(main(args.code))
