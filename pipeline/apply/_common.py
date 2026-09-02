"""Shared types, helpers, and platform detection for the Playwright applier."""
from __future__ import annotations

import random
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from pipeline import paths

if TYPE_CHECKING:
    from playwright.async_api import Page

ANSWERS_PATH = paths.DATA_DIR / "answers.yaml"
SCREENSHOTS_DIR = paths.DATA_DIR / "screenshots"
STAGING_DIR = paths.DATA_DIR / "upload_staging"

PLATFORM_PATTERNS: list[tuple[str, str]] = [
    (r"linkedin\.com", "linkedin"),
    (r"welcometothejungle\.com", "wtj"),
    (r"job-boards\.greenhouse\.io|boards\.greenhouse\.io|grnh\.se", "greenhouse"),
    (r"jobs\.lever\.co", "lever"),
    (r"jobs\.ashbyhq\.com", "ashby"),
    (r"jobup\.ch", "jobup"),
    (r"\.umantis\.com", "umantis"),
    (r"jobs\.migros\.ch", "migros"),
]


@dataclass
class ApplyResult:
    status: str          # "Applied" | "Needs you" | "Failed"
    detail: str
    screenshot_path: str | None = None
    channel: str = "applier"


def detect_platform(url: str) -> str:
    for pattern, name in PLATFORM_PATTERNS:
        if re.search(pattern, url or "", re.IGNORECASE):
            return name
    return "generic"


def load_answers(path: Path | str | None = None) -> dict:
    p = Path(path) if path is not None else ANSWERS_PATH
    return yaml.safe_load(p.read_text())


def _candidate_name(answers: dict) -> str:
    """Return 'First_Last' from answers, safe for filenames."""
    name = answers.get("personal", {}).get("name", "Candidate")
    return "_".join(name.split())


def _stage(src: Path, filename: str) -> Path:
    """Copy *src* to STAGING_DIR/<filename> and return the new path."""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    dst = STAGING_DIR / filename
    shutil.copy2(src, dst)
    return dst


def cv_path_for_job(job: dict, answers: dict) -> Path:
    lang = job.get("language") or "fr"
    lang = lang if lang in ("en", "fr") else "fr"
    cv = job.get("cv_pdf", {}).get(lang) or job.get("cv_pdf", {}).get("en")
    if not cv:
        raise FileNotFoundError(f"No CV PDF for job {job.get('job_id')}")
    src = Path(cv)
    display = f"{_candidate_name(answers)}_CV.pdf"
    return _stage(src, display)


def _render_letter_pdf(text: str, out_path: Path, *, personal: dict | None = None,
                       company: str | None = None, lang: str = "fr") -> None:
    """Plain-paragraph markdown (the only kind pipeline.digest/tailor_io
    writes for cover letters — blank-line-separated paragraphs, no tables or
    headings) to a simple PDF. ATS uploaders validate file content against
    the declared extension, so a renamed .md masquerading as .pdf gets
    rejected (or worse, silently delivered unreadable) — this must be a
    real PDF.

    Adds a CV-matching letterhead (name, contact line, date, optional
    recipient) rather than shipping bare body text — a cover letter with no
    header reads as an unfinished draft."""
    import html as _html
    from datetime import date

    from weasyprint import HTML

    esc = _html.escape
    paragraphs = "".join(
        f"<p>{esc(para).replace(chr(10), '<br>')}</p>"
        for para in text.strip().split("\n\n") if para.strip()
    )
    header = ""
    if personal:
        contact_bits = [b for b in (personal.get("phone"), personal.get("email"),
                                    personal.get("location")) if b]
        header += f"<h1>{esc(personal.get('name', ''))}</h1>"
        header += f"<p class='contact-line'>{esc(' · '.join(contact_bits))}</p>"
    today = date.today().strftime("%d.%m.%Y" if lang == "fr" else "%m/%d/%Y")
    header += f"<p class='date-line'>{esc(today)}</p>"
    if company:
        recipient = "À l'attention de" if lang == "fr" else "Attn:"
        header += f"<p class='recipient-line'>{recipient} {esc(company)}</p>"

    doc_html = (
        "<html><head><meta charset='utf-8'><style>"
        "body{font-family:Helvetica,Arial,sans-serif;font-size:11pt;"
        "color:#1a1a1a;line-height:1.5;margin:24mm 20mm;}"
        "h1{font-size:16pt;margin:0;color:#2e7d5b;letter-spacing:0.3px;}"
        ".contact-line{font-size:9.5pt;color:#555;margin:1mm 0 6mm;}"
        ".date-line{font-size:10pt;color:#555;margin:0 0 1mm;}"
        ".recipient-line{font-size:10pt;color:#555;margin:0 0 8mm;}"
        "p{margin:0 0 3mm;}"
        "</style></head><body>" + header + paragraphs + "</body></html>"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=doc_html).write_pdf(str(out_path))


