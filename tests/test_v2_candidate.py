# tests/test_v2_candidate.py
"""The candidate side: evidence, claims, authorization, availability.

The rule under test is the one CLAUDE.md states bluntly — never fabricate
candidate facts. In V1 that is a function `pipeline/tailor_io.py` calls; here it
is an invariant, so the tests are mostly about what *cannot* be constructed: a
claim with no evidence, a claim citing evidence the profile does not hold, or a
profile aggregating another user's records.
"""
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from backend.app.domain.candidate import (
    Availability,
    CandidateClaim,
    CandidateEvidence,
    CandidateProfile,
    ClaimType,
    EvidenceKind,
    WeeklyAvailabilitySlot,
    WorkAuthorization,
    WorkAuthorizationStatus,
)
from backend.app.domain.common import LanguageLevel, LanguageProficiency, Location, Weekday
from backend.app.domain.identifiers import (
    new_candidate_profile_id,
    new_claim_id,
    new_evidence_id,
    new_user_id,
)

RECORDED_AT = datetime(2026, 2, 1, 10, 0, tzinfo=UTC)
UPDATED_AT = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)


def an_evidence(user_id, **overrides):
    """A base-library CV bullet, in V2 form."""
    fields = {
        "id": new_evidence_id(),
        "user_id": user_id,
        "kind": EvidenceKind.CV_BULLET,
        "reference_key": "acme-checkout",
        "summary": "Rebuilt the checkout flow",
        "recorded_at": RECORDED_AT,
    }
    fields.update(overrides)
    return CandidateEvidence(**fields)


def a_claim(user_id, evidence_ids, **overrides):
    fields = {
        "id": new_claim_id(),
        "user_id": user_id,
        "claim_type": ClaimType.SKILL,
        "label": "Python",
        "evidence_ids": tuple(evidence_ids),
    }
    fields.update(overrides)
    return CandidateClaim(**fields)


def a_profile(user_id, **overrides):
    fields = {
        "id": new_candidate_profile_id(),
        "user_id": user_id,
        "display_name": "Candidate Under Test",
        "updated_at": UPDATED_AT,
    }
    fields.update(overrides)
    return CandidateProfile(**fields)


def test_a_fresh_profile_holds_nothing_yet():
    profile = a_profile(new_user_id())
    assert profile.evidence == ()
    assert profile.claims == ()
    assert profile.languages == ()
    assert profile.work_authorizations == ()
    assert profile.availability is None
    assert profile.base_location is None


def test_contact_details_are_deliberately_not_modelled():
    """PII with redaction rules of its own; nothing in Phase 1 needs it."""
    for absent in ("email", "phone", "address", "postal_address", "birth_date"):
        assert absent not in CandidateProfile.model_fields


def test_evidence_keeps_the_v1_bullet_id_as_its_reference_key():
    """The bridge Phase 10 needs: the base library already has stable ids."""
    evidence = an_evidence(new_user_id(), reference_key="acme-checkout")
    assert evidence.reference_key == "acme-checkout"
    assert evidence.kind is EvidenceKind.CV_BULLET


def test_no_evidence_kind_means_a_model_inferred_it():
    """`SELF_DECLARATION` is an attributed source, not a manufacturing licence."""
    values = {member.value for member in EvidenceKind}
    assert "SELF_DECLARATION" in values
    for forbidden in ("LLM_INFERENCE", "INFERRED", "ASSUMED", "GENERATED"):
        assert forbidden not in values


def test_an_evidence_validity_window_must_be_ordered():
    user_id = new_user_id()
    with pytest.raises(ValidationError) as failure:
        an_evidence(user_id, kind=EvidenceKind.CERTIFICATE,
                    issued_on=date(2026, 6, 1), valid_until=date(2025, 6, 1))
    assert "valid_until must not precede issued_on" in str(failure.value)
    same_day = an_evidence(user_id, issued_on=date(2026, 6, 1),
                           valid_until=date(2026, 6, 1))
    assert same_day.valid_until == date(2026, 6, 1)


