"""Real Playwright automation, driven against the local ATS fixtures (§35-43).

A real Chromium drives the deterministic fixture forms in `tests.v2_browser_fixtures` — never a live
job board (CLAUDE.md §Testing). Each test pins one invariant the phase rests on:

- **PREPARE reads structure** (§36): the questions and their stable keys, a fingerprint stable across
  a volatile token change but different across a structural one.
- **SUBMIT is safe** (§20-22, §41): Greenhouse/Lever/Ashby prepare→submit reaches a *confirmed*
  outcome with exactly one click; a form with no confirmation is STATE_UNKNOWN, never a retry.
- **the hard stops stop** (§10-11, §16, §40): CAPTCHA, MFA and login hand off before any click; a
  required unknown or sensitive field hands off; a changed form is refused — and in every one of
  these the fixture records zero submissions.

Local development may skip browser-dependent cases when Chromium is not installed.
The browser-test image and CI set JOBSEARCH_REQUIRE_BROWSER_TESTS=1, making a missing
browser a failure rather than a silently skipped suite.
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio

from backend.app.application_engine.browser.drivers.ashby import AshbyDriver
from backend.app.application_engine.browser.drivers.greenhouse import GreenhouseDriver
from backend.app.application_engine.browser.drivers.lever import LeverDriver
from backend.app.application_engine.browser.registry import BrowserDriverRegistry
from backend.app.application_engine.browser import runtime as browser_runtime
from backend.app.application_engine.browser.contracts import DriverContext, ResolvedUpload
from backend.app.application_engine.browser.execution import (
    _first_blocking_field,
    run_prepare,
    run_submit,
)
from backend.app.application_engine.task_dispatcher import BrowserTaskOutcome
from backend.app.domain.application_answer import (
    AnswerProvenance,
    ApplicationAnswer,
    ApplicationQuestion,
    ApplicationQuestionKind,
    QuestionSensitivity,
)
from backend.app.domain.application_channel import HumanRequiredReason
from backend.app.domain.documents import CandidateDocumentType
from backend.app.domain.identifiers import UserId
from tests.v2_browser_fixtures import serve_fixture_site
from tests.v2_builders import a_candidate_profile

pytestmark = pytest.mark.asyncio


def _missing_browser(reason: str) -> None:
    if os.environ.get("JOBSEARCH_REQUIRE_BROWSER_TESTS") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


async def test_profile_paths_are_owner_scoped_and_private(tmp_path: Path) -> None:
    """A second account must never inherit the first account's ATS cookies."""
    first_id = UserId(UUID("11111111-1111-4111-8111-111111111111"))
    second_id = UserId(UUID("22222222-2222-4222-8222-222222222222"))

    first = browser_runtime.profile_dir_for(first_id, root=tmp_path)
    second = browser_runtime.profile_dir_for(second_id, root=tmp_path)

    assert first == tmp_path / str(first_id)
    assert second == tmp_path / str(second_id)
    assert first != second
    assert first.stat().st_mode & 0o077 == 0
    assert second.stat().st_mode & 0o077 == 0


async def test_chromium_session_cookie_stays_with_its_owner(tmp_path: Path) -> None:
    """An ATS login in one persistent profile must not appear in another's."""
    from backend.app.application_engine.browser.runtime import browser_page

    first_id = UserId(UUID("11111111-1111-4111-8111-111111111111"))
    second_id = UserId(UUID("22222222-2222-4222-8222-222222222222"))
    first = browser_runtime.profile_dir_for(first_id, root=tmp_path)
    second = browser_runtime.profile_dir_for(second_id, root=tmp_path)
    try:
        async with browser_page(profile_dir=first) as page:
            await page.context.add_cookies([{
                "name": "ats_session", "value": "owner-one",
                "url": "https://ats.example.test",
                "expires": 1893456000,
            }])
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            _missing_browser("a Chromium binary is not available for the integration suite")
        raise

    async with browser_page(profile_dir=first) as page:
        owner_cookies = await page.context.cookies("https://ats.example.test")
    async with browser_page(profile_dir=second) as page:
        other_cookies = await page.context.cookies("https://ats.example.test")

    assert [(cookie["name"], cookie["value"]) for cookie in owner_cookies] == [
        ("ats_session", "owner-one")]
    assert other_cookies == []


