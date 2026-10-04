"""Field semantics: what a form field *means*, from its label and name (§9-10, §18).

Two site-agnostic classifications every driver shares, kept here so the knowledge lives once rather
than in each ATS driver:

- **the canonical key** (§9, §18): a first-name field is `first_name` whether Greenhouse calls its
  input ``job_application[first_name]`` or Ashby calls it ``_systemfield_name``. Mapping recognized
  standard fields to a stable canonical key is what lets `propose_from_profile` fill them from the
  profile and the driver locate them at submit; an unrecognized field keeps its own stable id as
  its key, so a custom screening question is still identified deterministically.
- **the sensitivity** (§10): a field asking about gender, ethnicity, disability, a criminal record
  or a legal work-authorization attestation is never answered automatically. Classifying it as
  non-`NORMAL` here means `resolve_answer` routes it to a human as `SENSITIVE_QUESTION`,
  structurally — the platform never infers a protected characteristic or signs an attestation.

The matching is deliberately conservative: it errs toward `SENSITIVE` (hand to a human) and toward
"custom question" (do not assume a profile value fits), because the cost of a wrong auto-answer is
far higher than the cost of asking a person.
"""
from __future__ import annotations

import re

from backend.app.domain.application_answer import QuestionSensitivity

# Canonical keys for the standard identity fields a driver can fill from the profile or a resolved
# declaration. Everything else keeps its own ATS field id as its key (a custom question, §9).
FIRST_NAME = "first_name"
LAST_NAME = "last_name"
FULL_NAME = "full_name"
EMAIL = "email"
PHONE = "phone"
LINKEDIN = "linkedin"
LOCATION = "location"

# Ordered most-specific-first: "first name" must win over the bare "name" that maps to full_name.
_CANONICAL_PATTERNS: tuple[tuple[str, str], ...] = (
    (FIRST_NAME, r"first\s*name|prénom|prenom|given name|vorname"),
    (LAST_NAME, r"last\s*name|surname|family name|nom de famille|\bnom\b|nachname"),
    (EMAIL, r"e-?mail|courriel|adresse électronique"),
    (PHONE, r"phone|téléphone|telephone|mobile|portable"),
    (LINKEDIN, r"linkedin"),
    (LOCATION, r"location|ville|localisation|city|adresse|address|standort"),
    (FULL_NAME, r"full name|nom complet|votre nom|your name|\bname\b"),
)

# Sensitive-topic patterns (§10). Any match makes the field non-NORMAL, so it is never auto-answered
# and is routed to a human. LEGAL is an attestation only the candidate may make; DEMOGRAPHIC is a
# protected characteristic that must never be inferred; SENSITIVE is private-but-not-protected.
_SENSITIVITY_PATTERNS: tuple[tuple[QuestionSensitivity, str], ...] = (
    (QuestionSensitivity.DEMOGRAPHIC,
     r"gender|genre|\bsex\b|sexe|race|ethnic|ethni|origin|disab|handicap|"
     r"veteran|militaire|sexual orientation|orientation sexuelle|religion|"
     r"political|politique|marital|marié|date of birth|date de naissance|"
     r"pregnan|grossesse|transgender"),
    (QuestionSensitivity.LEGAL,
     r"criminal|casier judiciaire|conviction|felony|"
     r"work authoriz|authorization to work|right to work|"
     r"autorisation de travail|permis de travail|sponsor|visa status|"
     r"security clearance|habilitation"),
    (QuestionSensitivity.SENSITIVE,
     r"salary|salaire|compensation|rémunération|remuneration|"
     r"expected pay|prétentions salariales|health|santé|medical|médical"),
)


# The same patterns keyed for lookup, so a driver can re-find a standard field by its canonical key
# at submit time via ``get_by_label`` (§18) when the field carries no stable DOM name to match on.
LABEL_PATTERNS: dict[str, str] = dict(_CANONICAL_PATTERNS)

# File-upload slots (§13, §38): which pinned document a file input is asking for.
RESUME_SLOT = "resume"
COVER_LETTER_SLOT = "cover_letter"
_UPLOAD_PATTERNS: tuple[tuple[str, str], ...] = (
    (COVER_LETTER_SLOT, r"cover letter|lettre de motivation|motivation"),
    (RESUME_SLOT, r"resume|résumé|\bcv\b|curriculum"),
)


def upload_slot(label: str, field_name: str | None) -> str:
    """Which document a file input asks for — resume unless it clearly asks for a cover letter."""
    haystack = f"{label} {field_name or ''}".lower()
    for slot, pattern in _UPLOAD_PATTERNS:
        if re.search(pattern, haystack):
            return slot
    return RESUME_SLOT


def canonical_key(label: str, field_name: str | None) -> str | None:
    """The canonical key a standard identity field maps to, or `None` for a custom field.

    Both the human label and the input's own name are searched, so a field is recognized whether
    the ATS labels it visibly or only names the input. `None` means "not a standard field" — the
    driver keeps the field's own stable id as its key rather than guessing a profile value fits.
    """
    haystack = f"{label} {field_name or ''}".lower()
    for key, pattern in _CANONICAL_PATTERNS:
        if re.search(pattern, haystack):
            return key
    return None


def classify_sensitivity(label: str, field_name: str | None) -> QuestionSensitivity:
    """The sensitivity of a field, from its label and name (§10).

    Errs toward caution: any match on a protected, legal or private topic returns the corresponding
    non-`NORMAL` sensitivity, which `resolve_answer` turns into a `SENSITIVE_QUESTION` hand-off. An
    ordinary field (years of experience, notice period) is `NORMAL` and may be answered.
    """
    haystack = f"{label} {field_name or ''}".lower()
    for sensitivity, pattern in _SENSITIVITY_PATTERNS:
        if re.search(pattern, haystack):
            return sensitivity
    return QuestionSensitivity.NORMAL
