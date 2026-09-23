# tests/test_v2_eligibility.py
"""`EligibilityResult` — the binary half, kept apart from compatibility.

Four properties are pinned here, all of them from the phase order rather than
from taste: a verdict that is not ELIGIBLE has to explain itself, an LLM may
never be the source of truth for a gate, an operator-maintained Country Pack
rule may never refuse an application on its own (only a VERIFIED rule can), and
the aggregate verdict is derived from the checks instead of being stored beside
them.
"""
import pytest
from pydantic import ValidationError

from backend.app.domain.eligibility import (
    DeterminationSource,
    EligibilityCheck,
    EligibilityRequirement,
    EligibilityResult,
    EligibilityStatus,
    RuleAuthority,
)
from tests.v2_builders import (
    NOW,
    OPPORTUNITY,
    PROFILE,
    USER,
    a_check,
    a_reason,
    an_eligibility_result,
)


def test_a_passing_gate_needs_no_explanation():
    check = a_check(status=EligibilityStatus.ELIGIBLE)
    assert check.reasons == ()
    assert check.status is EligibilityStatus.ELIGIBLE


@pytest.mark.parametrize("status",
                         [EligibilityStatus.INCOMPLETE,
                          EligibilityStatus.REVIEW_REQUIRED,
                          EligibilityStatus.INELIGIBLE])
def test_a_gate_that_does_not_pass_must_say_why(status):
    """§13: the candidate is entitled to know which gate closed."""
    with pytest.raises(ValidationError) as failure:
        EligibilityCheck(requirement=EligibilityRequirement.WORK_AUTHORIZATION,
                         status=status,
                         determined_by=DeterminationSource.DETERMINISTIC_RULE)
    assert "must carry at least one" in str(failure.value)


@pytest.mark.parametrize("status",
                         [EligibilityStatus.ELIGIBLE, EligibilityStatus.INELIGIBLE])
def test_a_model_may_flag_a_gate_but_never_close_it(status):
    """CLAUDE.md: no LLM is the source of truth for a deterministic rule."""
    with pytest.raises(ValidationError) as failure:
        EligibilityCheck(requirement=EligibilityRequirement.DRIVING_LICENCE,
                         status=status,
                         determined_by=DeterminationSource.LLM_EXTRACTION,
                         reasons=(a_reason(code="POSTING_MENTIONS_A_LICENCE"),))
    assert "may only be INCOMPLETE" in str(failure.value)


def test_a_model_may_propose_that_a_gate_exists():
    """The sanctioned use: extraction raises the question, a rule answers it."""
    check = EligibilityCheck(
        requirement=EligibilityRequirement.DRIVING_LICENCE,
        status=EligibilityStatus.INCOMPLETE,
        determined_by=DeterminationSource.LLM_EXTRACTION,
        reasons=(a_reason(code="POSTING_MENTIONS_A_LICENCE"),))
    assert check.status is EligibilityStatus.INCOMPLETE


def test_a_result_needs_at_least_one_evaluated_gate():
    """An empty result would read as vacuously eligible — i.e. as a clean pass."""
    with pytest.raises(ValidationError) as failure:
        an_eligibility_result(checks=())
    assert "checks" in str(failure.value)


def test_the_verdict_is_derived_not_stored():
    """A stored aggregate can drift out of step with the checks it summarizes."""
    assert "status" not in EligibilityResult.model_fields
    result = an_eligibility_result()
    with pytest.raises(ValidationError):
        result.status = EligibilityStatus.INELIGIBLE


def test_all_gates_open_means_eligible():
    result = an_eligibility_result(
        a_check(EligibilityRequirement.WORK_AUTHORIZATION),
        a_check(EligibilityRequirement.LANGUAGE_MINIMUM),
    )
    assert result.status is EligibilityStatus.ELIGIBLE
    assert result.is_blocking is False
    assert result.failed_checks() == ()
    assert result.unresolved_checks() == ()


def test_one_unknown_gate_makes_the_whole_result_incomplete():
    result = an_eligibility_result(
        a_check(EligibilityRequirement.WORK_AUTHORIZATION),
        a_check(EligibilityRequirement.MINIMUM_AGE,
                status=EligibilityStatus.INCOMPLETE),
    )
    assert result.status is EligibilityStatus.INCOMPLETE
    # Not blocking: it routes to human review, it does not refuse.
    assert result.is_blocking is False
    assert len(result.unresolved_checks()) == 1
    assert result.failed_checks() == ()


def test_one_closed_gate_closes_the_result():
    result = an_eligibility_result(
        a_check(EligibilityRequirement.WORK_AUTHORIZATION),
        a_check(EligibilityRequirement.MINIMUM_AGE,
                status=EligibilityStatus.INCOMPLETE),
        a_check(EligibilityRequirement.PERMIT_HOURS_CAP,
                status=EligibilityStatus.INELIGIBLE),
    )
    assert result.status is EligibilityStatus.INELIGIBLE
    assert result.is_blocking is True
    failed = result.failed_checks()
    assert [check.requirement for check in failed] == [
        EligibilityRequirement.PERMIT_HOURS_CAP]
    assert len(result.unresolved_checks()) == 1