async def test_driver_registry_only_accepts_ats_hostnames() -> None:
    registry = BrowserDriverRegistry()
    assert registry.detect("https://boards.greenhouse.io/acme/jobs/123") is not None
    assert registry.detect("https://jobs.lever.co/acme/123") is not None
    assert registry.detect("https://jobs.ashbyhq.com/acme/123") is not None
    assert registry.detect("http://127.0.0.1/jobs.lever.co/acme") is None
    assert registry.detect("https://jobs.lever.co.attacker.example/acme") is None
    assert registry.detect("https://jobs.lever.co@attacker.example/acme") is None


@pytest_asyncio.fixture
async def page() -> AsyncIterator[object]:
    """A real Chromium page, or a clean skip when no browser binary is installed."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - environment without playwright
        _missing_browser("playwright is not installed")
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(headless=True)
    except Exception:  # pragma: no cover - browser binary not installed (e.g. CI)
        await playwright.stop()
        _missing_browser("a Chromium binary is not available for the integration suite")
    page = await browser.new_page()
    try:
        yield page
    finally:
        await browser.close()
        await playwright.stop()


def _answer(key: str, value: str,
            provenance: AnswerProvenance = AnswerProvenance.HUMAN_SUPPLIED) -> ApplicationAnswer:
    return ApplicationAnswer(question_key=key, value=value, provenance=provenance)


def _identity_answers() -> tuple[ApplicationAnswer, ...]:
    """The required identity answers a reviewed application carries by submit time (§11-12)."""
    return (
        _answer("first_name", "Ada", AnswerProvenance.CANDIDATE_PROFILE),
        _answer("last_name", "Lovelace", AnswerProvenance.CANDIDATE_PROFILE),
        _answer("email", "ada@example.com"),
        _answer("full_name", "Ada Lovelace", AnswerProvenance.CANDIDATE_PROFILE),
        _answer("_first", "Ada", AnswerProvenance.CANDIDATE_PROFILE),
        _answer("_last", "Lovelace", AnswerProvenance.CANDIDATE_PROFILE),
        _answer("_email", "ada@example.com"),
    )


# --------------------------------------------------------------------- PREPARE (§36)


async def test_greenhouse_prepare_reads_questions_with_stable_keys(page) -> None:
    driver = GreenhouseDriver()
    with serve_fixture_site() as site:
        form = await driver.prepare(page, DriverContext(target_url=site.url("/greenhouse/job")))
    assert not form.blocked
    keys = {question.key for question in form.questions}
    assert {"first_name", "last_name", "email", "phone", "resume", "q_why"} <= keys
    # The hidden CSRF token is not a question — it never reaches the extracted structure (§14).
    assert "csrf_token" not in keys
    assert form.form_fingerprint is not None
    resume = next(q for q in form.questions if q.key == "resume")
    assert resume.kind is ApplicationQuestionKind.FILE_UPLOAD


async def test_fingerprint_is_stable_across_a_volatile_token(page) -> None:
    """A changed CSRF token is not a structural change — the fingerprint is identical (§36)."""
    driver = GreenhouseDriver()
    with serve_fixture_site() as site:
        a = await driver.prepare(page, DriverContext(target_url=site.url("/greenhouse/job")))
        b = await driver.prepare(
            page, DriverContext(target_url=site.url("/greenhouse/job-alt-token")))
    assert a.form_fingerprint == b.form_fingerprint


async def test_fingerprint_changes_on_a_structural_change(page) -> None:
    """Adding a required field is a structural change — the fingerprint differs (§36, §40)."""
    driver = GreenhouseDriver()
    with serve_fixture_site() as site:
        base = await driver.prepare(page, DriverContext(target_url=site.url("/greenhouse/job")))
        changed = await driver.prepare(
            page, DriverContext(target_url=site.url("/greenhouse/sensitive")))
    assert base.form_fingerprint != changed.form_fingerprint


async def test_prepare_proposes_only_profile_facts_never_contact_details(page) -> None:
    """The name comes from the profile; email/phone get no proposal (§9, §12)."""
    driver = GreenhouseDriver()
    profile = a_candidate_profile(display_name="Ada Lovelace")
    with serve_fixture_site() as site:
        result = await run_prepare(
            page, driver, DriverContext(target_url=site.url("/greenhouse/job")), profile)
    assert result.outcome is BrowserTaskOutcome.COMPLETED
    proposed = {p.question_key for p in result.answer_proposals}
    assert "first_name" in proposed and "last_name" in proposed
    assert "email" not in proposed and "phone" not in proposed  # never fabricated (§12)


async def test_sensitive_question_is_classified_not_answered(page) -> None:
    """A demographic field is read as sensitive, so the service routes it to a human (§10, §37)."""
    driver = GreenhouseDriver()
    with serve_fixture_site() as site:
        form = await driver.prepare(
            page, DriverContext(target_url=site.url("/greenhouse/sensitive")))
    gender = next(q for q in form.questions if q.key == "gender")
    assert gender.sensitivity is not QuestionSensitivity.NORMAL


# --------------------------------------------------------------------- SUBMIT success (§41)


async def test_greenhouse_submit_is_confirmed_with_exactly_one_click(page) -> None:
    driver = GreenhouseDriver()
    with serve_fixture_site() as site:
        url = site.url("/greenhouse/job")
        prepared = await driver.prepare(page, DriverContext(target_url=url))
        context = DriverContext(
            target_url=url, answers=_identity_answers(),
            expected_form_fingerprint=prepared.form_fingerprint)
        result = await run_submit(page, driver, context)
        assert result.outcome is BrowserTaskOutcome.COMPLETED
        assert result.submission_confirmed is True
        assert result.confirmation_reference == "REF12345"
        assert result.driver_key == "greenhouse/1"
        assert site.confirmations("/greenhouse/confirmation") == 1  # exactly one click (§20, §39)


async def test_lever_submit_is_confirmed(page) -> None:
    driver = LeverDriver()
    with serve_fixture_site() as site:
        url = site.url("/lever/job")
        prepared = await driver.prepare(page, DriverContext(target_url=url))
        context = DriverContext(
            target_url=url, answers=_identity_answers(),
            expected_form_fingerprint=prepared.form_fingerprint)
        result = await run_submit(page, driver, context)
    assert result.outcome is BrowserTaskOutcome.COMPLETED
    assert result.platform == "lever"


async def test_ashby_submit_is_confirmed(page) -> None:
    driver = AshbyDriver()
    with serve_fixture_site() as site:
        url = site.url("/ashby/job")
        prepared = await driver.prepare(page, DriverContext(target_url=url))
        context = DriverContext(
            target_url=url, answers=_identity_answers(),
            expected_form_fingerprint=prepared.form_fingerprint)
        result = await run_submit(page, driver, context)
    assert result.outcome is BrowserTaskOutcome.COMPLETED
    assert result.platform == "ashby"


async def test_submit_uploads_exactly_the_pinned_file(page) -> None:
    """The driver attaches only the file it was handed — the exact pinned version (§13, §38)."""
    driver = GreenhouseDriver()
    with tempfile.TemporaryDirectory() as tmp:
        pinned = Path(tmp) / "Ada_Lovelace_CV.pdf"
        pinned.write_bytes(b"%PDF-1.4 pinned-version-3")
        with serve_fixture_site() as site:
            url = site.url("/greenhouse/job")
            await driver.prepare(page, DriverContext(target_url=url))
            context = DriverContext(
                target_url=url, answers=_identity_answers(),
                uploads=(ResolvedUpload(
                    document_type=CandidateDocumentType.RESUME,
                    path=str(pinned), filename="Ada_Lovelace_CV.pdf"),))
            await driver._fill_answers(page, context)
            await driver._upload_files(page, context)
            selected = await page.locator("input[type='file']").first.evaluate(
                "el => el.files.length ? el.files[0].name : null")
    assert selected == "Ada_Lovelace_CV.pdf"


async def test_submit_without_confirmation_is_state_unknown(page) -> None:
    """A click with no positive confirmation is STATE_UNKNOWN, never FAILED or a retry (§22)."""
    driver = GreenhouseDriver()
    with serve_fixture_site() as site:
        url = site.url("/greenhouse/no-confirm")
        prepared = await driver.prepare(page, DriverContext(target_url=url))
        context = DriverContext(
            target_url=url, answers=_identity_answers(),
            expected_form_fingerprint=prepared.form_fingerprint)
        result = await run_submit(page, driver, context)
    assert result.outcome is BrowserTaskOutcome.STATE_UNKNOWN


async def test_submit_without_prepared_fingerprint_never_clicks(page) -> None:
    driver = GreenhouseDriver()
    with serve_fixture_site() as site:
        result = await run_submit(
            page, driver,
            DriverContext(target_url=site.url("/greenhouse/job"),
                          answers=_identity_answers()))
        assert result.outcome is BrowserTaskOutcome.REQUIRES_HUMAN
        assert result.human_required_reason is HumanRequiredReason.AMBIGUOUS_FORM
        assert site.confirmations("/greenhouse/confirmation") == 0


async def test_unknown_required_upload_is_unresolved() -> None:
    question = ApplicationQuestion(
        key="portfolio_file", label="Portfolio", kind=ApplicationQuestionKind.FILE_UPLOAD,
        required=True, sensitivity=QuestionSensitivity.NORMAL)
    blocked = _first_blocking_field((question,), DriverContext())
    assert blocked is not None
    assert blocked[0] is HumanRequiredReason.UPLOAD_UNRESOLVED


# --------------------------------------------------------------------- hard stops (§10-16, §40)


@pytest.mark.parametrize(("path", "reason"), [
    ("/greenhouse/captcha", HumanRequiredReason.CAPTCHA_PRESENT),
    ("/greenhouse/mfa", HumanRequiredReason.MFA_REQUIRED),
    ("/greenhouse/login", HumanRequiredReason.LOGIN_REQUIRED),
])
async def test_a_hard_stop_hands_off_before_any_click(page, path, reason) -> None:
    driver = GreenhouseDriver()
    with serve_fixture_site() as site:
        url = site.url(path)
        context = DriverContext(target_url=url, answers=_identity_answers())
        result = await run_submit(page, driver, context)
        assert result.outcome is BrowserTaskOutcome.REQUIRES_HUMAN
        assert result.human_required_reason is reason
        assert site.confirmations("/greenhouse/confirmation") == 0  # never submitted (§16, §39)


async def test_unknown_required_field_hands_off(page) -> None:
    """A required field the profile cannot answer stops for a human, never a guess (§11, §37)."""
    driver = GreenhouseDriver()
    with serve_fixture_site() as site:
        url = site.url("/greenhouse/unknown-required")
        prepared = await driver.prepare(page, DriverContext(target_url=url))
        context = DriverContext(
            target_url=url, answers=_identity_answers(),
            expected_form_fingerprint=prepared.form_fingerprint)
        result = await run_submit(page, driver, context)
        assert result.outcome is BrowserTaskOutcome.REQUIRES_HUMAN
        assert result.human_required_reason is HumanRequiredReason.UNKNOWN_REQUIRED_FIELD
        assert site.confirmations("/greenhouse/confirmation") == 0


async def test_a_changed_form_is_refused_before_filling(page) -> None:
    """The fingerprint moved since preparation → no fill, no click, human review (§14, §40)."""
    driver = GreenhouseDriver()
    with serve_fixture_site() as site:
        # Prepared against the plain form, but submitting against the form with an extra field.
        prepared = await driver.prepare(
            page, DriverContext(target_url=site.url("/greenhouse/job")))
        context = DriverContext(
            target_url=site.url("/greenhouse/sensitive"), answers=_identity_answers(),
            expected_form_fingerprint=prepared.form_fingerprint)
        result = await run_submit(page, driver, context)
        assert result.outcome is BrowserTaskOutcome.REQUIRES_HUMAN
        assert result.human_required_reason is HumanRequiredReason.FORM_CHANGED
        assert site.confirmations("/greenhouse/confirmation") == 0
