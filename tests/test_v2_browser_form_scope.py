"""Application data must not enter another form on the same ATS page."""
import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from backend.app.application_engine.browser.contracts import DriverContext
from backend.app.application_engine.browser.drivers.ashby import AshbyDriver
from backend.app.application_engine.browser.drivers.greenhouse import GreenhouseDriver
from backend.app.application_engine.browser.drivers.lever import LeverDriver
from backend.app.domain.application_answer import AnswerProvenance, ApplicationAnswer
from backend.app.domain.application_channel import HumanRequiredReason


@pytest_asyncio.fixture
async def scoped_page():
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch()
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", [GreenhouseDriver(), LeverDriver(), AshbyDriver()])
async def test_only_application_form_is_read_filled_and_submitted(scoped_page, driver):
    await scoped_page.set_content("""
        <script>window.applicationClicks=0; window.newsletterClicks=0</script>
        <form id='newsletter' onsubmit='event.preventDefault(); window.newsletterClicks++'>
          <input name='email' aria-label='Newsletter email'>
          <input name='subscribe' aria-label='Newsletter consent'>
          <button type='submit'>Subscribe</button>
        </form>
        <form id='application_form' class='application-form ashby-application-form'
          onsubmit="event.preventDefault(); window.applicationClicks++;
            document.querySelector('#application_confirmation').hidden=false">
          <input name='email' aria-label='Email'>
          <button type='submit'>Submit Application</button>
        </form>
        <div id='application_confirmation' class='application-confirmation' hidden>
          Your application was received.
        </div>
    """)
    questions = await driver._extract_questions(scoped_page)
    assert [question.key for question in questions] == ["email"]
    result = await driver.submit(scoped_page, DriverContext(answers=(ApplicationAnswer(
        question_key="email", value="candidate@example.test",
        provenance=AnswerProvenance.CANDIDATE_PROFILE,
    ),)))
    assert result.clicked and result.confirmed
    assert await scoped_page.locator("#newsletter input[name=email]").input_value() == ""
    assert await scoped_page.locator("#application_form input").input_value() == "candidate@example.test"
    assert await scoped_page.evaluate("[window.applicationClicks, window.newsletterClicks]") == [1, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", [GreenhouseDriver(), LeverDriver(), AshbyDriver()])
async def test_multiple_application_roots_require_review(scoped_page, driver):
    await scoped_page.set_content("""
        <form id='application_form' class='application-form ashby-application-form'>
          <input name='email' aria-label='Email'>
        </form>
        <form id='application_form' class='application-form ashby-application-form'>
          <input name='email' aria-label='Email'>
        </form>
    """)
    form = await driver.prepare(scoped_page, DriverContext())
    assert form.blocked
    assert form.human_required_reason is HumanRequiredReason.AMBIGUOUS_FORM
