"""An operator review authorizes only one exact Swiss ATS form variant."""

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from backend.app.application_engine.browser.form_capabilities import (
    FormCapabilityCatalogError,
    load_form_capabilities,
)
from backend.app.domain.application_answer import (
    ApplicationQuestion,
    ApplicationQuestionKind,
)
from backend.app.domain.application_channel import AdapterSafetyLevel
from backend.app.core.settings import BrowserSubmissionSettings


TODAY = date(2026, 10, 13)
TARGET = "https://jobs.lever.co/fixture/role/apply?source=site"


def _form(**changes: object) -> dict[str, object]:
    form: dict[str, object] = {
        "id": "lever-fixture-role-v1",
        "country": "CH",
        "ats": "lever",
        "employer": "Fixture SA",
        "target_url": TARGET,
        "driver_key": "lever/1",
        "form_fingerprint": "sha256:fixture",
        "allowed_required_fields": ["full_name", "email"],
        "allowed_upload_slots": ["resume"],
        "conditions_url": "https://fixture.example/terms",
        "review_id": "review-2026-10",
        "reviewed_at": TODAY.isoformat(),
        "access_decision": "approved",
    }
    form.update(changes)
    return form


def _write(path: Path, forms: list[dict[str, object]]) -> Path:
    path.write_text(yaml.safe_dump({"version": 1, "forms": forms}), encoding="utf-8")
    return path


def test_packaged_catalog_is_empty_and_missing_file_refuses(tmp_path: Path) -> None:
    catalog = load_form_capabilities(Path("config/ats_approved_forms.yaml"), today=TODAY)
    assert catalog.resolve(TARGET, "lever/1", "sha256:fixture").safety_level is (
        AdapterSafetyLevel.SUPPORTED_WITH_REVIEW
    )
    with pytest.raises(FormCapabilityCatalogError):
        load_form_capabilities(tmp_path / "missing.yaml", today=TODAY)


def test_exact_form_receives_scoped_authority(tmp_path: Path) -> None:
    catalog = load_form_capabilities(_write(tmp_path / "forms.yaml", [_form()]), today=TODAY)
    decision = catalog.resolve(TARGET, "lever/1", "sha256:fixture")
    assert decision.safety_level is AdapterSafetyLevel.FULLY_SUPPORTED
    assert decision.capability_id == "lever-fixture-role-v1"

    for target, driver, fingerprint in (
        (TARGET.replace("/role/", "/other/"), "lever/1", "sha256:fixture"),
        (TARGET.replace("site", "other"), "lever/1", "sha256:fixture"),
        (TARGET, "lever/2", "sha256:fixture"),
        (TARGET, "sha256:fixture", "lever/1"),
        (TARGET, "lever/1", "sha256:changed"),
        ("https://other.example/apply", "lever/1", "sha256:fixture"),
    ):
        denied = catalog.resolve(target, driver, fingerprint)
        assert denied.safety_level is AdapterSafetyLevel.SUPPORTED_WITH_REVIEW
        assert denied.capability_id is None


def test_actual_browser_digest_format_is_accepted(tmp_path: Path) -> None:
    digest = "a" * 64
    catalog = load_form_capabilities(
        _write(tmp_path / "forms.yaml", [_form(form_fingerprint=digest)]), today=TODAY,
    )
    assert catalog.resolve(TARGET, "lever/1", digest).capability_id == "lever-fixture-role-v1"


def test_required_fields_and_upload_slots_are_bounded(tmp_path: Path) -> None:
    catalog = load_form_capabilities(_write(tmp_path / "forms.yaml", [_form()]), today=TODAY)
    def question(key: str, kind: ApplicationQuestionKind) -> ApplicationQuestion:
        return ApplicationQuestion(key=key, label=key, kind=kind, required=True)
    assert catalog.allows_questions("lever-fixture-role-v1", (
        question("email", ApplicationQuestionKind.SHORT_TEXT),
        question("resume", ApplicationQuestionKind.FILE_UPLOAD),
    ))
    assert not catalog.allows_questions("lever-fixture-role-v1", (
        question("salary", ApplicationQuestionKind.SHORT_TEXT),
    ))
    assert not catalog.allows_questions("lever-fixture-role-v1", (
        question("cover_letter", ApplicationQuestionKind.FILE_UPLOAD),
    ))
    assert not catalog.allows_questions("unknown", ())


@pytest.mark.parametrize("change", [
    {"country": "FR"},
    {"ats": "other"},
    {"driver_key": "greenhouse/1"},
    {"target_url": "http://jobs.lever.co/fixture/role/apply"},
    {"target_url": "https://user:secret@jobs.lever.co/fixture/role/apply"},
    {"target_url": "https://jobs.lever.co/fixture/role/apply#anchor"},
    {"target_url": "https://jobs.lever.co:443/fixture/role/apply"},
    {"target_url": "https://jobs.lever.co/fixture/../role/apply"},
    {"target_url": "https://jobs.lever.co.evil.example/fixture/role/apply"},
    {"conditions_url": "https://user:secret@fixture.example/terms"},
    {"reviewed_at": (TODAY - timedelta(days=181)).isoformat()},
    {"reviewed_at": (TODAY + timedelta(days=1)).isoformat()},
    {"allowed_required_fields": ["email", "email"]},
    {"allowed_required_fields": ["email", "submit"]},
    {"allowed_upload_slots": ["arbitrary_attachment"]},
    {"access_decision": "yes"},
])
def test_malformed_entries_fail_closed_without_leaking_values(
    tmp_path: Path, change: dict[str, object],
) -> None:
    path = _write(tmp_path / "forms.yaml", [_form(**change)])
    with pytest.raises(FormCapabilityCatalogError) as error:
        load_form_capabilities(path, today=TODAY)
    assert "secret" not in str(error.value)


def test_duplicate_identity_and_hold_are_not_authority(tmp_path: Path) -> None:
    path = _write(tmp_path / "forms.yaml", [_form(), _form()])
    with pytest.raises(FormCapabilityCatalogError):
        load_form_capabilities(path, today=TODAY)
    held = load_form_capabilities(
        _write(path, [_form(access_decision="hold")]), today=TODAY,
    )
    assert held.resolve(TARGET, "lever/1", "sha256:fixture").capability_id is None


def test_operator_catalog_path_defaults_to_packaged_and_accepts_explicit_override(
    tmp_path: Path,
) -> None:
    default = BrowserSubmissionSettings.from_env({})
    assert default.form_catalog_path.name == "ats_approved_forms.yaml"
    custom = tmp_path / "reviewed.yaml"
    configured = BrowserSubmissionSettings.from_env({
        "JOBSEARCH_ATS_APPROVED_FORMS_PATH": str(custom),
    })
    assert configured.form_catalog_path == custom