def test_an_evidence_summary_must_say_something():
    with pytest.raises(ValidationError):
        an_evidence(new_user_id(), summary="   ")


def test_a_claim_without_evidence_cannot_be_constructed():
    """V1's truth gate, promoted from a function call to a type constraint."""
    with pytest.raises(ValidationError) as failure:
        a_claim(new_user_id(), ())
    assert "evidence_ids" in str(failure.value)


def test_a_claim_may_rest_on_several_records():
    user_id = new_user_id()
    first, second = an_evidence(user_id), an_evidence(user_id)
    claim = a_claim(user_id, (first.id, second.id))
    assert claim.evidence_ids == (first.id, second.id)


def test_a_claim_citing_evidence_the_profile_does_not_hold_is_refused():
    """This is V1's "unknown bullet id" rejection, as an invariant."""
    user_id = new_user_id()
    held = an_evidence(user_id)
    with pytest.raises(ValidationError) as failure:
        a_profile(user_id, evidence=(held,),
                  claims=(a_claim(user_id, (new_evidence_id(),)),))
    assert "cites evidence absent from the profile" in str(failure.value)


def test_a_supported_claim_resolves_to_its_records_in_profile_order():
    user_id = new_user_id()
    first = an_evidence(user_id, summary="Rebuilt the checkout flow")
    second = an_evidence(user_id, summary="Ran the migration")
    unrelated = an_evidence(user_id, summary="Won a hackathon")
    claim = a_claim(user_id, (second.id, first.id))
    profile = a_profile(user_id, evidence=(first, second, unrelated),
                        claims=(claim,))
    assert profile.evidence_for(claim) == (first, second)


def test_a_profile_refuses_another_users_records():
    """Cross-user leakage becomes a construction error, not a missed check."""
    owner, stranger = new_user_id(), new_user_id()
    with pytest.raises(ValidationError) as failure:
        a_profile(owner, evidence=(an_evidence(stranger),))
    assert "owned by another user" in str(failure.value)

    held = an_evidence(owner)
    with pytest.raises(ValidationError) as second_failure:
        a_profile(owner, evidence=(held,), claims=(a_claim(stranger, (held.id,)),))
    assert "owned by another user" in str(second_failure.value)


def test_a_profile_refuses_duplicate_identities():
    user_id = new_user_id()
    evidence = an_evidence(user_id)
    with pytest.raises(ValidationError) as failure:
        a_profile(user_id, evidence=(evidence, evidence))
    assert "evidence ids must be unique" in str(failure.value)

    claim = a_claim(user_id, (evidence.id,))
    with pytest.raises(ValidationError) as second_failure:
        a_profile(user_id, evidence=(evidence,), claims=(claim, claim))
    assert "claim ids must be unique" in str(second_failure.value)


def test_a_profile_states_each_language_and_country_once():
    user_id = new_user_id()
    with pytest.raises(ValidationError) as failure:
        a_profile(user_id, languages=(
            LanguageProficiency(language="fr", level=LanguageLevel.NATIVE),
            LanguageProficiency(language="fr", level=LanguageLevel.B2),
        ))
    assert "must not repeat a language" in str(failure.value)

    with pytest.raises(ValidationError) as second_failure:
        a_profile(user_id, work_authorizations=(
            WorkAuthorization(country="CH",
                              status=WorkAuthorizationStatus.CITIZEN),
            WorkAuthorization(country="CH",
                              status=WorkAuthorizationStatus.WORK_PERMIT_HELD),
        ))
    assert "must not repeat a country" in str(second_failure.value)


def test_an_unrecorded_country_is_unknown_not_refused():
    """`None` from `authorization_for` must route to INCOMPLETE, never to a no."""
    user_id = new_user_id()
    swiss = WorkAuthorization(country="CH",
                              status=WorkAuthorizationStatus.STUDENT_PERMIT_WITH_WORK_RIGHTS,
                              permit_label="Permis B étudiant",
                              permit_hours_cap=15.0)
    profile = a_profile(user_id, work_authorizations=(swiss,))
    assert profile.authorization_for("CH") is swiss
    assert profile.authorization_for("FR") is None
    assert WorkAuthorizationStatus.UNKNOWN in set(WorkAuthorizationStatus)


