"""`StructuredFormDriver`: the shared machinery every ATS driver is built on (§3, §26-28).

Greenhouse, Lever and Ashby are all structured HTML application forms — labelled inputs, a file
upload or two, a submit control, a confirmation page. That shape is captured once here, and each
driver supplies only what differs (its URL patterns, where its form lives, how it confirms). No
driver reuses another's selectors under a generic wrapper (§27): each subclass declares its own
recognition marker, navigation and confirmation, so a change to how Lever confirms never silently
alters Greenhouse.

The driver's two responsibilities, and their limits:

- **prepare** navigates, detects a hard stop (CAPTCHA/MFA/login, §16), validates the page really is
  this ATS's form (§4), reads the fields into typed `ApplicationQuestion`s with stable keys (§9),
  classifies their sensitivity (§10) and fingerprints the structure (§14). It never clicks submit.
- **submit** detects a hard stop again, fills only the answers it was handed (§12), uploads only the
  pinned files it was handed (§13), and clicks the one submit control at most once (§20), then reads
  a *positive* confirmation (§21). A field it cannot fill, a control it cannot find, is a pre-click
  failure — safe to retry — never a blind second click.

Field extraction runs one bounded `page.evaluate` that returns field *metadata* (name, type,
required, label, options) — never raw HTML, never a value already on the page (§8, §24, §33).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from backend.app.application_engine.browser import semantics
from backend.app.application_engine.browser.challenges import detect_challenge
from backend.app.application_engine.browser.contracts import (
    BrowserSubmissionEvidence,
    DriverContext,
    PreparedBrowserForm,
)
from backend.app.application_engine.browser.fingerprint import compute_form_fingerprint
from backend.app.domain.application_answer import (
    ApplicationQuestion,
    ApplicationQuestionKind,
    QuestionSensitivity,
)
from backend.app.domain.application_channel import HumanRequiredReason
from backend.app.domain.documents import CandidateDocumentType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import Locator, Page

# Bounded waits everywhere (§32): no infinite waits, and every value is a ceiling, not a target.
NAV_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 8_000
CONFIRM_TIMEOUT_MS = 15_000

# The one JS reader: it returns field *metadata* for the controls inside a form scope — never HTML,
# never a filled value. Hidden inputs (CSRF tokens and the like) and buttons are skipped, so a
# volatile token never becomes a question and never perturbs the fingerprint (§14).
_EXTRACT_JS = r"""
(formSelector) => {
  const root = document.querySelector(formSelector) || document.body;
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l && l.innerText) return l.innerText.trim();
    }
    const wrap = el.closest('label');
    if (wrap && wrap.innerText) return wrap.innerText.trim();
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const l = document.getElementById(lb);
      if (l && l.innerText) return l.innerText.trim();
    }
    if (el.placeholder) return el.placeholder.trim();
    return (el.name || el.id || '').trim();
  };
  const visible = (el) => !!(el.offsetParent !== null || el.getClientRects().length);
  const out = [];
  const seenRadio = new Set();
  root.querySelectorAll('input, textarea, select').forEach((el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' &&
        ['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) return;
    if (type !== 'file' && !visible(el)) return;
    const required = el.required || el.getAttribute('aria-required') === 'true';
    const label = labelFor(el);
    if (tag === 'input' && type === 'radio') {
      const name = el.name || el.id;
      if (!name || seenRadio.has(name)) return;
      seenRadio.add(name);
      const group = Array.from(
        root.querySelectorAll('input[type="radio"][name="' + CSS.escape(name) + '"]'));
      let groupLabel = label;
      const fs = el.closest('fieldset');
      if (fs) {
        const lg = fs.querySelector('legend');
        if (lg && lg.innerText) groupLabel = lg.innerText.trim();
      }
      out.push({
        name, id: el.id || '', tag: 'radiogroup', type: 'radio',
        required: group.some((r) => r.required),
        label: groupLabel,
        options: group.map((r) => labelFor(r) || r.value).filter((x) => x),
        multiple: false});
      return;
    }
    let options = [];
    let multiple = false;
    if (tag === 'select') {
      multiple = el.multiple;
      options = Array.from(el.options)
        .map((o) => (o.label || o.text || o.value || '').trim())
        .filter((x) => x);
    }
    out.push({name: el.name || '', id: el.id || '', tag, type, required, label, options, multiple});
  });
  return out;
}
"""


class StructuredFormDriver:
    """Base for the structured-form ATS drivers. Subclasses set the class attributes below."""

    DRIVER_KEY: str = "structured-form/1"
    PLATFORM: str = "structured-form"
    URL_PATTERNS: tuple[str, ...] = ()
    ALLOWED_HOSTS: tuple[str, ...] = ()
    # A CSS scope for the application form's controls, so extraction ignores site chrome.
    FORM_SELECTOR: str = "form"
    # A structural marker that must be present for the page to *be* this ATS's form (§4).
    RECOGNITION_SELECTORS: tuple[str, ...] = ("form",)
    # The submit control. The one irreversible click lands here (§20).
    SUBMIT_SELECTORS: tuple[str, ...] = ("button[type='submit']", "input[type='submit']")
    # Positive confirmation (§21): a URL fragment or on-page text that only appears once submitted.
    CONFIRM_URL_KEYWORDS: tuple[str, ...] = ("confirmation", "thank", "success", "submitted")
    CONFIRM_TEXT_KEYWORDS: tuple[str, ...] = (
        "thank you", "merci", "submitted", "application received", "candidature envoyée")
    # A container whose presence is itself confirmation, and whose text may hold a reference (§24).
    CONFIRM_SELECTORS: tuple[str, ...] = ()

    @property
    def driver_key(self) -> str:
        return self.DRIVER_KEY

    @property
    def platform(self) -> str:
        return self.PLATFORM

    def supports(self, target_url: str | None) -> bool:
        """Whether this driver's URL patterns nominate it for the target (§4)."""
        if not target_url:
            return False
        try:
            parsed = urlsplit(target_url)
            host = parsed.hostname
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"} or host not in self.ALLOWED_HOSTS:
            return False
        return any(re.search(pattern, target_url, re.IGNORECASE)
                   for pattern in self.URL_PATTERNS)

    # -- navigation ----------------------------------------------------------

    async def _navigate(self, page: Page, url: str) -> None:
        """Load the application form. Subclasses extend (Lever's `/apply`, Ashby's tab)."""
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=ACTION_TIMEOUT_MS)
        except Exception:  # a SPA may never idle; the form is already loading
            pass

    async def _is_recognized(self, page: Page) -> bool:
        """Whether the page structurally is this ATS's application form (§4)."""
        for selector in self.RECOGNITION_SELECTORS:
            try:
                if await page.locator(selector).count() > 0:
                    return True
            except Exception:  # best-effort: a page fault must not crash this
                continue
        return False

    # -- prepare -------------------------------------------------------------

    async def prepare(self, page: Page, context: DriverContext) -> PreparedBrowserForm:
        """Navigate, detect a hard stop, inspect the form, and fingerprint it — never submit."""
        if context.target_url:
            await self._navigate(page, context.target_url)

        challenge = await detect_challenge(page)
        if challenge is not None:
            return PreparedBrowserForm(
                platform=self.PLATFORM, human_required_reason=challenge,
                detail=self._challenge_detail(challenge))

        if not await self._is_recognized(page):
            return PreparedBrowserForm(
                platform=self.PLATFORM,
                human_required_reason=HumanRequiredReason.AMBIGUOUS_FORM,
                detail="the page did not match this ATS's application form")

        questions = await self._extract_questions(page)
        fingerprint = compute_form_fingerprint(self.PLATFORM, questions)
        return PreparedBrowserForm(
            platform=self.PLATFORM, questions=questions, form_fingerprint=fingerprint,
            detail=f"read {len(questions)} field(s) from the {self.PLATFORM} form")

    async def _extract_questions(
            self, page: Page) -> tuple[ApplicationQuestion, ...]:
        """Read the form's controls into typed questions with stable keys (§9-10)."""
        raw_fields: list[dict[str, Any]] = await page.evaluate(
            _EXTRACT_JS, self.FORM_SELECTOR)
        questions: list[ApplicationQuestion] = []
        used_keys: set[str] = set()
        for raw in raw_fields:
            question = self._to_question(raw, used_keys)
            if question is not None:
                questions.append(question)
                used_keys.add(question.key)
        return tuple(questions)

    def _to_question(
            self, raw: dict[str, Any], used_keys: set[str]) -> ApplicationQuestion | None:
        """Map one raw field's metadata to a typed question, or `None` to skip it."""
        label = str(raw.get("label") or "").strip()
        name = str(raw.get("name") or "") or None
        kind, options = self._kind_and_options(raw)
        if kind is None:
            return None
        if kind is ApplicationQuestionKind.FILE_UPLOAD:
            key = semantics.upload_slot(label, name)
            sensitivity = QuestionSensitivity.NORMAL
        else:
            key = (semantics.canonical_key(label, name)
                   or name or _slug(label) or str(raw.get("id") or ""))
            sensitivity = semantics.classify_sensitivity(label, name)
        if not key:
            return None
        key = _unique(key, used_keys)
        return ApplicationQuestion(
            key=key, label=label or key, kind=kind,
            required=bool(raw.get("required")), sensitivity=sensitivity,
            options=tuple(options))

    def _kind_and_options(
            self, raw: dict[str, Any]) -> tuple[ApplicationQuestionKind | None, tuple[str, ...]]:
        """The question kind and (for a choice) its options, from a raw field's metadata."""
        tag = str(raw.get("tag") or "")
        field_type = str(raw.get("type") or "")
        options = tuple(str(option) for option in raw.get("options") or () if str(option).strip())
        if tag == "input" and field_type == "file":
            return ApplicationQuestionKind.FILE_UPLOAD, ()
        if tag == "textarea":
            return ApplicationQuestionKind.LONG_TEXT, ()
        if tag == "select":
            if not options:
                return ApplicationQuestionKind.SHORT_TEXT, ()
            kind = (ApplicationQuestionKind.MULTIPLE_CHOICE if raw.get("multiple")
                    else ApplicationQuestionKind.SINGLE_CHOICE)
            return kind, options
        if tag == "radiogroup":
            if not options:
                return None, ()
            return ApplicationQuestionKind.SINGLE_CHOICE, options
        if field_type == "checkbox":
            return ApplicationQuestionKind.BOOLEAN, ()
        if field_type == "number":
            return ApplicationQuestionKind.NUMBER, ()
        if field_type == "date":
            return ApplicationQuestionKind.DATE, ()
        # text, email, tel, url and anything textual
        return ApplicationQuestionKind.SHORT_TEXT, ()

    # -- submit --------------------------------------------------------------

    async def submit(self, page: Page, context: DriverContext) -> BrowserSubmissionEvidence:
        """Fill answers, upload pinned files, and click submit at most once (§20)."""
        challenge = await detect_challenge(page)
        if challenge is not None:
            return BrowserSubmissionEvidence(
                human_required_reason=challenge, detail=self._challenge_detail(challenge))

        try:
            await self._fill_answers(page, context)
            await self._upload_files(page, context)
        except Exception:  # a pre-click failure is safe to retry; nothing was sent
            return BrowserSubmissionEvidence(
                detail="a field could not be filled before submission; nothing was sent")

        submit = await self._submit_control(page)
        if submit is None:
            return BrowserSubmissionEvidence(
                detail="no submit control was found on the form; nothing was sent")

        # The one irreversible click. Past this point any error is STATE_UNKNOWN, never a retry
        # (§20, §22): we do not know whether the request left the platform.
        try:
            await submit.click(timeout=ACTION_TIMEOUT_MS)
        except Exception:  # the click may or may not have landed
            return BrowserSubmissionEvidence(
                clicked=True,
                detail="the submit click did not complete cleanly; the outcome is unknown")

        try:
            confirmed, reference = await self._read_confirmation(page)
        except Exception:  # the page may have crashed post-click (§22)
            return BrowserSubmissionEvidence(
                clicked=True,
                detail="the confirmation could not be read after submitting; outcome unknown")

        if confirmed:
            return BrowserSubmissionEvidence(
                clicked=True, confirmed=True, confirmation_reference=reference,
                detail=f"the {self.PLATFORM} form confirmed the submission")
        return BrowserSubmissionEvidence(
            clicked=True,
            detail="submitted but no positive confirmation was read; outcome unknown")

    async def _fill_answers(self, page: Page, context: DriverContext) -> None:
        """Type each cleared answer into its field. Fills only what it was handed (§12)."""
        for answer in context.answers:
            await self._fill_one(page, answer.question_key, answer.value)

    async def _fill_one(self, page: Page, key: str, value: str) -> None:
        """Locate the field named `key` and set `value`, dispatching on the control kind."""
        locator = await self._locate(page, key)
        if locator is None:
            # A shared answer bundle may contain keys for another ATS. Required fields are
            # checked by the driver-agnostic orchestrator before this method runs; an answer for
            # a field absent from this form is therefore irrelevant and must not fail submission.
            return
        tag = (await locator.evaluate("el => el.tagName")).lower()
        if tag == "select":
            await locator.select_option(label=value, timeout=ACTION_TIMEOUT_MS)
            return
        field_type = (await locator.get_attribute("type") or "").lower()
        if field_type == "checkbox":
            if _is_truthy(value):
                await locator.check(timeout=ACTION_TIMEOUT_MS)
            else:
                await locator.uncheck(timeout=ACTION_TIMEOUT_MS)
            return
        if field_type == "radio":
            await page.get_by_label(value).check(timeout=ACTION_TIMEOUT_MS)
            return
        await locator.fill(value, timeout=ACTION_TIMEOUT_MS)

    async def _locate(self, page: Page, key: str) -> Locator | None:
        """A locator for the field named `key`: its DOM name, else its canonical label (§18)."""
        by_name = page.locator(
            f"input[name={key!r}], textarea[name={key!r}], select[name={key!r}]")
        if await by_name.count() > 0:
            return by_name.first
        pattern = semantics.LABEL_PATTERNS.get(key)
        if pattern is not None:
            by_label = page.get_by_label(re.compile(pattern, re.IGNORECASE))
            if await by_label.count() > 0:
                return by_label.first
        return None

    async def _upload_files(self, page: Page, context: DriverContext) -> None:
        """Attach the pinned files to their slots — exactly those, never "the latest" (§13)."""
        raw_fields = await page.evaluate(_EXTRACT_JS, self.FORM_SELECTOR)
        fields = [field for field in raw_fields if field.get("type") == "file"]
        inputs = page.locator(self.FORM_SELECTOR).first.locator("input[type='file']")
        if await inputs.count() != len(fields):
            raise ValueError("document upload form changed")
        types = {
            semantics.RESUME_SLOT: CandidateDocumentType.RESUME,
            semantics.COVER_LETTER_SLOT: CandidateDocumentType.COVER_LETTER,
        }
        slots = [semantics.upload_slot(
            str(field.get("label") or ""), field.get("name")) for field in fields]
        if any(slots.count(slot) > 1 for slot in types):
            raise ValueError("ambiguous document upload slots")
        for index, slot in enumerate(slots):
            document_type = types.get(slot)
            upload = context.upload_for(document_type) if document_type is not None else None
            if upload is not None:
                await inputs.nth(index).set_input_files({
                    "name": upload.filename, "mimeType": "application/pdf",
                    "buffer": Path(upload.path).read_bytes(),
                }, timeout=ACTION_TIMEOUT_MS)

    async def _submit_control(self, page: Page) -> Locator | None:
        """The one submit control, or `None` if the form has none."""
        for selector in self.SUBMIT_SELECTORS:
            locator = page.locator(selector)
            if await locator.count() > 0:
                return locator.first
        return None

    async def _read_confirmation(self, page: Page) -> tuple[bool, str | None]:
        """Positive confirmation and an optional reference — never mere absence of error (§21)."""
        url = (page.url or "").lower()
        if any(keyword in url for keyword in self.CONFIRM_URL_KEYWORDS):
            return True, await self._confirmation_reference(page)
        for selector in self.CONFIRM_SELECTORS:
            if await page.locator(selector).count() > 0:
                return True, await self._confirmation_reference(page)
        try:
            body = (await page.inner_text("body", timeout=ACTION_TIMEOUT_MS)).lower()
        except Exception:  # best-effort: a page fault must not crash this
            return False, None
        if any(keyword in body for keyword in self.CONFIRM_TEXT_KEYWORDS):
            return True, None
        return False, None

    async def _confirmation_reference(self, page: Page) -> str | None:
        """A confirmation reference the ATS displays, when present (§24). Never a secret."""
        for selector in self.CONFIRM_SELECTORS:
            locator = page.locator(selector)
            if await locator.count() > 0:
                try:
                    text = (await locator.first.inner_text(timeout=ACTION_TIMEOUT_MS)).strip()
                except Exception:  # best-effort: a page fault must not crash this
                    return None
                match = re.search(r"[A-Z0-9]{6,}", text)
                return match.group(0) if match else None
        return None

    def _challenge_detail(self, reason: HumanRequiredReason) -> str:
        return {
            HumanRequiredReason.CAPTCHA_PRESENT:
                "a CAPTCHA is present; a human must complete it (never solved automatically)",
            HumanRequiredReason.MFA_REQUIRED:
                "a multi-factor prompt is present; a human must complete it",
            HumanRequiredReason.LOGIN_REQUIRED:
                "the page requires a login; a human must authenticate",
        }.get(reason, "a human is required to continue")


def _slug(label: str) -> str:
    """A stable, url-safe key from a label, when a field carries no name (§9)."""
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _unique(key: str, used: set[str]) -> str:
    """`key`, or `key__2`, `key__3`… so two same-named fields keep distinct stable keys."""
    if key not in used:
        return key
    index = 2
    while f"{key}__{index}" in used:
        index += 1
    return f"{key}__{index}"


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"true", "yes", "1", "oui", "on", "checked", "y"}
