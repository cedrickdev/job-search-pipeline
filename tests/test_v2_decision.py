# tests/test_v2_decision.py
"""`ApplicationDecision` — the one place fit, legality and policy meet.

The case the phase order names explicitly is the first test in this file:
technical fit high, eligibility failed, decision do-not-apply, all three
representable at once. The rest of the file is about the gate that follows from
it — nothing that would contact an employer may be built on a closed verdict.
"""
import pytest
from pydantic import ValidationError

from backend.app.domain.decision import (
    SUBMITTING_DECISION_KINDS,
    ApplicationDecision,
    ApplicationDecisionKind,
)
from backend.app.domain.eligibility import EligibilityRequirement, EligibilityStatus
from backend.app.domain.matching import DimensionScore, MatchDimension
from tests.v2_builders import (
    COMPANY,
    DECISION,
    NOW,
    OPPORTUNITY,
    OTHER_OPPORTUNITY,
    OTHER_PROFILE,
    OTHER_USER,
    POLICY,
    PROFILE,
    USER,
    a_check,
    a_reason,
    an_eligibility_result,
    an_evaluation,
)


def a_decision(**overrides):
    fields = {
        "id": DECISION,
        "user_id": USER,
        "candidate_profile_id": PROFILE,
        "opportunity_id": OPPORTUNITY,
        "kind": ApplicationDecisionKind.SAVE,
        "reasons": (a_reason(code="WORTH_A_LOOK"),),
        "decided_at": NOW,
    }
    fields.update(overrides)
    return ApplicationDecision(**fields)


def test_high_fit_failed_eligibility_and_do_not_apply_coexist():
    """The Phase 1 target case, in one object.

    One number could not express it: 0.94 on skills and a closed permit gate have
    to survive side by side, or the decision cannot be explained to the candidate.
    """
    evaluation = an_evaluation(overall=0.94, dimensions=(
        DimensionScore(dimension=MatchDimension.SKILLS_FIT, score=0.97),
        DimensionScore(dimension=MatchDimension.EXPERIENCE_FIT, score=0.9),
    ))
    eligibility = an_eligibility_result(
        a_check(EligibilityRequirement.PERMIT_HOURS_CAP,
                status=EligibilityStatus.INELIGIBLE),
    )
    decision = a_decision(
        kind=ApplicationDecisionKind.SKIP,
        reasons=(a_reason(code="ELIGIBILITY_BLOCKED"),),
        match=evaluation,
        eligibility=eligibility,
        policy_id=POLICY,
    )
    assert decision.match.overall == 0.94
    assert decision.match.score_for(MatchDimension.SKILLS_FIT).score == 0.97
    assert decision.eligibility.status is EligibilityStatus.INELIGIBLE
    assert decision.eligibility.is_blocking
    assert decision.kind is ApplicationDecisionKind.SKIP
    assert decision.is_submission is False


@pytest.mark.parametrize("kind", sorted(SUBMITTING_DECISION_KINDS))
def test_nothing_submits_past_a_closed_gate(kind):
    blocked = an_eligibility_result(
        a_check(EligibilityRequirement.WORK_AUTHORIZATION,
                status=EligibilityStatus.INELIGIBLE))
    with pytest.raises(ValidationError) as failure:
        a_decision(kind=kind, company_id=COMPANY, match=an_evaluation(),
                   eligibility=blocked)
    message = str(failure.value)
    assert "eligibility is INELIGIBLE" in message
    assert "WORK_AUTHORIZATION" in message, "the closed gate must be named"


def test_the_submitting_kinds_are_named_once_and_only_these():
    """Phase 8 gates real submissions on this set, so it is pinned here."""
    assert SUBMITTING_DECISION_KINDS == frozenset({
        ApplicationDecisionKind.AUTO_APPLY,
        ApplicationDecisionKind.APPLY_AND_OUTREACH,
        ApplicationDecisionKind.SPONTANEOUS_APPLICATION,
    })


@pytest.mark.parametrize("kind,expected", [
    (ApplicationDecisionKind.SKIP, False),
    (ApplicationDecisionKind.SAVE, False),
    (ApplicationDecisionKind.PREPARE, False),
    (ApplicationDecisionKind.REQUIRE_REVIEW, False),
    (ApplicationDecisionKind.AUTO_APPLY, True),
    (ApplicationDecisionKind.APPLY_AND_OUTREACH, True),
    (ApplicationDecisionKind.SPONTANEOUS_APPLICATION, True),
])
def test_is_submission_answers_would_this_contact_an_employer(kind, expected):
    decision = a_decision(kind=kind, company_id=COMPANY,
                          requires_human_review=(
                              kind is ApplicationDecisionKind.REQUIRE_REVIEW))
    assert decision.is_submission is expected


@pytest.mark.parametrize("kind", [k for k in ApplicationDecisionKind
                                  if k is not ApplicationDecisionKind
                                  .SPONTANEOUS_APPLICATION])
def test_a_decision_about_a_posting_must_name_it(kind):
    with pytest.raises(ValidationError) as failure:
        a_decision(kind=kind, opportunity_id=None, company_id=COMPANY,
                   requires_human_review=(
                       kind is ApplicationDecisionKind.REQUIRE_REVIEW))
    assert "must name an opportunity_id" in str(failure.value)


