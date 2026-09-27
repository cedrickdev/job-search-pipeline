# tests/test_v2_strategy_change.py
"""The spine's only mutation, pinned: a proposal describes a change and never performs one.

`StrategyChangeProposal` is the one Phase 15 value with any path to the platform's state, so
these tests hold the guardrails that make it safe: a change must actually move a field to
exist, the sensitivity flag reads `True` for exactly the edits that loosen an application
brake (and `False` for every discovery-scope search edit), the proposal's queryable
`target`/`target_id` cannot contradict the typed change they summarize, the version
precondition and expiry are computable without a clock, and a proposal leaves `PROPOSED`
exactly once. The discriminated union is checked the way every closed vocabulary is — an
invented `kind` fails to parse rather than widening the grammar.
"""
from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.app.domain.base import Score
from backend.app.domain.identifiers import (
    new_career_recommendation_id,
    new_strategy_change_proposal_id,
    strategy_change_execution_id,
)
from backend.app.domain.opportunity import OpportunityType
from backend.app.domain.strategy_change import (
    STRATEGY_CHANGE_ADAPTER,
    FieldChange,
    SetApplicationVolumeChange,
    SetMinimumScoreChange,
    SetPolicyOpportunityTypesChange,
    SetSearchKeywordsChange,
    SetSearchOpportunityTypesChange,
    SetSearchRadiusChange,
    SetSearchSourcesChange,
    StrategyChangeExecution,
    StrategyChangeExecutionOutcome,
    StrategyChangeProposal,
    StrategyChangeProposalStatus,
    StrategyChangeTarget,
)
from tests.v2_builders import LATER, NOW, POLICY, SEARCH_PROFILE, USER

PROPOSAL = new_strategy_change_proposal_id()
EXPIRES = NOW + timedelta(days=7)

def a_radius_change(**overrides):
    fields = {"search_profile_id": SEARCH_PROFILE, "radius_km": 50.0, "before_radius_km": 25.0}
    fields.update(overrides)
    return SetSearchRadiusChange(**fields)


def a_proposal(change=None, **overrides):
    change = change or a_radius_change()
    fields = {
        "id": PROPOSAL,
        "user_id": USER,
        "target": change.target,
        "target_id": change.target_ref,
        "change": change,
        "target_version": NOW,
        "summary": "raise the radius so more of your best-converting slice surfaces",
        "created_at": NOW,
        "updated_at": NOW,
        "expires_at": EXPIRES,
    }
    fields.update(overrides)
    return StrategyChangeProposal(**fields)


# --- the typed changes: target routing, sensitivity, and the no-op guard ---------------


def test_a_change_that_moves_nothing_does_not_construct():
    """A proposal must propose something — a before that equals the after is not a change."""
    with pytest.raises(ValidationError):
        SetSearchSourcesChange(
            search_profile_id=SEARCH_PROFILE,
            source_keys=("indeed", "linkedin"),
            before_source_keys=("indeed", "linkedin"))


def test_every_search_edit_routes_to_the_search_profile_and_is_never_sensitive():
    """Discovery scope is safe: radius, keywords, sources and search types never gate."""
    changes = (
        a_radius_change(),
        SetSearchKeywordsChange(
            search_profile_id=SEARCH_PROFILE,
            title_keywords=("data engineer",), before_title_keywords=(),
            excluded_keywords=(), before_excluded_keywords=()),
        SetSearchSourcesChange(
            search_profile_id=SEARCH_PROFILE,
            source_keys=("indeed",), before_source_keys=()),
        SetSearchOpportunityTypesChange(
            search_profile_id=SEARCH_PROFILE,
            opportunity_types=(OpportunityType.FULL_TIME,), before_opportunity_types=()),
    )
    for change in changes:
        assert change.target is StrategyChangeTarget.SEARCH_PROFILE
        assert change.target_ref == SEARCH_PROFILE
        assert change.is_sensitive is False


def test_every_policy_edit_routes_to_the_policy():
    change = SetMinimumScoreChange(
        application_policy_id=POLICY,
        minimum_overall_score=Score(0.7), before_minimum_overall_score=Score(0.6))
    assert change.target is StrategyChangeTarget.APPLICATION_POLICY
    assert change.target_ref == POLICY


@pytest.mark.parametrize(("before", "after", "sensitive"), [
    ((), (OpportunityType.FULL_TIME,), False),            # restricting is safe
    ((OpportunityType.FULL_TIME,), (), True),             # opening to every type loosens
    ((OpportunityType.FULL_TIME,),
     (OpportunityType.FULL_TIME, OpportunityType.INTERNSHIP), True),  # adds a type
    ((OpportunityType.FULL_TIME, OpportunityType.INTERNSHIP),
     (OpportunityType.FULL_TIME,), False),                # dropping a type narrows
])
def test_widening_what_the_platform_may_apply_to_is_sensitive(before, after, sensitive):
    """The application allow-list is authority, not discovery — widening it must be flagged."""
    change = SetPolicyOpportunityTypesChange(
        application_policy_id=POLICY,
        allowed_opportunity_types=after, before_allowed_opportunity_types=before)
    assert change.is_sensitive is sensitive


@pytest.mark.parametrize(("before_day", "after_day", "sensitive"), [
    (5, 10, True),      # raising a cap loosens
    (5, None, True),    # lifting to unlimited loosens
    (10, 5, False),     # tightening is safe
    (None, 5, False),   # imposing a cap where there was none is safe
])
def test_raising_a_submission_cap_is_sensitive(before_day, after_day, sensitive):
    change = SetApplicationVolumeChange(
        application_policy_id=POLICY,
        max_applications_per_day=after_day, before_max_applications_per_day=before_day,
        max_applications_per_week=100, before_max_applications_per_week=100)
    assert change.is_sensitive is sensitive


