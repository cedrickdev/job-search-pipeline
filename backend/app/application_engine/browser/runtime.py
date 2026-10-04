"""Launching the one real Chromium the worker drives — standard Playwright, no evasion (§17, §31).

A single async context manager that yields a `Page` on a persistent Chromium profile. Persistent so
an already-authenticated session survives between runs (§15: we support a logged-in profile, we
never scrape or store a password); shared-profile-serialized by the `browser_lock` the worker holds
around it, so two executions never mutate one profile at once (§31). Standard Playwright
throughout — no `navigator.webdriver` spoofing, no fingerprint evasion, no stealth plugin, no proxy
rotation (§17). If a site refuses ordinary automation, that is a human hand-off, not a cue to evade.
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from backend.app.domain.identifiers import UserId
from pipeline import paths

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import Page

# The persistent Chromium profile, under the same browser-state directory V1 uses and the
# `browser_lock` guards, so a logged-in session is reused and never fought over (§15, §31).
BROWSER_PROFILE_DIR: Path = paths.DATA_DIR / "browser_state" / "profile"
_HEADLESS_VARIABLE = "JOBSEARCH_BROWSER_HEADLESS"


def profile_dir_for(user_id: UserId, *, root: Path = BROWSER_PROFILE_DIR) -> Path:
    """Return a private, stable Chromium profile for one account."""
    directory = root / str(user_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def _headless() -> bool:
    """Headless unless explicitly disabled — a server runs headless; a human debugging may not."""
    return os.environ.get(_HEADLESS_VARIABLE, "true").strip().lower() not in {"0", "false", "no"}


@contextlib.asynccontextmanager
async def browser_page(*, profile_dir: Path | None = None,
                       headless: bool | None = None) -> AsyncIterator[Page]:
    """Yield a `Page` on a persistent Chromium profile, closing everything cleanly on exit (§31).

    Imports Playwright lazily so this module — and the package — loads in an environment without a
    browser (the API and general worker never install Chromium, §30). The persistent context is
    always closed on the way out, even on error, so a crash never leaks a browser process.
    """
    from playwright.async_api import async_playwright

    directory = profile_dir if profile_dir is not None else BROWSER_PROFILE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    resolved_headless = _headless() if headless is None else headless

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(directory), headless=resolved_headless)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            yield page
        finally:
            with contextlib.suppress(Exception):
                await context.close()