def test_a_permit_document_must_be_held_to_be_cited():
    user_id = new_user_id()
    with pytest.raises(ValidationError) as failure:
        a_profile(user_id, work_authorizations=(
            WorkAuthorization(country="CH",
                              status=WorkAuthorizationStatus.WORK_PERMIT_HELD,
                              evidence_ids=(new_evidence_id(),)),))
    assert "cites evidence absent from the profile" in str(failure.value)


@pytest.mark.parametrize("cap", [0.0, -1.0, 168.1, 200.0])
def test_an_impossible_permit_cap_is_refused(cap):
    with pytest.raises(ValidationError):
        WorkAuthorization(country="CH",
                          status=WorkAuthorizationStatus.WORK_PERMIT_HELD,
                          permit_hours_cap=cap)


def test_the_local_permit_word_lives_in_the_label_not_in_the_enum():
    authorization = WorkAuthorization(
        country="CH", status=WorkAuthorizationStatus.WORK_PERMIT_HELD,
        permit_label="Permis B", valid_until=date(2027, 8, 31))
    assert authorization.permit_label == "Permis B"
    assert "PERMIS_B" not in {member.value for member in WorkAuthorizationStatus}


@pytest.mark.parametrize("start,end", [(9, 9), (17, 9), (24, 24), (0, 0)])
def test_an_availability_slot_must_be_a_real_window(start, end):
    with pytest.raises(ValidationError):
        WeeklyAvailabilitySlot(weekday=Weekday.SATURDAY, start_hour=start,
                               end_hour=end)


def test_overlapping_slots_on_one_weekday_are_refused():
    with pytest.raises(ValidationError) as failure:
        Availability(weekly_slots=(
            WeeklyAvailabilitySlot(weekday=Weekday.SATURDAY, start_hour=9,
                                   end_hour=14),
            WeeklyAvailabilitySlot(weekday=Weekday.SATURDAY, start_hour=13,
                                   end_hour=18),
        ))
    assert "overlapping availability slots" in str(failure.value)


def test_adjacent_slots_and_repeated_windows_on_other_days_are_fine():
    availability = Availability(weekly_slots=(
        WeeklyAvailabilitySlot(weekday=Weekday.SATURDAY, start_hour=9, end_hour=12),
        WeeklyAvailabilitySlot(weekday=Weekday.SATURDAY, start_hour=12, end_hour=18),
        WeeklyAvailabilitySlot(weekday=Weekday.SUNDAY, start_hour=9, end_hour=12),
    ))
    assert len(availability.weekly_slots) == 3


def test_availability_bounds_must_be_coherent():
    with pytest.raises(ValidationError):
        Availability(earliest_start=date(2026, 9, 1), latest_end=date(2026, 6, 1))
    with pytest.raises(ValidationError):
        Availability(min_weekly_hours=20.0, max_weekly_hours=12.0)


def test_a_preference_is_not_a_legal_limit():
    """`Availability` feeds SCHEDULE_FIT; `permit_hours_cap` feeds eligibility."""
    user_id = new_user_id()
    profile = a_profile(
        user_id,
        base_location=Location(country="CH", city="Yverdon-les-Bains"),
        availability=Availability(min_weekly_hours=8.0, max_weekly_hours=20.0,
                                  notice_period_days=30),
        work_authorizations=(WorkAuthorization(
            country="CH",
            status=WorkAuthorizationStatus.STUDENT_PERMIT_WITH_WORK_RIGHTS,
            permit_hours_cap=15.0),),
    )
    assert profile.availability.max_weekly_hours == 20.0
    assert profile.authorization_for("CH").permit_hours_cap == 15.0
