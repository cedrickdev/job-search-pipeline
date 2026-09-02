"""
Log into jobup.ch with the credentials in data/site_credentials.json and save
the Playwright session for pipeline/apply/jobup.py to reuse.

jobup.ch requires a fresh 6-digit email code on any new browser/device
fingerprint — not on every application run, since this session is then
reused indefinitely by the applier. Re-run only if the saved session expires
or is revoked.

Usage:
    .venv/bin/python scripts/jobup_login.py            # first pass: triggers the code email
    .venv/bin/python scripts/jobup_login.py --code 123456   # second pass: completes login
"""
import argparse
import asyncio
import json

from pipeline import paths
from pipeline.apply.jobup import SESSION_DIR

CREDENTIALS_PATH = paths.DATA_DIR / "site_credentials.json"
SEARCH_URL = "https://www.jobup.ch/fr/emplois/?term=job"


def _load_credentials() -> dict:
    creds = json.loads(CREDENTIALS_PATH.read_text())
    return creds["jobup"]


async def main(code: str | None) -> None:
    from playwright.async_api import async_playwright

    creds = _load_credentials()
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(locale="fr-CH")
        page = await context.new_page()
        await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)
        try:
            accept = page.get_by_text("Accepter tout", exact=False).first
            if await accept.count():
                await accept.click(timeout=3000)
        except Exception:
            pass
        await page.wait_for_timeout(500)

        await page.get_by_text("Se connecter", exact=False).first.click(timeout=5000)
        await page.wait_for_timeout(1000)
        await page.locator("input[name='email']").fill(creds["email"])
        await page.get_by_role("button", name="Continuer").first.click(timeout=5000)
        await page.wait_for_timeout(2000)

        content = await page.content()
        if "vérification" in content.lower():
            if not code:
                print(f"jobup.ch sent a new 6-digit code to {creds['email']}.")
                print("Re-run: .venv/bin/python scripts/jobup_login.py --code 123456")
                await browser.close()
                return
            await page.locator("input[name='code']").fill(code)
            await page.get_by_role("button", name="Continuer").first.click(timeout=5000)
            await page.wait_for_timeout(2000)

        content = await page.content()
        if "mot de passe" in content.lower():
            pwd_input = page.locator("input[type='password'], input[name='password']").first
            await pwd_input.fill(creds["password"])
            await page.get_by_role("button", name="Continuer").first.click(timeout=5000)
            await page.wait_for_timeout(2000)

        await context.storage_state(path=str(SESSION_DIR / "storage_state.json"))
        print(f"Session saved to {SESSION_DIR / 'storage_state.json'}")
        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Log into jobup.ch and save the session")
    parser.add_argument("--code", help="6-digit code from the jobup.ch confirmation email")
    args = parser.parse_args()
    asyncio.run(main(args.code))
