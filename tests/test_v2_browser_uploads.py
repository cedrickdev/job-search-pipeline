"""Pinned files go to named ATS slots; Chromium never contacts an employer."""
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from backend.app.application_engine.browser.contracts import DriverContext, ResolvedUpload
from backend.app.application_engine.browser.drivers.ashby import AshbyDriver
from backend.app.application_engine.browser.drivers.greenhouse import GreenhouseDriver
from backend.app.application_engine.browser.drivers.lever import LeverDriver
from backend.app.application_engine.browser.execution import _first_blocking_field
from backend.app.domain.application_channel import HumanRequiredReason
from backend.app.domain.documents import CandidateDocumentType


@pytest_asyncio.fixture
async def upload_page():
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch()
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


def _context(tmp_path: Path) -> DriverContext:
    uploads = []
    for kind, filename in (
        (CandidateDocumentType.RESUME, "Candidate_CV.pdf"),
        (CandidateDocumentType.COVER_LETTER, "Candidate_Letter.pdf"),
    ):
        path = tmp_path / f"internal-{kind.value}.pdf"
        path.write_bytes(b"%PDF-fixture-" + kind.value.encode())
        uploads.append(ResolvedUpload(document_type=kind, path=str(path), filename=filename))
    return DriverContext(uploads=tuple(uploads))


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", [GreenhouseDriver(), LeverDriver(), AshbyDriver()])
async def test_reordered_uploads_use_slots_and_candidate_filenames(upload_page, tmp_path, driver):
    await upload_page.set_content("""
        <input type="file" id="unrelated" aria-label="Newsletter attachment">
        <form id="application_form" class="application-form ashby-application-form">
          <label for="letter">Lettre de motivation</label><input id="letter" type="file">
          <label for="cv">Curriculum vitae</label><input id="cv" type="file">
        </form>
    """)
    await driver._upload_files(upload_page, _context(tmp_path))
    attached = await upload_page.locator("input[type=file]").evaluate_all(
        "els => els.map(el => Array.from(el.files).map(file => file.name))")
    assert attached == [[], ["Candidate_Letter.pdf"], ["Candidate_CV.pdf"]]
    contents = await upload_page.locator("#cv").evaluate(
        "async el => await el.files[0].text()")
    assert contents == "%PDF-fixture-RESUME"


@pytest.mark.asyncio
async def test_unknown_required_upload_does_not_inherit_resume_authority(upload_page, tmp_path):
    await upload_page.set_content("""
        <form id="application_form">
          <label for="portfolio">Portfolio</label>
          <input id="portfolio" name="portfolio" type="file" required>
        </form>
    """)
    driver = GreenhouseDriver()
    questions = await driver._extract_questions(upload_page)
    blocked = _first_blocking_field(questions, _context(tmp_path))
    assert blocked is not None
    assert blocked[0] is HumanRequiredReason.UPLOAD_UNRESOLVED
    await driver._upload_files(upload_page, _context(tmp_path))
    assert await upload_page.locator("#portfolio").evaluate("el => el.files.length") == 0


@pytest.mark.asyncio
async def test_duplicate_resume_slots_refuse_before_attaching_any_file(upload_page, tmp_path):
    await upload_page.set_content("""
        <form id="application_form">
          <input type="file" aria-label="CV">
          <input type="file" aria-label="Resume">
        </form>
    """)
    with pytest.raises(ValueError, match="ambiguous document upload slots"):
        await GreenhouseDriver()._upload_files(upload_page, _context(tmp_path))
    assert await upload_page.locator("input[type=file]").evaluate_all(
        "els => els.every(el => el.files.length === 0)")
