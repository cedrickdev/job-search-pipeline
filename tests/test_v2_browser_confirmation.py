"""Only a visible application receipt can confirm a fixture submission."""
import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from backend.app.application_engine.browser.contracts import DriverContext
from backend.app.application_engine.browser.drivers import base
from backend.app.application_engine.browser.drivers.ashby import AshbyDriver
from backend.app.application_engine.browser.drivers.greenhouse import GreenhouseDriver
from backend.app.application_engine.browser.drivers.lever import LeverDriver
from tests.v2_browser_fixtures import serve_fixture_site


@pytest_asyncio.fixture
async def receipt_page(monkeypatch):
    monkeypatch.setattr(base, "CONFIRM_TIMEOUT_MS", 400)
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch()
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", [GreenhouseDriver(), LeverDriver(), AshbyDriver()])
@pytest.mark.parametrize("receipt", [
    "<p>Applications received after the deadline cannot be considered.</p>",
    "<div id='application_confirmation' class='application-confirmation' hidden>"
    "Your application was received.</div>",
    "<div id='application_confirmation' class='application-confirmation'></div>",
    "<p>Your application was not received.</p>",
])
async def test_generic_success_words_and_stale_markup_do_not_confirm(receipt_page, driver, receipt):
    html = """<script>window.clicks=0</script>
        <form id='application_form' class='application-form ashby-application-form'
          onsubmit='event.preventDefault(); window.clicks++'>
          <button type='submit'>Submit Application</button>
        </form>""" + receipt
    with serve_fixture_site({"/success-manager": html}) as site:
        await receipt_page.goto(site.url("/success-manager"))
        result = await driver.submit(receipt_page, DriverContext())
    assert result.clicked
    assert not result.confirmed
    assert await receipt_page.evaluate("window.clicks") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", [GreenhouseDriver(), LeverDriver(), AshbyDriver()])
async def test_delayed_application_receipt_confirms_one_click(receipt_page, driver):
    await receipt_page.set_content("""
        <script>window.clicks=0</script>
        <form id='application_form' class='application-form ashby-application-form'
          onsubmit="event.preventDefault(); window.clicks++; setTimeout(() => {
            this.remove(); document.querySelector('#receipt').textContent =
            'Your application has been submitted.';
          }, 100)">
          <button type='submit'>Submit Application</button>
        </form><p id='receipt'></p>
    """)
    result = await driver.submit(receipt_page, DriverContext())
    assert result.clicked and result.confirmed
    assert await receipt_page.evaluate("window.clicks") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", [GreenhouseDriver(), LeverDriver(), AshbyDriver()])
async def test_existing_visible_receipt_prevents_another_click(receipt_page, driver):
    await receipt_page.set_content("""
        <script>window.clicks=0</script>
        <form id='application_form' class='application-form ashby-application-form'
          onsubmit='event.preventDefault(); window.clicks++'>
          <button type='submit'>Submit Application</button>
        </form>
        <div id='application_confirmation' class='application-confirmation'>
          Your application was received.
        </div>
    """)
    result = await driver.submit(receipt_page, DriverContext())
    assert not result.clicked
    assert result.human_required_reason is not None
    assert await receipt_page.evaluate("window.clicks") == 0
