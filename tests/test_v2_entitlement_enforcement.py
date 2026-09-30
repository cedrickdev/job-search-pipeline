# tests/test_v2_entitlement_enforcement.py
"""The commercial quota, enforced through the *real* domain actions, not just the meter (§4-9).

`test_v2_entitlement.py` and `test_v2_usage.py` pin the spine's pieces in isolation — the
entitlement algebra and the idempotent ledger. These tests close the loop the final corrective
asks for: that each of the six metered capabilities is actually refused *at its own service
boundary* once the plan's allowance is gone, with `QUOTA_EXCEEDED`, and that the domain
side-effect the quota guards never happens when it is.

Every test drives the genuine service over the in-memory fakes, with a `MeteringService` whose
only plan is a free tier granting a deliberately tiny ceiling. Two shapes of exhaustion are
exercised, both real: a *consume-then-block* run (do the action until the ceiling is reached,
then watch the next one refuse — proving the record path feeds back into authorize), and a
*pre-spent period* (the ledger already carries the ceiling, so the very next action refuses —
the shape a mid-period request meets). The gauge (active searches) is a live count rather than a
ledger sum, so it is exercised the first way and also shown to ignore paused searches.

The one rule underneath all six: the quota is a clause of the effective-permission AND that can
only *restrict*. It never authorizes an action a safety gate would have stopped — the
application test proves the gate is evaluated first and the quota only afterward.
"""
from types import SimpleNamespace

import pytest

from backend.app.application_engine.adapters.generic import GenericManualAdapter
from backend.app.application_engine.registry import ApplicationAdapterRegistry
from backend.app.billing.catalogue import FREE_PLAN_SLUG
from backend.app.billing.entitlements import EntitlementResolver
from backend.app.billing.errors import BillingError, BillingErrorCode
from backend.app.billing.metering import MeteringService
from backend.app.career.analytics import CareerAnalyticsService
from backend.app.career.recommendations import CareerRecommendationEngine
from backend.app.documents import (
    CandidateEvidenceGuard,
    DeterministicDocumentGenerator,
    LocalDocumentArtifactStore,
)
from backend.app.domain.application import ApplicationState
from backend.app.domain.documents import CandidateDocumentType
from backend.app.domain.entitlement import EntitlementKey
from backend.app.domain.interview import InterviewMode
from backend.app.domain.search import CountrySearchArea
from backend.app.domain.usage import UsageSourceType
from backend.app.interview.context import InterviewContextBuilder
from backend.app.interview.service import InterviewService
from backend.app.llm.recorder import LLMTelemetryRecorder
from backend.app.services.applications import ApplicationService
from backend.app.services.documents import DocumentService
from backend.app.services.onboarding import OnboardingService, SearchProfileDraft
from tests.test_v2_application_engine import _FullyAutomatedAdapter, _eligible, _submitted
from tests.test_v2_llm_recorder import _EXTERNAL, _FakeMonotonic, _clock, _request, _router
from tests.v2_builders import (
    NOW,
    OPPORTUNITY,
    USER,
    a_candidate_profile,
    a_decision,
    a_plan,
    a_usage_event,
    an_autopilot_policy,
    an_entitlement,
    an_evaluation,
    an_opportunity,
)
from tests.v2_fakes import (
    FakeApplicationDecisionRepository,
    FakeApplicationEventRepository,
    FakeApplicationOutcomeRepository,
    FakeApplicationPolicyRepository,
    FakeApplicationRepository,
    FakeCandidateDocumentRepository,
    FakeCandidateProfileRepository,
    FakeCareerRecommendationRepository,
    FakeEligibilityResultRepository,
    FakeLLMRunRepository,
    FakeMatchEvaluationRepository,
    FakeOpportunityRepository,
    FakePlanRepository,
    FakeRoleClassificationRepository,
    FakeSearchProfileRepository,
    FakeSubmissionAttemptRepository,
    FakeSubscriptionRepository,
    FakeUsageEventRepository,
    FakeUserRepository,
)
from tests.v2_interview import build_interview_service
from tests.v2_llm import FakeProvider
pytestmark = pytest.mark.asyncio


