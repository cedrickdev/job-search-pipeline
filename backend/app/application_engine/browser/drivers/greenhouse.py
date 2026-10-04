"""The Greenhouse driver (boards.greenhouse.io, job-boards.greenhouse.io, grnh.se) — §26.

Greenhouse serves the application form directly on the posting URL, inside a `#application_form`
(or `#application-form`) container. The DOM knowledge that was proven in V1's `pipeline/apply/
greenhouse.py` lives here and only here (§5, §27): the form scope, the submit control's French and
English labels, and the confirmation cues. Personal fields, email, phone, resume/cover-letter
upload, text and textarea questions, selects, radios and required-field detection are all handled
by the structured base; a widget the base cannot represent surfaces as a question with no fill and
becomes an `UNKNOWN_REQUIRED_FIELD` hand-off rather than a guess.
"""
from __future__ import annotations

from backend.app.application_engine.browser.drivers.base import StructuredFormDriver


class GreenhouseDriver(StructuredFormDriver):
    DRIVER_KEY = "greenhouse/1"
    PLATFORM = "greenhouse"
    ALLOWED_HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io", "grnh.se",
                     "greenhouse.io")
    URL_PATTERNS = (
        r"job-boards\.greenhouse\.io",
        r"boards\.greenhouse\.io",
        r"grnh\.se",
        r"greenhouse\.io/embed",
    )
    FORM_SELECTOR = "#application_form, #application-form, form#application-form, form"
    RECOGNITION_SELECTORS = (
        "#application_form",
        "#application-form",
        "form[action*='greenhouse']",
        "div[class*='application']",
    )
    SUBMIT_SELECTORS = (
        "#submit_app",
        "button:has-text('Submit Application')",
        "button:has-text('Submit application')",
        "button:has-text('Envoyer ma candidature')",
        "input[type='submit']",
        "button[type='submit']",
    )
    CONFIRM_URL_KEYWORDS = ("confirmation", "thank", "success", "submitted", "post_app")
    CONFIRM_TEXT_KEYWORDS = (
        "thank you", "merci", "submitted", "candidature envoyée",
        "application has been submitted", "we've received")
    CONFIRM_SELECTORS = ("#application_confirmation", ".application-confirmation")