def test_review_required_outranks_incomplete_but_does_not_block():
    """The four-state ordering: a flagged concern beats missing evidence."""
    result = an_eligibility_result(
        a_check(EligibilityRequirement.WORK_AUTHORIZATION),
        a_check(EligibilityRequirement.MINIMUM_AGE,
                status=EligibilityStatus.INCOMPLETE),
        a_check(EligibilityRequirement.PERMIT_HOURS_CAP,
                status=EligibilityStatus.REVIEW_REQUIRED,
                determined_by=DeterminationSource.COUNTRY_PACK_RULE),
    )
    assert result.status is EligibilityStatus.REVIEW_REQUIRED
    assert result.is_blocking is False
    assert [c.requirement for c in result.review_checks()] == [
        EligibilityRequirement.PERMIT_HOURS_CAP]
    assert len(result.unresolved_checks()) == 1
    assert result.failed_checks() == ()


def test_ineligible_outranks_review_required():
    """Worst-of: a definite refusal beats a request for review."""
    result = an_eligibility_result(
        a_check(EligibilityRequirement.PERMIT_HOURS_CAP,
                status=EligibilityStatus.REVIEW_REQUIRED,
                determined_by=DeterminationSource.COUNTRY_PACK_RULE),
        a_check(EligibilityRequirement.WORK_AUTHORIZATION,
                status=EligibilityStatus.INELIGIBLE,
                determined_by=DeterminationSource.CANDIDATE_DECLARATION),
    )
    assert result.status is EligibilityStatus.INELIGIBLE
    assert result.is_blocking is True


def test_the_same_requirement_may_be_checked_twice():
    """Two required languages produce two LANGUAGE_MINIMUM checks."""
    result = an_eligibility_result(
        a_check(EligibilityRequirement.LANGUAGE_MINIMUM),
        a_check(EligibilityRequirement.LANGUAGE_MINIMUM,
                status=EligibilityStatus.INELIGIBLE),
    )
    assert len(result.checks) == 2
    assert result.status is EligibilityStatus.INELIGIBLE


def test_a_permit_cap_is_an_eligibility_gate_not_a_schedule_preference():
    """A 15h/week permit against a 20h/week job is a gate, not a low SCHEDULE_FIT.

    But *who* said the cap decides whether it can refuse. A rule an operator
    maintains in a Country Pack (the default `OPERATOR_CONFIG`) may not close the
    gate on its own — the domain refuses to construct such a verdict — because a
    wrong YAML number must never become an automatic "you may not apply"
    (docs/COUNTRY_PACKS.md §Eligibility).
    """
    with pytest.raises(ValidationError) as failure:
        EligibilityCheck(
            requirement=EligibilityRequirement.PERMIT_HOURS_CAP,
            status=EligibilityStatus.INELIGIBLE,
            determined_by=DeterminationSource.COUNTRY_PACK_RULE,
            authority=RuleAuthority.OPERATOR_CONFIG,
            detail="permit caps paid work at 15h/week; the posting asks for 20h",
            reasons=(a_reason(code="PERMIT_HOURS_EXCEEDED"),))
    assert "only be INELIGIBLE when its authority is VERIFIED" in str(failure.value)


def test_an_unverified_country_pack_rule_may_raise_review_not_refuse():
    """The safe outcome the engine is expected to reach on operator config."""
    check = EligibilityCheck(
        requirement=EligibilityRequirement.PERMIT_HOURS_CAP,
        status=EligibilityStatus.REVIEW_REQUIRED,
        determined_by=DeterminationSource.COUNTRY_PACK_RULE,
        authority=RuleAuthority.OPERATOR_CONFIG,
        detail="permit caps paid work at 15h/week; a human must verify the rule",
        reasons=(a_reason(code="PERMIT_HOURS_CAP_NEEDS_REVIEW"),))
    result = an_eligibility_result(check)
    assert result.status is EligibilityStatus.REVIEW_REQUIRED
    assert result.is_blocking is False


def test_a_verified_rule_may_close_the_gate():
    """The escape hatch: an operator who has checked the law against its source
    marks the rule VERIFIED, and only then may it refuse."""
    check = EligibilityCheck(
        requirement=EligibilityRequirement.PERMIT_HOURS_CAP,
        status=EligibilityStatus.INELIGIBLE,
        determined_by=DeterminationSource.COUNTRY_PACK_RULE,
        authority=RuleAuthority.VERIFIED,
        detail="verified permit rule caps paid work at 15h/week; posting asks 20h",
        reasons=(a_reason(code="PERMIT_HOURS_EXCEEDED"),))
    result = an_eligibility_result(check)
    assert result.is_blocking
    assert "15h/week" in result.failed_checks()[0].detail


def test_a_candidate_declaration_may_refuse_without_verification():
    """A candidate stating their own status is not an unverified legal rule: the
    authority gate is about COUNTRY_PACK_RULE, so a declaration can still block."""
    check = EligibilityCheck(
        requirement=EligibilityRequirement.WORK_AUTHORIZATION,
        status=EligibilityStatus.INELIGIBLE,
        determined_by=DeterminationSource.CANDIDATE_DECLARATION,
        detail="candidate declares they are not authorised to work in this country",
        reasons=(a_reason(code="CANDIDATE_NOT_AUTHORISED"),))
    result = an_eligibility_result(check)
    assert result.is_blocking


def test_a_result_names_the_pair_it_describes():
    result = an_eligibility_result()
    assert (result.user_id, result.candidate_profile_id) == (USER, PROFILE)
    assert result.opportunity_id == OPPORTUNITY
    assert result.determined_at == NOW
