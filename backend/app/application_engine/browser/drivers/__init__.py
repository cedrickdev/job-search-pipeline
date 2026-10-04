"""The concrete ATS drivers and the default set the worker composes (§2-3, §26-28)."""
from backend.app.application_engine.browser.contracts import BrowserApplicationDriver
from backend.app.application_engine.browser.drivers.ashby import AshbyDriver
from backend.app.application_engine.browser.drivers.greenhouse import GreenhouseDriver
from backend.app.application_engine.browser.drivers.lever import LeverDriver


def default_drivers() -> tuple[BrowserApplicationDriver, ...]:
    """The drivers supported today (§2): Greenhouse, Lever, Ashby.

    Workday and LinkedIn are deliberately absent — they need dedicated drivers before they may be
    called supported, and until then an unrecognized target hands off to a human rather than being
    driven blind (§2, §29).
    """
    return (GreenhouseDriver(), LeverDriver(), AshbyDriver())


__all__ = ["AshbyDriver", "GreenhouseDriver", "LeverDriver", "default_drivers"]