def test_a_volume_change_keeps_the_daily_below_weekly_invariant():
    with pytest.raises(ValidationError):
        SetApplicationVolumeChange(
            application_policy_id=POLICY,
            max_applications_per_day=20, before_max_applications_per_day=5,
            max_applications_per_week=10, before_max_applications_per_week=10)


@pytest.mark.parametrize(("before", "after", "sensitive"), [
    (Score(0.6), Score(0.5), True),   # lowering the floor admits weaker matches
    (Score(0.6), None, True),         # removing the floor admits everything
    (Score(0.5), Score(0.6), False),  # raising the floor is safe
    (None, Score(0.6), False),        # imposing a floor is safe
])
def test_lowering_the_score_floor_is_sensitive(before, after, sensitive):
    change = SetMinimumScoreChange(
        application_policy_id=POLICY,
        minimum_overall_score=after, before_minimum_overall_score=before)
    assert change.is_sensitive is sensitive


def test_diff_shows_only_the_fields_that_move():
    """A keyword change that leaves the exclusions alone must not read as if it rewrote them."""
    change = SetSearchKeywordsChange(
        search_profile_id=SEARCH_PROFILE,
        title_keywords=("data engineer", "ml engineer"), before_title_keywords=("developer",),
        excluded_keywords=("senior",), before_excluded_keywords=("senior",))
    fields = {fc.field for fc in change.diff()}
    assert fields == {"title_keywords"}


def test_a_field_change_must_actually_move():
    with pytest.raises(ValidationError):
        FieldChange(field="radius_km", before="25 km", after="25 km")


# --- the proposal: agreement, precondition, expiry, lifecycle --------------------------


def test_a_proposal_target_must_agree_with_its_change():
    """The queryable columns cannot contradict the typed payload they summarize."""
    with pytest.raises(ValidationError):
        a_proposal(target=StrategyChangeTarget.APPLICATION_POLICY)


def test_a_proposal_target_id_must_be_the_id_the_change_acts_on():
    with pytest.raises(ValidationError):
        a_proposal(target_id=UUID("00000000-0000-4000-8000-0000000009ff"))


def test_a_proposal_must_expire_after_it_is_created():
    with pytest.raises(ValidationError):
        a_proposal(expires_at=NOW)


def test_sensitivity_is_derived_from_the_change():
    safe = a_proposal()
    assert safe.is_sensitive is False
    loosening = SetMinimumScoreChange(
        application_policy_id=POLICY,
        minimum_overall_score=Score(0.4), before_minimum_overall_score=Score(0.7))
    assert a_proposal(change=loosening).is_sensitive is True


def test_expiry_and_version_precondition_are_pure_functions_of_their_inputs():
    proposal = a_proposal()
    assert proposal.is_expired(as_of=EXPIRES) is True
    assert proposal.is_expired(as_of=NOW) is False
    assert proposal.matches_target_version(NOW) is True
    assert proposal.matches_target_version(LATER) is False


def test_a_proposal_carries_its_source_recommendation_for_the_audit_trail():
    recommendation_id = new_career_recommendation_id()
    proposal = a_proposal(source_recommendation_id=recommendation_id)
    assert proposal.source_recommendation_id == recommendation_id


def test_a_fresh_proposal_is_open_and_reaches_a_terminal_state_once():
    proposal = a_proposal()
    assert proposal.is_open is True
    executed = proposal.executed(at=LATER)
    assert executed.status is StrategyChangeProposalStatus.EXECUTED
    assert executed.updated_at == LATER
    assert executed.is_open is False


@pytest.mark.parametrize("transition", ["executed", "rejected", "failed", "dismissed",
                                        "expired"])
def test_a_terminal_proposal_refuses_every_further_transition(transition):
    """A change is applied at most once — a dismissed proposal cannot later execute."""
    dismissed = a_proposal().dismissed(at=NOW)
    with pytest.raises(ValueError, match="terminal"):
        getattr(dismissed, transition)(at=LATER)


def test_the_proposal_diff_delegates_to_its_change():
    proposal = a_proposal()
    assert proposal.diff() == a_radius_change().diff()


# --- the execution record and the union parser -----------------------------------------


def test_the_execution_record_reports_whether_the_change_was_written():
    execution = StrategyChangeExecution(
        id=strategy_change_execution_id(PROPOSAL),
        proposal_id=PROPOSAL,
        user_id=USER,
        outcome=StrategyChangeExecutionOutcome.SUCCEEDED,
        observed_target_version=NOW,
        created_at=LATER)
    assert execution.succeeded is True
    rejected = execution.model_copy(
        update={"outcome": StrategyChangeExecutionOutcome.REJECTED})
    assert rejected.succeeded is False


def test_the_union_parses_a_known_change_and_rejects_an_invented_kind():
    payload = {
        "kind": "SET_SEARCH_RADIUS",
        "search_profile_id": str(SEARCH_PROFILE),
        "radius_km": 40.0,
        "before_radius_km": 25.0,
    }
    parsed = STRATEGY_CHANGE_ADAPTER.validate_python(payload)
    assert isinstance(parsed, SetSearchRadiusChange)
    with pytest.raises(ValidationError):
        STRATEGY_CHANGE_ADAPTER.validate_python({**payload, "kind": "SET_AUTOPILOT_ON"})
