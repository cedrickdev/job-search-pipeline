"""Form filling must not cross a challenge or changed-form boundary."""
import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from backend.app.application_engine.browser.contracts import DriverContext
from backend.app.application_engine.browser.drivers.ashby import AshbyDriver
from backend.app.application_engine.browser.drivers.greenhouse import GreenhouseDriver
from backend.app.application_engine.browser.drivers.lever import LeverDriver
from backend.app.application_engine.browser.fingerprint import compute_form_fingerprint
from backend.app.domain.application_answer import AnswerProvenance, ApplicationAnswer
from backend.app.domain.application_channel import HumanRequiredReason


@pytest_asyncio.fixture
async def gate_page():
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch()
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", [GreenhouseDriver(), LeverDriver(), AshbyDriver()])
@pytest.mark.parametrize("injected,reason", [
    ("<div class='g-recaptcha'></div>", HumanRequiredReason.CAPTCHA_PRESENT),
    ("<input autocomplete='one-time-code'>", HumanRequiredReason.MFA_REQUIRED),
    ("<input type='password'>", HumanRequiredReason.LOGIN_REQUIRED),
    ("<input name='custom_referral' required>", HumanRequiredReason.FORM_CHANGED),
])
async def test_dynamic_gate_after_filling_prevents_click(gate_page, driver, injected, reason):
    await gate_page.set_content("""
        <script>window.clicks=0</script>
        <form id='application_form' class='application-form ashby-application-form'
          onsubmit="event.preventDefault(); window.clicks++; this.remove();
            document.body.insertAdjacentHTML('beforeend',
              '<p>Your application has been submitted.</p>')">
          <input name='email' type='email' aria-label='Email'>
          <button type='submit'>Submit Application</button>
        </form>
    """)
    fingerprint = compute_form_fingerprint(
        driver.platform, await driver._extract_questions(gate_page))
    await gate_page.locator("input[name=email]").evaluate(
        "(el, html) => el.addEventListener('input', () => "
        "el.form.insertAdjacentHTML('beforeend', html), {once: true})", injected)
    result = await driver.submit(gate_page, DriverContext(
        expected_form_fingerprint=fingerprint,
        answers=(ApplicationAnswer(
            question_key="email", value="candidate@example.test",
            provenance=AnswerProvenance.CANDIDATE_PROFILE,
        ),),
    ))
    assert not result.clicked
    assert result.human_required_reason is reason
    assert await gate_page.evaluate("window.clicks") == 0