def cover_letter_path_for_job(job: dict, answers: dict | None = None) -> Path | None:
    lang = job.get("language") or "fr"
    lang = lang if lang in ("en", "fr") else "fr"
    cl = job.get("cover_letter", {}).get(lang) or job.get("cover_letter", {}).get("en")
    if cl and Path(cl).exists():
        src = Path(cl)
        personal = answers.get("personal") if answers else None
        name = _candidate_name(answers) if answers else "Candidate"
        filename = f"{name}_Lettre_de_Motivation.pdf" if lang == "fr" else f"{name}_Cover_Letter.pdf"
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        out_path = STAGING_DIR / filename
        _render_letter_pdf(src.read_text(encoding="utf-8"), out_path, personal=personal,
                           company=job.get("company"), lang=lang)
        return out_path
    return None


def screenshot_path(job_id: int, step: str) -> Path:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    return SCREENSHOTS_DIR / f"{job_id}_{step}.png"


async def upload_cv(page: "Page", cv_path: Path, timeout: int = 10000) -> None:
    """Upload CV, handling both direct file inputs and custom upload buttons."""
    # Fast path: visible file input already in DOM
    try:
        file_input = page.locator("input[type='file']").first
        if await file_input.count():
            await file_input.set_input_files(str(cv_path), timeout=3000)
            return
    except Exception:
        pass

    # Slow path: custom upload button opens a file chooser dialog
    upload_trigger = page.locator(
        "a:has-text('ATTACH'), a:has-text('Attach'), a:has-text('Upload'),"
        "button:has-text('Upload'), button:has-text('Attach'),"
        "button:has-text('Joindre'), button:has-text('Importer'),"
        "button:has-text('Déposer'), button:has-text('Parcourir'),"
        "button:has-text('Choisir'), button:has-text('Sélectionner'),"
        "button:has-text('Resume'), button:has-text('CV'),"
        "label[for*='resume'], label[for*='cv'], label[for*='file']"
    ).first
    async with page.expect_file_chooser(timeout=timeout) as fc_info:
        await upload_trigger.click(timeout=5000)
    file_chooser = await fc_info.value
    await file_chooser.set_files(str(cv_path))


async def fill_field_by_label(page: "Page", label_text: str, value: str) -> bool:
    """Find an input associated with label_text and type value with human-like delays."""
    locator = page.get_by_label(re.compile(label_text, re.IGNORECASE))
    try:
        el = locator.first
        await el.click(timeout=3000)
        await el.press_sequentially(value, delay=random.uniform(60, 180))
        return True
    except Exception:
        try:
            await locator.first.fill(value, timeout=3000)
            return True
        except Exception:
            return False


async def fill_screening_questions(page: "Page", answers: dict) -> None:
    """Best-effort: match visible question text against screening_answers dict."""
    qa = answers.get("screening_answers", {})
    for question_key, answer_value in qa.items():
        for label in await page.locator("label").all():
            try:
                text = (await label.inner_text()).lower()
            except Exception:
                continue
            if question_key.lower() in text:
                for_attr = await label.get_attribute("for")
                if for_attr:
                    inp = page.locator(f"#{for_attr}")
                    try:
                        tag = await inp.evaluate("el => el.tagName")
                        if tag == "INPUT":
                            await inp.click(timeout=2000)
                            await inp.press_sequentially(str(answer_value), delay=random.uniform(60, 180))
                        elif tag == "SELECT":
                            await inp.select_option(label=str(answer_value), timeout=2000)
                        elif tag == "TEXTAREA":
                            await inp.click(timeout=2000)
                            await inp.press_sequentially(str(answer_value), delay=random.uniform(60, 180))
                    except Exception:
                        pass
                break
