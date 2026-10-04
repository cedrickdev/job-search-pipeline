"""Nominating a driver from a target URL — the one place the platform map lives (§3-4).

No `if "greenhouse" in url` anywhere else (§3): a caller asks the registry which driver a target
URL nominates, and gets a `BrowserApplicationDriver` or `None`. `None` is a first-class answer — an
unknown or unsupported platform (Workday, LinkedIn, an arbitrary careers page) — and the worker
turns it into an `UNSUPPORTED_CHANNEL` human hand-off, never a blind attempt (§2, §29).

Nomination is by URL pattern only; whether the page *actually is* that ATS's form is the driver's
own structural check in `prepare` (§4), so a URL that merely looks like Greenhouse but serves a
different page is caught there and routed to a human as an ambiguous form.
"""
from __future__ import annotations

from backend.app.application_engine.browser.contracts import BrowserApplicationDriver
from backend.app.application_engine.browser.drivers import default_drivers


class BrowserDriverRegistry:
    """The set of ATS drivers, nominated by target URL (§3)."""

    def __init__(self, drivers: tuple[BrowserApplicationDriver, ...] | None = None) -> None:
        self._drivers = drivers if drivers is not None else default_drivers()

    def detect(self, target_url: str | None) -> BrowserApplicationDriver | None:
        """The first driver whose URL patterns nominate the target, or `None` if none does."""
        for driver in self._drivers:
            if driver.supports(target_url):
                return driver
        return None

    @property
    def driver_keys(self) -> tuple[str, ...]:
        """The provenance keys of every registered driver, for logs and the support matrix."""
        return tuple(driver.driver_key for driver in self._drivers)