def _free_tier(*entitlements, used=()):
    """A `MeteringService` whose only plan is the free tier, granting exactly `entitlements`.

    No subscription is seeded, so the resolver falls back to the free plan — the tier a real
    account starts on (§4). Each test grants a *deliberately tiny* ceiling for the one key it
    exercises, so the real domain action reaches the quota at its own service boundary rather
    than a fixture-scale limit that would never be hit. `used` pre-loads the usage ledger with
    already-consumed events, so a period seeded to its ceiling refuses the very next action —
    the shape a mid-period request meets (§8).

    The free plan sets neither `price_amount_cents` nor `currency` (the price is all-or-nothing)
    and carries no billing interval, satisfying the `Plan` coherence validator.
    """
    plan = a_plan(
        slug=FREE_PLAN_SLUG, price_amount_cents=None, currency=None,
        billing_interval=None, external_price_id=None, entitlements=tuple(entitlements))
    plans = FakePlanRepository()
    plans.plans[plan.id] = plan
    usage = FakeUsageEventRepository()
    for event in used:
        usage.events[event.id] = event
    return MeteringService(EntitlementResolver(plans, FakeSubscriptionRepository()), usage)


def _draft(name, *, active=True):
    """A saved-search draft over one Swiss country area — active unless paused."""
    return SearchProfileDraft(
        name=name, is_active=active, areas=(CountrySearchArea(country="CH"),))


def _wire_applications(metering, *, adapter=None):
    """The Phase 12 lifecycle wired so the safety gate PERMITS an unattended submission,
    plus the `metering` under test — so the commercial clause is the only thing left that can
    refuse a submit the gate would otherwise allow.

    Mirrors `_wire` from the application-engine tests (an AUTOPILOT policy, an AUTO_APPLY
    decision, a high-fit evaluation, an ELIGIBLE result and a FULLY_SUPPORTED adapter that
    scripts a SUBMITTED result), the exact shape that reaches APPROVED then SUBMITTED — the
    difference here being the `metering` argument, which a paid plan could never use to loosen
    any of those gates (§4, §9). `adapter` overrides the scripted FULLY_SUPPORTED adapter when a
    test needs to observe whether the irreversible send was reached (the browser-lane regression).
    """
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
    policy = an_autopilot_policy()
    policies.policies[policy.id] = policy
    decision = a_decision()
    decisions.decisions[decision.id] = decision
    match = an_evaluation()
    matches.evaluations[match.id] = match
    eligibility = _eligible()
    eligs.results[eligibility.id] = eligibility

    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    registry.register(adapter if adapter is not None else _FullyAutomatedAdapter(_submitted()))

    service = ApplicationService(
        applications=apps, events=events, attempts=attempts, decisions=decisions,
        policies=policies, matches=matches, eligibilities=eligs, profiles=profiles,
        opportunities=opps, documents=docs, registry=registry, metering=metering)
    return service, SimpleNamespace(apps=apps, attempts=attempts)


async def test_active_search_creation_is_blocked_once_the_gauge_is_full():
    """ACTIVE_SEARCH_PROFILES is a live gauge, enforced at the onboarding boundary (§4-9).

    Consume-then-block: at a ceiling of one, the first active search is created, a paused one
    is created *at* the ceiling (proving a paused search consumes no gauge room), and the second
    *active* create is refused with `QUOTA_EXCEEDED`. The refused create leaves no row.
    """
    metering = _free_tier(
        an_entitlement(key=EntitlementKey.ACTIVE_SEARCH_PROFILES, limit=1))
    searches = FakeSearchProfileRepository()
    service = OnboardingService(
        FakeCandidateProfileRepository(), searches, FakeUserRepository(),
        metering=metering)

    await service.create_search(USER, _draft("first"), now=NOW)
    # A paused search is inert — it weighs nothing against the gauge, so it is allowed here.
    await service.create_search(USER, _draft("paused", active=False), now=NOW)

    with pytest.raises(BillingError) as caught:
        await service.create_search(USER, _draft("second"), now=NOW)
    assert caught.value.code is BillingErrorCode.QUOTA_EXCEEDED

    # The refused active search wrote no row: one active and one paused remain, nothing more.
    assert len(await searches.list_for_user(USER, active_only=True)) == 1
    assert len(await searches.list_for_user(USER)) == 2


