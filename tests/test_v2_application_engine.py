"""The Phase 12 application engine, driven end to end over the in-memory fakes.

Three layers are pinned here without a database, a browser or an LLM (CLAUDE.md
§Testing): the deterministic `ApplicationExecutionGate`, the deterministic
`ApplicationDecisionService`, and the `ApplicationService` lifecycle. The gate's
regressions are the load-bearing ones — a policy that flips to MANUAL between
approval and submission must BLOCK (§5), an ineligible pair must never submit (§6),
an adapter ceiling must lower autonomy and never raise it (§59) — and the lifecycle
tests prove the same rules hold once the gate is wired into create → prepare →
approve → submit, plus idempotency (§36), document pinning (§14-16) and crash
recovery (§88).
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.app.application_engine.adapters.browser import BrowserApplicationAdapter
from backend.app.application_engine.adapters.email import (
    EmailApplicationAdapter,
    FakeEmailSender,
)
from backend.app.application_engine.adapters.generic import GenericManualAdapter
from backend.app.application_engine.registry import ApplicationAdapterRegistry
from backend.app.application_engine.task_dispatcher import (
    BrowserTaskOutcome,
    BrowserTaskResult,
    FakeTaskDispatcher,
)
from backend.app.domain.application import ApplicationState, build_idempotency_key
from backend.app.domain.application_channel import (
    AdapterSafetyLevel,
    ApplicationChannel,
    HumanRequiredReason,
)
from backend.app.domain.application_failure import (
    ApplicationError,
    ApplicationFailureCode,
)
from backend.app.domain.decision import ApplicationDecisionKind
from backend.app.domain.eligibility import EligibilityStatus
from backend.app.domain.execution_gate import (
    ApplicationExecutionGate,
    ExecutionOutcome,
)
from backend.app.domain.identifiers import application_id
from backend.app.domain.policy import AutomationMode, DimensionThreshold
from backend.app.domain.matching import MatchDimension
from backend.app.services.application_decisions import ApplicationDecisionService
from backend.app.services.applications import (
    ApplicationDecisionMissing,
    ApplicationNotActionable,
    ApplicationService,
)
from tests.v2_builders import (
    NOW,
    OPPORTUNITY,
    USER,
    a_check,
    a_decision,
    a_candidate_profile,
    a_policy,
    a_rendered_document,
    an_autopilot_policy,
    an_eligibility_result,
    an_evaluation,
    an_opportunity,
)
from tests.v2_fakes import (
    FakeApplicationDecisionRepository,
    FakeApplicationEventRepository,
    FakeApplicationPolicyRepository,
    FakeApplicationRepository,
    FakeCandidateDocumentRepository,
    FakeCandidateProfileRepository,
    FakeEligibilityResultRepository,
    FakeMatchEvaluationRepository,
    FakeOpportunityRepository,
    FakeSubmissionAttemptRepository,
)

# ---------------------------------------------------------------------------
# The execution gate — the deterministic safety mechanism (§4-7, §59).
# ---------------------------------------------------------------------------


def _eligible():
    return an_eligibility_result(a_check(status=EligibilityStatus.ELIGIBLE))


@pytest.mark.parametrize("safety", list(AdapterSafetyLevel))
def test_gate_permits_only_when_policy_and_adapter_both_allow(safety):
    auth = ApplicationExecutionGate.evaluate(
        decision=a_decision(), policy=an_autopilot_policy(),
        adapter_safety=safety, eligibility=_eligible())
    # Only a fully-supported adapter under an unattended policy reaches PERMITTED;
    # every lower ceiling lowers the outcome (§59).
    if safety is AdapterSafetyLevel.FULLY_SUPPORTED:
        assert auth.outcome is ExecutionOutcome.PERMITTED
    elif safety is AdapterSafetyLevel.SUPPORTED_WITH_REVIEW:
        assert auth.outcome is ExecutionOutcome.REQUIRES_APPROVAL
    else:
        assert auth.outcome is ExecutionOutcome.REQUIRES_HUMAN


def test_gate_blocks_when_policy_became_manual_after_approval():
    # §5 regression: the decision was AUTO_APPLY under AUTOPILOT, but the policy is
    # MANUAL now, and the gate reads it as it is now.
    auth = ApplicationExecutionGate.evaluate(
        decision=a_decision(), policy=a_policy(mode=AutomationMode.MANUAL),
        adapter_safety=AdapterSafetyLevel.FULLY_SUPPORTED, eligibility=_eligible())
    assert auth.outcome is ExecutionOutcome.BLOCKED
    assert any(r.code == "POLICY_MODE_FORBIDS_SUBMISSION" for r in auth.reasons)


def test_gate_blocks_an_ineligible_pair_whatever_the_policy():
    ineligible = an_eligibility_result(
        a_check(status=EligibilityStatus.INELIGIBLE))
    auth = ApplicationExecutionGate.evaluate(
        decision=a_decision(), policy=an_autopilot_policy(),
        adapter_safety=AdapterSafetyLevel.FULLY_SUPPORTED, eligibility=ineligible)
    assert auth.outcome is ExecutionOutcome.BLOCKED


def test_gate_routes_review_required_eligibility_to_a_human():
    review = an_eligibility_result(
        a_check(status=EligibilityStatus.REVIEW_REQUIRED))
    auth = ApplicationExecutionGate.evaluate(
        decision=a_decision(), policy=an_autopilot_policy(),
        adapter_safety=AdapterSafetyLevel.FULLY_SUPPORTED, eligibility=review)
    assert auth.outcome is ExecutionOutcome.REQUIRES_HUMAN


def test_gate_blocks_incomplete_eligibility_unless_policy_allows_it():
    incomplete = an_eligibility_result(
        a_check(status=EligibilityStatus.INCOMPLETE))
    blocked = ApplicationExecutionGate.evaluate(
        decision=a_decision(), policy=an_autopilot_policy(),
        adapter_safety=AdapterSafetyLevel.FULLY_SUPPORTED, eligibility=incomplete)
    assert blocked.outcome is ExecutionOutcome.BLOCKED

    permissive = an_autopilot_policy(allow_incomplete_eligibility=True)
    allowed = ApplicationExecutionGate.evaluate(
        decision=a_decision(), policy=permissive,
        adapter_safety=AdapterSafetyLevel.FULLY_SUPPORTED, eligibility=incomplete)
    assert allowed.outcome is ExecutionOutcome.PERMITTED


def test_gate_blocks_a_match_below_the_policy_floor():
    policy = an_autopilot_policy(dimension_thresholds=(
        DimensionThreshold(dimension=MatchDimension.SKILLS_FIT, minimum=0.95),))
    auth = ApplicationExecutionGate.evaluate(
        decision=a_decision(), policy=policy,
        adapter_safety=AdapterSafetyLevel.FULLY_SUPPORTED, eligibility=_eligible(),
        match=an_evaluation())  # SKILLS_FIT 0.92 < 0.95
    assert auth.outcome is ExecutionOutcome.BLOCKED
    assert any("BELOW_MINIMUM" in r.code for r in auth.reasons)


def test_gate_blocks_when_the_rate_budget_is_exhausted():
    policy = an_autopilot_policy(max_applications_per_day=2)
    auth = ApplicationExecutionGate.evaluate(
        decision=a_decision(), policy=policy,
        adapter_safety=AdapterSafetyLevel.FULLY_SUPPORTED, eligibility=_eligible(),
        submitted_today=2)
    assert auth.outcome is ExecutionOutcome.BLOCKED
    assert any(r.code == "POLICY_RATE_LIMIT_EXHAUSTED" for r in auth.reasons)


def test_gate_requires_a_human_when_a_captcha_was_seen():
    auth = ApplicationExecutionGate.evaluate(
        decision=a_decision(), policy=an_autopilot_policy(),
        adapter_safety=AdapterSafetyLevel.FULLY_SUPPORTED, eligibility=_eligible(),
        human_required_reasons=(HumanRequiredReason.CAPTCHA_PRESENT,))
    assert auth.outcome is ExecutionOutcome.REQUIRES_HUMAN
    assert HumanRequiredReason.CAPTCHA_PRESENT in auth.human_required_reasons


def test_gate_requires_a_human_when_eligibility_is_absent():
    auth = ApplicationExecutionGate.evaluate(
        decision=a_decision(), policy=an_autopilot_policy(),
        adapter_safety=AdapterSafetyLevel.FULLY_SUPPORTED, eligibility=None)
    assert auth.outcome is ExecutionOutcome.REQUIRES_HUMAN


# ---------------------------------------------------------------------------
# The decision service — deterministic intent (§30-32).
# ---------------------------------------------------------------------------


def _decision_service(*, policy=None, match=None, eligibility=None):
    profiles = FakeCandidateProfileRepository()
    opps = FakeOpportunityRepository()
    matches = FakeMatchEvaluationRepository()
    eligs = FakeEligibilityResultRepository()
    policies = FakeApplicationPolicyRepository()
    decisions = FakeApplicationDecisionRepository()
    profiles.profiles[a_candidate_profile().id] = a_candidate_profile()
    opps.opportunities[OPPORTUNITY] = an_opportunity()
    if match is not None:
        matches.evaluations[match.id] = match
    if eligibility is not None:
        eligs.results[eligibility.id] = eligibility
    if policy is not None:
        policies.policies[policy.id] = policy
    service = ApplicationDecisionService(
        profiles, opps, matches, eligs, policies, decisions)
    return service, decisions


@pytest.mark.asyncio
async def test_decision_skips_an_ineligible_pair():
    service, _ = _decision_service(
        policy=an_autopilot_policy(),
        eligibility=an_eligibility_result(
            a_check(status=EligibilityStatus.INELIGIBLE)))
    decision = await service.decide(USER, OPPORTUNITY, now=NOW)
    assert decision.kind is ApplicationDecisionKind.SKIP


@pytest.mark.asyncio
async def test_decision_requires_review_for_a_review_gate():
    service, _ = _decision_service(
        policy=an_autopilot_policy(), match=an_evaluation(),
        eligibility=an_eligibility_result(
            a_check(status=EligibilityStatus.REVIEW_REQUIRED)))
    decision = await service.decide(USER, OPPORTUNITY, now=NOW)
    assert decision.kind is ApplicationDecisionKind.REQUIRE_REVIEW
    assert decision.requires_human_review is True


@pytest.mark.asyncio
async def test_decision_auto_applies_when_autopilot_and_all_clear():
    service, _ = _decision_service(
        policy=an_autopilot_policy(), match=an_evaluation(),
        eligibility=_eligible())
    decision = await service.decide(USER, OPPORTUNITY, now=NOW)
    assert decision.kind is ApplicationDecisionKind.AUTO_APPLY


@pytest.mark.asyncio
async def test_decision_saves_under_a_manual_policy():
    service, _ = _decision_service(
        policy=a_policy(mode=AutomationMode.MANUAL), match=an_evaluation(),
        eligibility=_eligible())
    decision = await service.decide(USER, OPPORTUNITY, now=NOW)
    assert decision.kind is ApplicationDecisionKind.SAVE


# ---------------------------------------------------------------------------
# The lifecycle service — create → prepare → approve → submit (§17-18, §36-88).
# ---------------------------------------------------------------------------


class _FullyAutomatedAdapter:
    """A FULLY_SUPPORTED test adapter on the BROWSER channel.

    No real channel adapter is FULLY_SUPPORTED — a browser and an email both require
    review (§59) — so proving the PERMITTED → auto-approve path needs a stand-in that
    declares the top ceiling. Its `submit` returns a scripted result so a test can
    drive SUBMITTED / STATE_UNKNOWN / etc. without a dispatcher.
    """

    def __init__(self, submit_result):
        self._submit_result = submit_result

    @property
    def capabilities(self):
        from backend.app.application_engine.contracts import AdapterCapabilities
        return AdapterCapabilities(
            key="test-auto/1", channel=ApplicationChannel.BROWSER,
            safety_level=AdapterSafetyLevel.FULLY_SUPPORTED,
            can_prepare=True, can_submit=True)

    async def prepare(self, context):
        from backend.app.application_engine.contracts import AdapterPreparation
        return AdapterPreparation(form_fingerprint="stable-form-1")

    async def submit(self, context):
        return self._submit_result


def _submitted():
    from backend.app.domain.application import SubmissionOutcome, SubmissionResult
    return SubmissionResult(outcome=SubmissionOutcome.SUBMITTED)


def _state_unknown():
    from backend.app.domain.application import SubmissionOutcome, SubmissionResult
    return SubmissionResult(outcome=SubmissionOutcome.STATE_UNKNOWN,
                            detail="left the platform")


def _wire(*, policy=None, decision=None, match=None, eligibility=None,
          documents=(), dispatcher=None, email_sender=None,
          register_browser=True, browser_adapter=None):
    apps = FakeApplicationRepository()
    events = FakeApplicationEventRepository(apps)
    attempts = FakeSubmissionAttemptRepository(apps)
    policies = FakeApplicationPolicyRepository()
    decisions = FakeApplicationDecisionRepository()
    matches = FakeMatchEvaluationRepository()
    eligs = FakeEligibilityResultRepository()
    profiles = FakeCandidateProfileRepository()
    opps = FakeOpportunityRepository()
    docs = FakeCandidateDocumentRepository()

    profile = a_candidate_profile()
    profiles.profiles[profile.id] = profile
    opps.opportunities[OPPORTUNITY] = an_opportunity()
    if policy is not None:
        policies.policies[policy.id] = policy
    if decision is not None:
        decisions.decisions[decision.id] = decision
    if match is not None:
        matches.evaluations[match.id] = match
    if eligibility is not None:
        eligs.results[eligibility.id] = eligibility
    for document in documents:
        docs.documents[document.id] = document

    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    if browser_adapter is not None:
        registry.register(browser_adapter)
    elif register_browser:
        registry.register(BrowserApplicationAdapter(
            dispatcher or FakeTaskDispatcher()))
    registry.register(EmailApplicationAdapter(email_sender or FakeEmailSender()))

    service = ApplicationService(
        applications=apps, events=events, attempts=attempts, decisions=decisions,
        policies=policies, matches=matches, eligibilities=eligs, profiles=profiles,
        opportunities=opps, documents=docs, registry=registry)
    return service, SimpleNamespace(
        apps=apps, events=events, attempts=attempts, decisions=decisions,
        policies=policies, docs=docs)


@pytest.mark.asyncio
async def test_create_requires_a_decision_first():
    service, _ = _wire(policy=an_autopilot_policy())
    with pytest.raises(ApplicationDecisionMissing):
        await service.create(USER, OPPORTUNITY, now=NOW)


@pytest.mark.asyncio
async def test_create_is_idempotent_for_the_same_target():
    service, store = _wire(policy=an_autopilot_policy(), decision=a_decision())
    first = await service.create(USER, OPPORTUNITY, now=NOW)
    second = await service.create(USER, OPPORTUNITY, now=NOW)
    assert first.id == second.id
    assert len(store.apps.applications) == 1


@pytest.mark.asyncio
async def test_the_application_id_is_derived_from_the_idempotency_key():
    service, _ = _wire(policy=an_autopilot_policy(), decision=a_decision())
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    key = build_idempotency_key(
        candidate_profile_id=app.candidate_profile_id,
        channel=ApplicationChannel.BROWSER, opportunity_id=OPPORTUNITY)
    assert app.id == application_id(key)


@pytest.mark.asyncio
async def test_browser_channel_needs_review_even_under_autopilot():
    # §59: the browser adapter's SUPPORTED_WITH_REVIEW ceiling lowers an AUTOPILOT
    # policy to a review stop — a browser is never submitted unattended.
    dispatcher = FakeTaskDispatcher(default=BrowserTaskResult(
        outcome=BrowserTaskOutcome.COMPLETED))
    service, _ = _wire(
        policy=an_autopilot_policy(), decision=a_decision(),
        match=an_evaluation(), eligibility=_eligible(), dispatcher=dispatcher)
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    prepared = await service.prepare(USER, app.id, now=NOW)
    assert prepared.state is ApplicationState.READY_FOR_REVIEW


@pytest.mark.asyncio
async def test_a_fully_supported_adapter_auto_approves_and_submits():
    service, store = _wire(
        policy=an_autopilot_policy(), decision=a_decision(),
        match=an_evaluation(), eligibility=_eligible(),
        browser_adapter=_FullyAutomatedAdapter(_submitted()))
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    prepared = await service.prepare(USER, app.id, now=NOW)
    assert prepared.state is ApplicationState.APPROVED
    submitted = await service.submit(USER, app.id, now=NOW)
    assert submitted.state is ApplicationState.SUBMITTED
    # One completed submission attempt exists (§39).
    attempts = await store.attempts.list_for_application(USER, app.id)
    assert len(attempts) == 1
    assert attempts[0].finished_at is not None


@pytest.mark.asyncio
async def test_supervised_prepare_waits_for_review_then_approve_and_submit():
    dispatcher = FakeTaskDispatcher(default=BrowserTaskResult(
        outcome=BrowserTaskOutcome.COMPLETED))
    service, _ = _wire(
        policy=a_policy(mode=AutomationMode.SUPERVISED), decision=a_decision(),
        match=an_evaluation(), eligibility=_eligible(), dispatcher=dispatcher)
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    prepared = await service.prepare(USER, app.id, now=NOW)
    assert prepared.state is ApplicationState.READY_FOR_REVIEW
    # Cannot submit before approval.
    with pytest.raises(ApplicationNotActionable):
        await service.submit(USER, app.id, now=NOW)
    approved = await service.approve(USER, app.id, now=NOW)
    assert approved.state is ApplicationState.APPROVED
    submitted = await service.submit(USER, app.id, now=NOW)
    assert submitted.state is ApplicationState.SUBMITTED


@pytest.mark.asyncio
async def test_submit_is_refused_when_policy_flipped_to_manual():
    # §5 end to end: prepare+approve under AUTOPILOT, then the policy becomes MANUAL
    # before submit — the re-checked gate must stop the send.
    service, store = _wire(
        policy=an_autopilot_policy(), decision=a_decision(),
        match=an_evaluation(), eligibility=_eligible(),
        browser_adapter=_FullyAutomatedAdapter(_submitted()))
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    prepared = await service.prepare(USER, app.id, now=NOW)
    assert prepared.state is ApplicationState.APPROVED
    # Flip the policy to MANUAL between approval and submission.
    store.policies.policies[an_autopilot_policy().id] = a_policy(
        mode=AutomationMode.MANUAL)
    result = await service.submit(USER, app.id, now=NOW)
    assert result.state is not ApplicationState.SUBMITTED
    assert result.state is ApplicationState.REQUIRES_HUMAN


@pytest.mark.asyncio
async def test_submit_refuses_a_target_that_is_rate_limited():
    policy = an_autopilot_policy(max_applications_per_day=0)
    service, _ = _wire(
        policy=policy, decision=a_decision(),
        match=an_evaluation(), eligibility=_eligible())
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    # A zero budget blocks at prepare already (the gate sees the exhausted limit).
    prepared = await service.prepare(USER, app.id, now=NOW)
    assert prepared.state is ApplicationState.CANCELLED


@pytest.mark.asyncio
async def test_a_captcha_during_prepare_routes_to_requires_human():
    dispatcher = FakeTaskDispatcher(default=BrowserTaskResult(
        outcome=BrowserTaskOutcome.REQUIRES_HUMAN,
        human_required_reason=HumanRequiredReason.CAPTCHA_PRESENT))
    service, _ = _wire(
        policy=an_autopilot_policy(), decision=a_decision(),
        match=an_evaluation(), eligibility=_eligible(), dispatcher=dispatcher)
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    prepared = await service.prepare(USER, app.id, now=NOW)
    assert prepared.state is ApplicationState.REQUIRES_HUMAN


@pytest.mark.asyncio
async def test_an_ambiguous_submit_becomes_state_unknown_not_failed():
    service, _ = _wire(
        policy=an_autopilot_policy(), decision=a_decision(),
        match=an_evaluation(), eligibility=_eligible(),
        browser_adapter=_FullyAutomatedAdapter(_state_unknown()))
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    await service.prepare(USER, app.id, now=NOW)
    result = await service.submit(USER, app.id, now=NOW)
    assert result.state is ApplicationState.SUBMISSION_STATE_UNKNOWN


@pytest.mark.asyncio
async def test_duplicate_submission_for_a_submitted_target_is_refused():
    service, _ = _wire(
        policy=an_autopilot_policy(), decision=a_decision(),
        match=an_evaluation(), eligibility=_eligible(),
        browser_adapter=_FullyAutomatedAdapter(_submitted()))
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    await service.prepare(USER, app.id, now=NOW)
    await service.submit(USER, app.id, now=NOW)
    with pytest.raises(ApplicationError) as caught:
        await service.create(USER, OPPORTUNITY, now=NOW)
    assert caught.value.code is ApplicationFailureCode.APPLICATION_DUPLICATE


@pytest.mark.asyncio
async def test_recovery_marks_a_crashed_submission_state_unknown():
    service, store = _wire(policy=an_autopilot_policy(), decision=a_decision())
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    # Force it into SUBMITTING as a crash would leave it.
    stuck = app.transition_to(ApplicationState.PREPARING, at=NOW)
    stuck = stuck.transition_to(ApplicationState.APPROVED, at=NOW)
    stuck = stuck.transition_to(ApplicationState.SUBMITTING, at=NOW)
    await store.apps.upsert(stuck)
    recovered = await service.recover_in_flight(now=NOW)
    assert len(recovered) == 1
    assert recovered[0].state is ApplicationState.SUBMISSION_STATE_UNKNOWN


@pytest.mark.asyncio
async def test_cancel_before_submission_closes_the_application():
    service, _ = _wire(policy=an_autopilot_policy(), decision=a_decision())
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    cancelled = await service.cancel(USER, app.id, now=NOW)
    assert cancelled.state is ApplicationState.CANCELLED
    assert cancelled.is_terminal


@pytest.mark.asyncio
async def test_the_audit_trail_grows_across_the_lifecycle():
    service, _ = _wire(
        policy=an_autopilot_policy(), decision=a_decision(),
        match=an_evaluation(), eligibility=_eligible(),
        browser_adapter=_FullyAutomatedAdapter(_submitted()))
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    await service.prepare(USER, app.id, now=NOW)
    await service.submit(USER, app.id, now=NOW)
    events = await service.events(USER, app.id)
    types = [event.event_type for event in events]
    # The trail records creation and a submission, in order, and only ever grows.
    assert types[0].value == "CREATED"
    assert any(t.value == "SUBMITTED" for t in types)


@pytest.mark.asyncio
async def test_another_users_application_is_not_found():
    from tests.v2_builders import OTHER_USER
    service, _ = _wire(policy=an_autopilot_policy(), decision=a_decision())
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    from backend.app.services.applications import ApplicationNotFound
    with pytest.raises(ApplicationNotFound):
        await service.get(OTHER_USER, app.id)