def test_a_spontaneous_application_names_a_company_instead():
    """§6: approaching an employer that has no posting is a first-class case."""
    decision = a_decision(kind=ApplicationDecisionKind.SPONTANEOUS_APPLICATION,
                          opportunity_id=None, company_id=COMPANY)
    assert decision.opportunity_id is None
    assert decision.company_id == COMPANY
    assert decision.is_submission is True

    with pytest.raises(ValidationError) as failure:
        a_decision(kind=ApplicationDecisionKind.SPONTANEOUS_APPLICATION,
                   opportunity_id=None, company_id=None)
    assert "must name the company it targets" in str(failure.value)


def test_a_decision_must_explain_itself():
    """A SKIP with no reason is indistinguishable from a crash."""
    with pytest.raises(ValidationError) as failure:
        a_decision(kind=ApplicationDecisionKind.SKIP, reasons=())
    assert "reasons" in str(failure.value)


def test_require_review_must_agree_with_its_own_flag():
    with pytest.raises(ValidationError) as failure:
        a_decision(kind=ApplicationDecisionKind.REQUIRE_REVIEW,
                   requires_human_review=False)
    assert "must set requires_human_review" in str(failure.value)
    assert a_decision(kind=ApplicationDecisionKind.REQUIRE_REVIEW,
                      requires_human_review=True)


@pytest.mark.parametrize("kind", sorted(SUBMITTING_DECISION_KINDS))
def test_a_submission_cannot_also_be_waiting_on_a_human(kind):
    with pytest.raises(ValidationError) as failure:
        a_decision(kind=kind, company_id=COMPANY, requires_human_review=True)
    assert "decide REQUIRE_REVIEW instead" in str(failure.value)


def test_an_unresolved_gate_is_left_to_the_policy_to_decide():
    """`INCOMPLETE` is deliberately not refused here.

    Whether an unknown gate may be submitted through is
    `ApplicationPolicy.allow_incomplete_eligibility`; hard-coding it in the model
    would make that setting dead.
    """
    unresolved = an_eligibility_result(
        a_check(EligibilityRequirement.MINIMUM_AGE,
                status=EligibilityStatus.INCOMPLETE))
    decision = a_decision(kind=ApplicationDecisionKind.AUTO_APPLY,
                          eligibility=unresolved, match=an_evaluation())
    assert decision.eligibility.status is EligibilityStatus.INCOMPLETE
    assert decision.is_submission is True
    # And the cautious route is equally representable for the same evidence.
    reviewed = a_decision(kind=ApplicationDecisionKind.REQUIRE_REVIEW,
                          requires_human_review=True, eligibility=unresolved)
    assert reviewed.eligibility.unresolved_checks()[0].requirement is \
        EligibilityRequirement.MINIMUM_AGE


@pytest.mark.parametrize("overrides,expected", [
    ({"user_id": OTHER_USER}, "match belongs to a different candidate"),
    ({"candidate_profile_id": OTHER_PROFILE},
     "match belongs to a different candidate"),
    ({"opportunity_id": OTHER_OPPORTUNITY}, "match describes a different opportunity"),
])
def test_a_decision_refuses_someone_elses_match(overrides, expected):
    """Pairing one candidate's fit with another's decision is a leak, not a bug."""
    with pytest.raises(ValidationError) as failure:
        a_decision(match=an_evaluation(**overrides))
    assert expected in str(failure.value)


@pytest.mark.parametrize("overrides,expected", [
    ({"user_id": OTHER_USER}, "eligibility belongs to a different candidate"),
    ({"candidate_profile_id": OTHER_PROFILE},
     "eligibility belongs to a different candidate"),
    ({"opportunity_id": OTHER_OPPORTUNITY},
     "eligibility describes a different opportunity"),
])
def test_a_decision_refuses_someone_elses_eligibility(overrides, expected):
    with pytest.raises(ValidationError) as failure:
        a_decision(eligibility=an_eligibility_result(**overrides))
    assert expected in str(failure.value)


def test_an_early_decision_precedes_any_scoring():
    """SAVE on a freshly discovered posting is legitimate and carries no evidence."""
    decision = a_decision()
    assert decision.match is None
    assert decision.eligibility is None
    assert decision.confidence is None
    assert decision.requires_human_review is False
    assert decision.policy_id is None
    assert decision.decided_by is None


@pytest.mark.parametrize("confidence", [-0.01, 1.01, 90])
def test_confidence_is_a_unit_interval_score(confidence):
    with pytest.raises(ValidationError):
        a_decision(confidence=confidence)


def test_a_decision_records_who_decided_and_when():
    """Provenance, so an AUTO_APPLY can be audited after the fact."""
    decision = a_decision(kind=ApplicationDecisionKind.PREPARE,
                          decided_by="policy-engine-v1", confidence=0.8)
    assert decision.decided_by == "policy-engine-v1"
    assert decision.decided_at == NOW


def test_a_decision_is_a_record_of_intent_not_an_action():
    """Nothing here submits: no adapter, credential or session field exists."""
    for absent in ("session", "credentials", "cookies", "browser", "api_key",
                   "submitted_at", "screenshot_path"):
        assert absent not in ApplicationDecision.model_fields