async def test_document_generation_is_blocked_when_the_period_is_spent(tmp_path):
    """DOCUMENT_GENERATIONS is reserved before a byte is composed (§4-9).

    Pre-spent period: the ledger already carries the ceiling, so the next `generate` is refused
    with `QUOTA_EXCEEDED` before the generator runs — and because the quota fires ahead of
    composition, the sparse profile never reaches `InsufficientEvidence` and nothing is stored.
    """
    metering = _free_tier(
        an_entitlement(key=EntitlementKey.DOCUMENT_GENERATIONS, limit=3),
        used=(a_usage_event(
            entitlement_key=EntitlementKey.DOCUMENT_GENERATIONS,
            source_type=UsageSourceType.DOCUMENT_VERSION,
            source_id="seed", quantity=3),))
    profiles = FakeCandidateProfileRepository()
    profile = a_candidate_profile()
    profiles.profiles[profile.id] = profile
    opportunities = FakeOpportunityRepository()
    opportunities.opportunities[OPPORTUNITY] = an_opportunity()
    documents = FakeCandidateDocumentRepository()
    service = DocumentService(
        profiles, opportunities, documents,
        DeterministicDocumentGenerator(), CandidateEvidenceGuard(),
        LocalDocumentArtifactStore(tmp_path / "documents"), metering=metering)

    with pytest.raises(BillingError) as caught:
        await service.generate(USER, OPPORTUNITY, CandidateDocumentType.RESUME, now=NOW)
    assert caught.value.code is BillingErrorCode.QUOTA_EXCEEDED
    # The quota fired before composition, so no version was ever built or stored.
    assert documents.documents == {}


async def test_interview_session_creation_is_blocked_once_the_period_is_spent():
    """INTERVIEW_SESSIONS is reserved before the planner runs (§4-9).

    Consume-then-block: at a ceiling of one, the first `create_session` succeeds and the second
    is refused with `QUOTA_EXCEEDED`. Only the first session is stored — the refusal is before
    the plan is asked for or a session is opened.
    """
    metering = _free_tier(
        an_entitlement(key=EntitlementKey.INTERVIEW_SESSIONS, limit=1))
    harness = build_interview_service()
    service = InterviewService(
        sessions=harness.sessions, questions=harness.questions,
        answers=harness.answers, evaluations=harness.evaluations,
        summaries=harness.summaries, applications=harness.applications,
        context=InterviewContextBuilder(
            profiles=harness.profiles, opportunities=harness.opportunities),
        llm=harness.llm, transcriber=harness.transcriber, metering=metering)

    await service.create_session(
        USER, candidate_profile_id=harness.profile.id,
        opportunity_id=harness.opportunity.id, mode=InterviewMode.BEHAVIORAL, now=NOW)

    with pytest.raises(BillingError) as caught:
        await service.create_session(
            USER, candidate_profile_id=harness.profile.id,
            opportunity_id=harness.opportunity.id,
            mode=InterviewMode.BEHAVIORAL, now=NOW)
    assert caught.value.code is BillingErrorCode.QUOTA_EXCEEDED
    assert len(await service.list_sessions(USER)) == 1


