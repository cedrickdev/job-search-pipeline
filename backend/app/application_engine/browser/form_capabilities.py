"""Operator-reviewed authority for one exact Swiss ATS form, default-denied."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from backend.app.domain.application_answer import (
    ApplicationQuestion,
    ApplicationQuestionKind,
    QuestionSensitivity,
)
from backend.app.domain.application_channel import AdapterSafetyLevel

DEFAULT_FORM_CAPABILITIES_PATH = (
    Path(__file__).resolve().parents[4] / "config/ats_approved_forms.yaml"
)
_MAX_REVIEW_AGE = timedelta(days=180)
_HOSTS = {
    "lever": frozenset({"jobs.lever.co"}),
    "greenhouse": frozenset({
        "boards.greenhouse.io", "job-boards.greenhouse.io", "grnh.se",
        "greenhouse.io",
    }),
}
_REQUIRED_FIELDS = frozenset({
    "first_name", "last_name", "full_name", "email", "phone", "linkedin", "location",
})
_UPLOAD_SLOTS = frozenset({"resume", "cover_letter"})
_KEYS = frozenset({
    "id", "country", "ats", "employer", "target_url", "driver_key",
    "form_fingerprint", "allowed_required_fields", "allowed_upload_slots",
    "conditions_url", "review_id", "reviewed_at", "access_decision",
})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_FINGERPRINT = re.compile(r"(?:[a-f0-9]{64}|sha256:[A-Za-z0-9_-]{7,128})\Z")


class FormCapabilityCatalogError(ValueError):
    """Operator catalog is missing or invalid. Never echo its contents."""


@dataclass(frozen=True)
class FormCapability:
    id: str
    ats: str
    employer: str
    target_url: str
    driver_key: str
    form_fingerprint: str
    allowed_required_fields: frozenset[str]
    allowed_upload_slots: frozenset[str]
    conditions_url: str
    review_id: str
    reviewed_at: date
    access_decision: str


@dataclass(frozen=True)
class FormCapabilityDecision:
    safety_level: AdapterSafetyLevel
    capability_id: str | None
    reason_code: str


@dataclass(frozen=True)
class FormCapabilityCatalog:
    forms: tuple[FormCapability, ...]

    def resolve(
        self, target_url: str | None, driver_key: str | None,
        fingerprint: str | None,
    ) -> FormCapabilityDecision:
        target = _canonical_url(target_url, ats=None) if target_url else None
        if target is not None and driver_key and fingerprint:
            for form in self.forms:
                if (form.access_decision == "approved" and form.target_url == target
                        and form.driver_key == driver_key
                        and form.form_fingerprint == fingerprint):
                    return FormCapabilityDecision(
                        AdapterSafetyLevel.FULLY_SUPPORTED, form.id, "FORM_APPROVED",
                    )
        return FormCapabilityDecision(
            AdapterSafetyLevel.SUPPORTED_WITH_REVIEW, None, "FORM_REVIEW_REQUIRED",
        )

    def allows_questions(
        self, capability_id: str, questions: tuple[ApplicationQuestion, ...],
    ) -> bool:
        form = next((item for item in self.forms
                     if item.id == capability_id and item.access_decision == "approved"), None)
        if form is None:
            return False
        return all(
            question.sensitivity is QuestionSensitivity.NORMAL
            and (question.key in form.allowed_upload_slots
                 if question.kind is ApplicationQuestionKind.FILE_UPLOAD
                 else question.key in form.allowed_required_fields)
            for question in questions if question.required
        )


def _canonical_url(value: object, *, ats: str | None) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme != "https" or not host or parts.username is not None
                or parts.password is not None or parts.fragment or parts.port is not None
                or parts.netloc != host or not parts.path.startswith("/")
                or "//" in parts.path or "%" in parts.path
                or any(segment in {".", ".."} for segment in parts.path.split("/"))
                or any(char.isspace() or ord(char) < 32 for char in value)):
            return None
        if ats is not None and host not in _HOSTS[ats]:
            return None
        if host == "greenhouse.io" and not parts.path.startswith("/embed/"):
            return None
        return value
    except ValueError:
        return None


def _parse_date(value: object, today: date) -> date | None:
    try:
        reviewed = value if type(value) is date else date.fromisoformat(value) \
            if isinstance(value, str) else None
    except ValueError:
        return None
    return reviewed if reviewed and today - _MAX_REVIEW_AGE <= reviewed <= today else None


def _parse_fields(value: object, allowed: frozenset[str]) -> frozenset[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    if len(value) != len(set(value)) or not set(value) <= allowed:
        return None
    return frozenset(value)


def _parse_form(raw: object, today: date) -> FormCapability:
    if not isinstance(raw, dict) or set(raw) != _KEYS:
        raise FormCapabilityCatalogError("Form review has invalid fields")
    ats = raw["ats"]
    if not isinstance(ats, str) or ats not in _HOSTS:
        raise FormCapabilityCatalogError("Form ATS is invalid")
    target = _canonical_url(raw["target_url"], ats=ats)
    conditions = _canonical_url(raw["conditions_url"], ats=None)
    reviewed = _parse_date(raw["reviewed_at"], today)
    fields = _parse_fields(raw["allowed_required_fields"], _REQUIRED_FIELDS)
    uploads = _parse_fields(raw["allowed_upload_slots"], _UPLOAD_SLOTS)
    if (raw["country"] != "CH" or target is None or conditions is None
            or reviewed is None or fields is None or uploads is None
            or raw["access_decision"] not in ("approved", "hold")
            or not isinstance(raw["id"], str) or not _IDENTIFIER.fullmatch(raw["id"])
            or not isinstance(raw["review_id"], str)
            or not _IDENTIFIER.fullmatch(raw["review_id"])
            or not isinstance(raw["employer"], str)
            or not 1 <= len(raw["employer"].strip()) <= 200
            or raw["driver_key"] != f"{ats}/1"
            or not isinstance(raw["form_fingerprint"], str)
            or not _FINGERPRINT.fullmatch(raw["form_fingerprint"])):
        raise FormCapabilityCatalogError("Form review is invalid")
    return FormCapability(
        id=raw["id"], ats=ats, employer=raw["employer"].strip(),
        target_url=target, driver_key=raw["driver_key"],
        form_fingerprint=raw["form_fingerprint"],
        allowed_required_fields=fields, allowed_upload_slots=uploads,
        conditions_url=conditions, review_id=raw["review_id"],
        reviewed_at=reviewed, access_decision=raw["access_decision"],
    )


def load_form_capabilities(path: Path, *, today: date) -> FormCapabilityCatalog:
    """Parse an immutable operator snapshot without network access or fallback."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise FormCapabilityCatalogError("Form catalog unavailable or invalid") from error
    if (not isinstance(raw, dict) or set(raw) != {"version", "forms"}
            or type(raw["version"]) is not int or raw["version"] != 1
            or not isinstance(raw["forms"], list)):
        raise FormCapabilityCatalogError("Form catalog schema is invalid")
    forms = tuple(_parse_form(item, today) for item in raw["forms"])
    if len({item.id for item in forms}) != len(forms) or len({
        (item.target_url, item.driver_key, item.form_fingerprint) for item in forms
    }) != len(forms):
        raise FormCapabilityCatalogError("Form capability is duplicated")
    return FormCapabilityCatalog(forms=forms)
