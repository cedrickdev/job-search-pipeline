# tests/test_v2_outcome.py
"""The outcome/execution firewall, pinned: a rejection is a fact about hiring, not a failed submit.

`ApplicationOutcome` records what the employer and candidate did after the platform applied, and
these tests hold the rules that keep that history trustworthy: there is no GHOSTED kind (silence
is derived, never stored), the natural key folds in `occurred_at` for repeatable rounds so a
second interview is a second row while a re-recorded offer collapses, the key must match the
fact it claims to identify, terminal kinds are recognized for maturity, and a correction
supersedes or a mistake retracts without ever deleting the row.
"""
from datetime import timedelta

import pytest
from pydantic import ValidationError

from backend.app.domain.identifiers import application_outcome_id
from backend.app.domain.outcome import (
    TERMINAL_OUTCOME_KINDS,
    ApplicationOutcome,
    OutcomeKind,
    OutcomeSource,
    OutcomeStatus,
    build_outcome_key,
    is_repeatable_outcome_kind,
)
from tests.v2_builders import APPLICATION, NOW, USER

LATER = NOW + timedelta(days=3)


def an_outcome(*, kind=OutcomeKind.INTERVIEW, occurred_at=NOW, supersedes_id=None, **overrides):
    key = build_outcome_key(kind=kind, occurred_at=occurred_at, supersedes_id=supersedes_id)
    fields = {
        "id": application_outcome_id(APPLICATION, key),
        "user_id": USER,
        "application_id": APPLICATION,
        "kind": kind,
        "outcome_key": key,
        "occurred_at": occurred_at,
        "recorded_at": occurred_at,
        "supersedes_id": supersedes_id,
    }
    fields.update(overrides)
    return ApplicationOutcome(**fields)


def test_there_is_no_ghosted_kind():
    """Silence is an absence the analytics layer derives, never a stored outcome (§5)."""
    assert "GHOSTED" not in OutcomeKind.__members__
    assert "NO_RESPONSE" not in OutcomeKind.__members__


def test_only_screens_assessments_and_interviews_repeat():
    for kind in (OutcomeKind.SCREEN, OutcomeKind.ASSESSMENT, OutcomeKind.INTERVIEW):
        assert is_repeatable_outcome_kind(kind) is True
    for kind in (OutcomeKind.ACKNOWLEDGED, OutcomeKind.OFFER_RECEIVED, OutcomeKind.REJECTED):
        assert is_repeatable_outcome_kind(kind) is False


def test_a_repeatable_round_folds_in_when_it_happened():
    """A second interview on another day is a second row; the same round twice is one."""
    first = build_outcome_key(kind=OutcomeKind.INTERVIEW, occurred_at=NOW)
    again = build_outcome_key(kind=OutcomeKind.INTERVIEW, occurred_at=NOW)
    other_day = build_outcome_key(kind=OutcomeKind.INTERVIEW, occurred_at=LATER)
    assert first == again
    assert first != other_day


def test_a_once_only_milestone_keys_on_its_kind_alone():
    """A re-recorded offer collapses onto one row regardless of the instant."""
    assert build_outcome_key(kind=OutcomeKind.OFFER_RECEIVED, occurred_at=NOW) \
        == build_outcome_key(kind=OutcomeKind.OFFER_RECEIVED, occurred_at=LATER)


def test_the_outcome_key_must_match_the_fact_it_identifies():
    with pytest.raises(ValidationError):
        an_outcome(outcome_key="INTERVIEW:not-the-real-key")


def test_terminal_kinds_conclude_the_process():
    assert OutcomeKind.REJECTED in TERMINAL_OUTCOME_KINDS
    assert an_outcome(kind=OutcomeKind.REJECTED).is_terminal is True
    assert an_outcome(kind=OutcomeKind.SCREEN).is_terminal is False


def test_the_default_source_and_status_are_the_cautious_common_case():
    outcome = an_outcome()
    assert outcome.source is OutcomeSource.MANUAL_USER
    assert outcome.status is OutcomeStatus.EFFECTIVE
    assert outcome.is_effective is True


def test_a_retraction_flips_status_without_deleting_the_row():
    outcome = an_outcome()
    retracted = outcome.retracted(at=LATER)
    assert retracted.status is OutcomeStatus.RETRACTED
    assert retracted.is_effective is False
    assert retracted.recorded_at == LATER
    assert retracted.id == outcome.id


def test_a_correction_supersedes_a_distinct_predecessor():
    original = an_outcome(kind=OutcomeKind.OFFER_RECEIVED)
    correction = an_outcome(kind=OutcomeKind.OFFER_RECEIVED, supersedes_id=original.id)
    assert correction.is_correction is True
    assert correction.id != original.id
    assert original.superseded(at=LATER).status is OutcomeStatus.SUPERSEDED


def test_an_outcome_cannot_supersede_itself():
    key = build_outcome_key(kind=OutcomeKind.OFFER_RECEIVED, occurred_at=NOW,
                            supersedes_id=None)
    outcome_id = application_outcome_id(APPLICATION, key)
    with pytest.raises(ValidationError):
        ApplicationOutcome(
            id=outcome_id, user_id=USER, application_id=APPLICATION,
            kind=OutcomeKind.OFFER_RECEIVED, outcome_key=key, occurred_at=NOW,
            recorded_at=NOW, supersedes_id=outcome_id)