async def test_application_submission_is_blocked_after_the_gate_permits_it():
    """APPLICATION_SUBMISSIONS is the one *added* clause, evaluated last (§4, §9).

    The gate is checked first and permits (FULLY_SUPPORTED adapter, AUTOPILOT policy, ELIGIBLE);
    only then does the exhausted commercial quota refuse with `QUOTA_EXCEEDED`. Because it fires
    before the SUBMITTING transition, the application stays APPROVED — retryable when the window
    resets — with no attempt opened and no irreversible send. A paid plan could never have made
    the gate permit; the quota can only restrict what the gate already allowed.
    """
    metering = _free_tier(
        an_entitlement(key=EntitlementKey.APPLICATION_SUBMISSIONS, limit=5),
        used=(a_usage_event(quantity=5),))
    service, store = _wire_applications(metering)
    app = await service.create(USER, OPPORTUNITY, now=NOW)
    prepared = await service.prepare(USER, app.id, now=NOW)
    assert prepared.state is ApplicationState.APPROVED

    with pytest.raises(BillingError) as caught:
        await service.submit(USER, app.id, now=NOW)
    assert caught.value.code is BillingErrorCode.QUOTA_EXCEEDED

    # The gate permitted, but the quota refused before SUBMITTING: untouched, no attempt opened.
    assert (await service.get(USER, app.id)).state is ApplicationState.APPROVED
    assert await store.attempts.list_for_application(USER, app.id) == ()


async def test_recommendation_generation_is_blocked_when_the_period_is_spent():
    """RECOMMENDATION_GENERATIONS is reserved before the funnel is even read (§4-9).

    Pre-spent period over empty analytics repositories: the ledger already carries the ceiling,
    so `recommend` is refused with `QUOTA_EXCEEDED` before any report is computed — and nothing
    is derived or stored.
    """
    metering = _free_tier(
        an_entitlement(key=EntitlementKey.RECOMMENDATION_GENERATIONS, limit=3),
        used=(a_usage_event(
            entitlement_key=EntitlementKey.RECOMMENDATION_GENERATIONS,
            source_type=UsageSourceType.CAREER_RECOMMENDATION,
            source_id="seed", quantity=3),))
    apps = FakeApplicationRepository()
    analytics = CareerAnalyticsService(
        FakeApplicationOutcomeRepository(), apps,
        FakeApplicationEventRepository(apps), FakeRoleClassificationRepository(),
        FakeOpportunityRepository(), FakeCandidateDocumentRepository())
    recommendations = FakeCareerRecommendationRepository()
    engine = CareerRecommendationEngine(analytics, recommendations, metering=metering)

    with pytest.raises(BillingError) as caught:
        await engine.recommend(USER, now=NOW)
    assert caught.value.code is BillingErrorCode.QUOTA_EXCEEDED
    assert await engine.latest(USER) == ()


async def test_llm_call_is_blocked_and_unrouted_when_tokens_are_spent():
    """LLM_TOKENS is reserved before the router runs (§5-9).

    Pre-spent period: the ledger already carries the ceiling, so `recorder.route` is refused
    with `QUOTA_EXCEEDED` before any provider is asked — no provider is spent and no telemetry
    row is written, exactly the shape of a refusal with no eligible provider.
    """
    metering = _free_tier(
        an_entitlement(key=EntitlementKey.LLM_TOKENS, limit=100),
        used=(a_usage_event(
            entitlement_key=EntitlementKey.LLM_TOKENS,
            source_type=UsageSourceType.LLM_RUN, source_id="seed", quantity=100),))
    provider = FakeProvider(provider_key="conn_gateway")
    runs = FakeLLMRunRepository()
    recorder = LLMTelemetryRecorder(
        runs=runs, clock=_clock, monotonic=_FakeMonotonic(), metering=metering)

    with pytest.raises(BillingError) as caught:
        await recorder.route(_router(provider), _request(), _EXTERNAL, user_id=USER)
    assert caught.value.code is BillingErrorCode.QUOTA_EXCEEDED
    # No provider was asked and no run was recorded — the refusal is ahead of both.
    assert provider.calls == 0
    assert runs.runs == {}

