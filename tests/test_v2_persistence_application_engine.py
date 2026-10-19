"""The Phase 12 application-engine tables, asserted against PostgreSQL.

The unit suite (`test_v2_application_engine.py`) proves the service logic over fakes;
this proves the properties only a real database establishes for the five new tables:
an upsert is an upsert (a retried write updates one row), the UNIQUE idempotency key
makes a duplicate application collide (§36), a user-scoped read cannot see another
account's application, the append-only trail plus the in-flight recovery query behave
as the service depends on (§41, §88) — and, load-bearing for the whole engine, that
the submission-budget reservation is atomic across two workers racing the last slot
(§49-51), so exactly one irreversible submit runs.

Most tests run inside the transaction `db_session` opened and rolls back. The
concurrency tests are the exception: an advisory lock only serializes across
independent connections that see each other's *committed* rows, so those use two real
`session_scope` units of work over `db_engine` and clean up after themselves.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from backend.app.application_engine.adapters.generic import GenericManualAdapter
from backend.app.application_engine.browser.form_capabilities import (
    FormCapability, FormCapabilityCatalog,
)
from backend.app.application_engine.contracts import (
    AdapterCapabilities,
    AdapterPreparation,
    ApplicationContext,
)
from backend.app.application_engine.registry import ApplicationAdapterRegistry
from backend.app.api.dependencies import application_queue_service
from backend.app.domain.application import (
    Application,
    ApplicationState,
    SubmissionOutcome,
    SubmissionResult,
    build_idempotency_key,
)
from backend.app.domain.application_channel import (
    AdapterSafetyLevel,
    ApplicationChannel,
)
from backend.app.domain.application_event import (
    ApplicationEvent,
    ApplicationEventType,
    SubmissionAttempt,
)
from backend.app.domain.application_failure import (
    ApplicationError,
    ApplicationFailureCode,
)
from backend.app.domain.decision import ApplicationDecisionKind
from backend.app.domain.identifiers import (
    ApplicationDecisionId,
    EligibilityResultId,
    application_id,
    default_candidate_profile_id,
    new_application_event_id,
    submission_attempt_id,
)
from backend.app.domain.task import TaskKind, TaskSpec
from backend.app.infrastructure.database.engine import (
    create_session_factory,
    session_scope,
)
from backend.app.infrastructure.database.models import ApplicationRow
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyApplicationDecisionRepository,
    SqlAlchemyApplicationEventRepository,
    SqlAlchemyApplicationPolicyRepository,
    SqlAlchemyApplicationRepository,
    SqlAlchemyCandidateDocumentRepository,
    SqlAlchemyCandidateProfileRepository,
    SqlAlchemyEligibilityResultRepository,
    SqlAlchemyMatchEvaluationRepository,
    SqlAlchemyOpportunityRepository,
    SqlAlchemySubmissionAttemptRepository,
    SqlAlchemyTaskRunRepository,
)
from backend.app.services.application_queue import ApplicationQueueService
from backend.app.services.applications import ApplicationNotActionable, ApplicationService
from backend.app.tasks.dispatcher import PersistedTaskDispatcher
from backend.app.tasks.failures import TaskFailure
from backend.app.tasks.handlers import build_application_submission_handler
from tests.v2_builders import (
    DECISION,
    ELIGIBILITY,
    LATER,
    NOW,
    OPPORTUNITY,
    OTHER_OPPORTUNITY,
    USER,
    a_candidate_profile,
    a_decision,
    a_source_record,
    an_autopilot_policy,
    an_eligibility_result,
    an_opportunity,
)
from tests.v2_api import api_harness
from tests.v2_rows import a_candidate_profile_row, a_user_row


@pytest_asyncio.fixture
async def prerequisites(db_session):
    """A user, its profile and an opportunity — the foreign-key targets."""
    from tests.v2_builders import OTHER_USER
    db_session.add_all([a_user_row(display_name="owner"),
                        a_user_row(id=OTHER_USER, display_name="other")])
    await db_session.flush()
    db_session.add_all([a_candidate_profile_row(display_name="candidate")])
    await db_session.flush()
    await SqlAlchemyOpportunityRepository(db_session).upsert(an_opportunity())
    await db_session.flush()


async def _count(session, model) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


async def _seed_decision(db_session):
    policy = await SqlAlchemyApplicationPolicyRepository(db_session).upsert(
        an_autopilot_policy())
    decision = await SqlAlchemyApplicationDecisionRepository(db_session).upsert(
        a_decision(policy_id=policy.id))
    return policy, decision


def _an_application(decision, *, channel: ApplicationChannel = ApplicationChannel.BROWSER,
                    opportunity_id=OPPORTUNITY, **overrides) -> Application:
    key = build_idempotency_key(
        candidate_profile_id=decision.candidate_profile_id,
        channel=channel, opportunity_id=opportunity_id)
    fields = {
        "id": application_id(key),
        "user_id": USER,
        "candidate_profile_id": decision.candidate_profile_id,
        "decision_id": decision.id,
        "channel": channel,
        "state": ApplicationState.PLANNED,
        "idempotency_key": key,
        "opportunity_id": opportunity_id,
        "policy_id": decision.policy_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return Application(**fields)


@pytest.mark.asyncio
async def test_a_policy_round_trips(db_session, prerequisites):
    repo = SqlAlchemyApplicationPolicyRepository(db_session)
    stored = await repo.upsert(an_autopilot_policy())
    read = await repo.get(USER, stored.id)
    assert read == stored
    assert (await repo.get_default(USER)).id == stored.id


@pytest.mark.asyncio
async def test_a_decision_round_trips_and_is_found_by_pair(db_session, prerequisites):
    _, decision = await _seed_decision(db_session)
    repo = SqlAlchemyApplicationDecisionRepository(db_session)
    found = await repo.get_for_pair(USER, decision.candidate_profile_id, OPPORTUNITY)
    assert found is not None
    assert found.kind is ApplicationDecisionKind.AUTO_APPLY


@pytest.mark.asyncio
async def test_an_application_round_trips(db_session, prerequisites):
    _, decision = await _seed_decision(db_session)
    repo = SqlAlchemyApplicationRepository(db_session)
    stored = await repo.upsert(_an_application(decision))
    read = await repo.get(USER, stored.id)
    assert read == stored


@pytest.mark.asyncio
async def test_the_idempotency_key_is_unique(db_session, prerequisites):
    _, decision = await _seed_decision(db_session)
    repo = SqlAlchemyApplicationRepository(db_session)
    await repo.upsert(_an_application(decision))
    # A different id but the same key is the duplicate the derivation prevents; the
    # UNIQUE constraint is the second defence and must reject it.
    from uuid import UUID

    from backend.app.domain.identifiers import ApplicationId
    clash = _an_application(decision).model_copy(
        update={"id": ApplicationId(UUID("00000000-0000-4000-8000-0000000009f1"))})
    db_session.add(ApplicationRow(
        id=clash.id, user_id=clash.user_id,
        candidate_profile_id=clash.candidate_profile_id, decision_id=clash.decision_id,
        channel=clash.channel.value, state=clash.state.value,
        idempotency_key=clash.idempotency_key, opportunity_id=clash.opportunity_id,
        created_at=clash.created_at, updated_at=clash.updated_at))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_upserting_an_application_twice_updates_one_row(db_session, prerequisites):
    _, decision = await _seed_decision(db_session)
    repo = SqlAlchemyApplicationRepository(db_session)
    app = await repo.upsert(_an_application(decision))
    await repo.upsert(app.transition_to(ApplicationState.PREPARING, at=LATER))
    assert await _count(db_session, ApplicationRow) == 1
    assert (await repo.get(USER, app.id)).state is ApplicationState.PREPARING


@pytest.mark.asyncio
async def test_another_users_application_reads_as_absent(db_session, prerequisites):
    from tests.v2_builders import OTHER_USER
    _, decision = await _seed_decision(db_session)
    repo = SqlAlchemyApplicationRepository(db_session)
    app = await repo.upsert(_an_application(decision))
    assert await repo.get(OTHER_USER, app.id) is None


@pytest.mark.asyncio
async def test_count_active_submissions_counts_every_slot_consuming_state(
        db_session, prerequisites):
    """The rate count sees a reserved slot, not only a confirmed one (§49-51).

    A slot is consumed the moment an application reaches SUBMITTING, not only when it
    reaches SUBMITTED — otherwise a worker that has just reserved would be invisible to
    the next and the last slot could be double-spent. STATE_UNKNOWN also holds a slot
    (it may have landed); FAILED and every pre-submission state do not.
    """
    _, decision = await _seed_decision(db_session)
    repo = SqlAlchemyApplicationRepository(db_session)
    # Distinct channels give distinct idempotency keys for one opportunity, so four
    # applications coexist. Three states consume a slot, one (FAILED) does not.
    for channel, state in (
            (ApplicationChannel.BROWSER, ApplicationState.SUBMITTED),
            (ApplicationChannel.EMAIL, ApplicationState.SUBMITTING),
            (ApplicationChannel.ATS_FORM, ApplicationState.SUBMISSION_STATE_UNKNOWN),
            (ApplicationChannel.DIRECT_FORM, ApplicationState.FAILED)):
        await repo.upsert(_an_application(
            decision, channel=channel, state=state, updated_at=NOW))
    assert await repo.count_active_submissions_since(USER, NOW - timedelta(days=1)) == 3
    assert await repo.count_pending_submissions_since(USER, NOW - timedelta(days=1)) == 2
    assert await repo.count_active_submissions_since(USER, NOW + timedelta(days=1)) == 0


@pytest.mark.asyncio
async def test_list_in_flight_finds_a_stuck_submission(db_session, prerequisites):
    _, decision = await _seed_decision(db_session)
    repo = SqlAlchemyApplicationRepository(db_session)
    await repo.upsert(_an_application(decision, state=ApplicationState.SUBMITTING))
    in_flight = await repo.list_in_flight()
    assert [a.state for a in in_flight] == [ApplicationState.SUBMITTING]


@pytest.mark.asyncio
async def test_the_event_trail_is_append_only_and_scoped(db_session, prerequisites):
    from tests.v2_builders import OTHER_USER
    _, decision = await _seed_decision(db_session)
    app = await SqlAlchemyApplicationRepository(db_session).upsert(
        _an_application(decision))
    events = SqlAlchemyApplicationEventRepository(db_session)
    await events.append(ApplicationEvent(
        id=new_application_event_id(), application_id=app.id,
        event_type=ApplicationEventType.CREATED, to_state=ApplicationState.PLANNED,
        occurred_at=NOW))
    await events.append(ApplicationEvent(
        id=new_application_event_id(), application_id=app.id,
        event_type=ApplicationEventType.PREPARATION_STARTED, occurred_at=LATER))
    mine = await events.list_for_application(USER, app.id)
    assert [e.event_type for e in mine] == [
        ApplicationEventType.CREATED, ApplicationEventType.PREPARATION_STARTED]
    assert await events.list_for_application(OTHER_USER, app.id) == ()


@pytest.mark.asyncio
async def test_a_submission_attempt_completes_in_place(db_session, prerequisites):
    _, decision = await _seed_decision(db_session)
    app = await SqlAlchemyApplicationRepository(db_session).upsert(
        _an_application(decision))
    attempts = SqlAlchemySubmissionAttemptRepository(db_session)
    attempt = SubmissionAttempt(
        id=submission_attempt_id(app.id, 1), application_id=app.id,
        attempt_number=1, adapter_key="browser/1", started_at=NOW)
    await attempts.upsert(attempt)  # in flight
    await attempts.upsert(attempt.model_copy(update={
        "outcome": SubmissionOutcome.FAILED,
        "failure_code": ApplicationFailureCode.APPLICATION_ADAPTER_ERROR,
        "finished_at": NOW}))
    stored = await attempts.get(app.id, 1)
    assert stored is not None
    assert stored.outcome is SubmissionOutcome.FAILED
    assert stored.is_in_flight is False


# ---------------------------------------------------------------------------
# The submission-budget reservation, under real concurrency (§49-51).
#
# The single most load-bearing safety property of the engine: two workers of one user
# racing the last slot must never both perform the irreversible submit. It cannot be
# shown over `db_session` (one savepointed connection cannot demonstrate a
# cross-connection advisory lock) nor over fakes (whose no-op lock proves nothing), so
# these two tests commit for real over independent `session_scope` units of work and
# assert the invariant that matters: `adapter.submit` runs exactly once.
# ---------------------------------------------------------------------------

_OTHER_DECISION = ApplicationDecisionId(UUID("00000000-0000-4000-8000-000000000062"))
_OTHER_ELIGIBILITY = EligibilityResultId(UUID("00000000-0000-4000-8000-000000000046"))
# A distinct instant from the seed (NOW), so the day/week window includes the winner's
# freshly reserved slot when the loser counts the budget.
_SUBMIT_AT = LATER
_QUEUE_TARGET = "https://jobs.lever.co/fixture/role/apply"
_QUEUE_FINGERPRINT = "a" * 64


def _queue_fixture_catalog() -> FormCapabilityCatalog:
    return FormCapabilityCatalog(forms=(FormCapability(
        id="lever-queue-fixture", ats="lever", employer="Fixture SA",
        target_url=_QUEUE_TARGET, driver_key="lever/1",
        form_fingerprint=_QUEUE_FINGERPRINT,
        allowed_required_fields=frozenset(), allowed_upload_slots=frozenset(),
        conditions_url="https://fixture.example/terms", review_id="fixture-review",
        reviewed_at=_SUBMIT_AT.date(), access_decision="approved",
    ),))


class _CountingBrowserAdapter:
    """A FULLY_SUPPORTED browser adapter that counts its irreversible submits.

    Shared between the two racing workers, so the test can assert the reservation let
    exactly one through. FULLY_SUPPORTED so an autopilot policy resolves to PERMITTED
    and the winner submits unattended rather than stopping for review — which is what
    puts two workers on the same irreversible path in the first place.
    """

    def __init__(self) -> None:
        self.submit_calls = 0

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            key="counting-browser/1", channel=ApplicationChannel.BROWSER,
            safety_level=AdapterSafetyLevel.FULLY_SUPPORTED,
            can_prepare=True, can_submit=True)

    async def prepare(self, context: ApplicationContext) -> AdapterPreparation:
        return AdapterPreparation(form_fingerprint="counting-form/1")

    async def submit(self, context: ApplicationContext) -> SubmissionResult:
        # No `await` between the read and the write, so the increment is atomic on the
        # event loop: a double-submit shows up as `submit_calls == 2`, never as a lost
        # update that would mask the very bug this test exists to catch.
        self.submit_calls += 1
        return SubmissionResult(outcome=SubmissionOutcome.SUBMITTED,
                                confirmation_reference="confirmed-001")


def _service(session, registry: ApplicationAdapterRegistry) -> ApplicationService:
    """An `ApplicationService` wired to real repositories over one session."""
    return ApplicationService(
        applications=SqlAlchemyApplicationRepository(session),
        events=SqlAlchemyApplicationEventRepository(session),
        attempts=SqlAlchemySubmissionAttemptRepository(session),
        decisions=SqlAlchemyApplicationDecisionRepository(session),
        policies=SqlAlchemyApplicationPolicyRepository(session),
        matches=SqlAlchemyMatchEvaluationRepository(session),
        eligibilities=SqlAlchemyEligibilityResultRepository(session),
        profiles=SqlAlchemyCandidateProfileRepository(session),
        opportunities=SqlAlchemyOpportunityRepository(session),
        documents=SqlAlchemyCandidateDocumentRepository(session),
        registry=registry)


@pytest_asyncio.fixture
async def committed_world(db_engine):
    """A real session factory whose committed rows are truncated on teardown.

    The reservation can only be shown across two connections that see each other's
    committed writes, so these tests cannot lean on `db_session`'s rollback. This
    commits for real and, on the way out, truncates `users` and `opportunities` with
    CASCADE — which reaches every account- and posting-scoped row the tests wrote — so
    the once-per-session schema is left clean for whatever runs next.
    """
    factory = create_session_factory(db_engine)
    try:
        yield factory
    finally:
        async with db_engine.begin() as connection:
            await connection.execute(text(
                "TRUNCATE users, opportunities RESTART IDENTITY CASCADE"))


async def _seed_two_approved_browser_applications(factory, policy):
    """Commit a user, profile, two postings and two APPROVED BROWSER applications.

    Returns the two application ids. Each posting gets its own AUTO_APPLY decision and
    an ELIGIBLE eligibility, so the submit-time gate re-read finds intent and a passing
    verdict for both; the shared `policy` carries whichever rate limit the test set.
    """
    async with session_scope(factory) as session:
        session.add(a_user_row(display_name="owner"))
        await session.flush()
        await SqlAlchemyCandidateProfileRepository(session).upsert(a_candidate_profile())
        opportunities = SqlAlchemyOpportunityRepository(session)
        await opportunities.upsert(an_opportunity())
        await opportunities.upsert(an_opportunity(
            id=OTHER_OPPORTUNITY,
            source=a_source_record(external_id="posting-2",
                                   source_url="https://example.test/postings/2"),
            application_url="https://example.test/postings/2/apply",
            dedup_fingerprint="fingerprint-2"))
        stored_policy = await SqlAlchemyApplicationPolicyRepository(session).upsert(policy)
        decisions = SqlAlchemyApplicationDecisionRepository(session)
        eligibilities = SqlAlchemyEligibilityResultRepository(session)
        applications = SqlAlchemyApplicationRepository(session)
        app_ids = []
        for opportunity_id, decision_id, eligibility_id in (
                (OPPORTUNITY, DECISION, ELIGIBILITY),
                (OTHER_OPPORTUNITY, _OTHER_DECISION, _OTHER_ELIGIBILITY)):
            decision = await decisions.upsert(a_decision(
                id=decision_id, opportunity_id=opportunity_id,
                policy_id=stored_policy.id))
            await eligibilities.upsert(an_eligibility_result(
                id=eligibility_id, opportunity_id=opportunity_id))
            app = await applications.upsert(_an_application(
                decision, opportunity_id=opportunity_id,
                state=ApplicationState.APPROVED))
            app_ids.append(app.id)
        return app_ids[0], app_ids[1]


async def _submit_in_own_unit_of_work(factory, registry, app_id):
    """One worker: submit `app_id` in its own committed transaction.

    Mirrors a real worker process — its own session, its own `session_scope` — so the
    advisory lock it takes is held on its own connection and released only when this
    unit of work commits or rolls back.
    """
    async with session_scope(factory) as session:
        return await _service(session, registry).submit(USER, app_id, now=_SUBMIT_AT)


@pytest.mark.asyncio
async def test_crash_after_click_preserves_committed_attempt_and_blocks_redelivery(
        committed_world):
    app_id, _ = await _seed_two_approved_browser_applications(
        committed_world, an_autopilot_policy())
    adapter = _CountingBrowserAdapter()
    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    registry.register(adapter)

    async with session_scope(committed_world) as session:
        reservation = await _service(session, registry).reserve_submission(
            USER, app_id, now=_SUBMIT_AT)
    async with session_scope(committed_world, commit=False) as session:
        app = await SqlAlchemyApplicationRepository(session).get(USER, app_id)
        attempts = await SqlAlchemySubmissionAttemptRepository(session).list_for_application(
            USER, app_id)
        assert app.state is ApplicationState.SUBMITTING
        assert len(attempts) == 1
        assert attempts[0].id == reservation.attempt_id
        assert attempts[0].is_in_flight

    # Browser action may have happened, but the worker dies before finalization.
    async with session_scope(committed_world) as session:
        recovered = await _service(session, registry).recover_in_flight(now=LATER)
    assert any(app.id == app_id and app.state is ApplicationState.SUBMISSION_STATE_UNKNOWN
               for app in recovered)
    async with session_scope(committed_world, commit=False) as session:
        attempts = await SqlAlchemySubmissionAttemptRepository(session).list_for_application(
            USER, app_id)
        assert attempts[0].outcome is SubmissionOutcome.STATE_UNKNOWN
        assert not attempts[0].is_in_flight
    async with session_scope(committed_world) as session:
        with pytest.raises(ApplicationNotActionable):
            await _service(session, registry).reserve_submission(USER, app_id, now=LATER)
    assert adapter.submit_calls == 0


@pytest.mark.asyncio
async def test_concurrent_reservations_cannot_spend_one_daily_slot_twice(committed_world):
    first, second = await _seed_two_approved_browser_applications(
        committed_world, an_autopilot_policy(max_applications_per_day=1))
    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    registry.register(_CountingBrowserAdapter())

    async def reserve(app_id):
        async with session_scope(committed_world) as session:
            return await _service(session, registry).reserve_submission(
                USER, app_id, now=_SUBMIT_AT)

    results = await asyncio.gather(reserve(first), reserve(second), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, ApplicationError) and
               item.code is ApplicationFailureCode.APPLICATION_RATE_LIMITED
               for item in results) == 1


@pytest.mark.asyncio
async def test_recovered_unknown_holds_its_original_billing_period_not_a_new_one(
        committed_world):
    app_id, _ = await _seed_two_approved_browser_applications(
        committed_world, an_autopilot_policy())
    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    registry.register(_CountingBrowserAdapter())
    async with session_scope(committed_world) as session:
        await _service(session, registry).reserve_submission(USER, app_id, now=_SUBMIT_AT)
    recovered_at = _SUBMIT_AT + timedelta(days=40)
    async with session_scope(committed_world) as session:
        await _service(session, registry).recover_in_flight(now=recovered_at)
    async with session_scope(committed_world, commit=False) as session:
        repo = SqlAlchemyApplicationRepository(session)
        assert await repo.count_pending_submissions_since(
            USER, _SUBMIT_AT - timedelta(days=1)) == 1
        assert await repo.count_pending_submissions_since(
            USER, recovered_at - timedelta(days=1)) == 0


@pytest.mark.asyncio
async def test_browser_handler_commits_reservation_before_adapter_submit(committed_world):
    app_id, _ = await _seed_two_approved_browser_applications(
        committed_world, an_autopilot_policy())
    observed = []

    class ObservingAdapter(_CountingBrowserAdapter):
        async def submit(self, context):
            async with session_scope(committed_world, commit=False) as session:
                app = await SqlAlchemyApplicationRepository(session).get(USER, app_id)
                attempts = await SqlAlchemySubmissionAttemptRepository(
                    session).list_for_application(USER, app_id)
                observed.append((app.state, len(attempts), attempts[0].is_in_flight))
            return await super().submit(context)

    adapter = ObservingAdapter()
    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    registry.register(adapter)
    handler = build_application_submission_handler(
        lambda session: _service(session, registry),
        session_factory=committed_world, live_submissions_enabled=True,
        clock=lambda: _SUBMIT_AT)
    task = TaskSpec(
        kind=TaskKind.APPLICATION_SUBMISSION, idempotency_key="durable-handler-test",
        user_id=USER, payload={"application_id": str(app_id)},
    ).to_queued_run(as_of=_SUBMIT_AT)
    await handler(task, None)

    assert observed == [(ApplicationState.SUBMITTING, 1, True)]
    assert adapter.submit_calls == 1
    async with session_scope(committed_world, commit=False) as session:
        app = await SqlAlchemyApplicationRepository(session).get(USER, app_id)
        assert app.state is ApplicationState.SUBMITTED


@pytest.mark.asyncio
async def test_policy_brake_waits_for_in_flight_browser_click(committed_world):
    """A pause cannot be acknowledged between final authorization and the click."""
    app_id, _ = await _seed_two_approved_browser_applications(
        committed_world, an_autopilot_policy(max_applications_per_day=10))
    entered = asyncio.Event()
    release = asyncio.Event()
    pause_done = asyncio.Event()

    class PausedBrowserAdapter(_CountingBrowserAdapter):
        async def submit(self, context):
            entered.set()
            await release.wait()
            return await super().submit(context)

    adapter = PausedBrowserAdapter()
    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    registry.register(adapter)
    handler = build_application_submission_handler(
        lambda session: _service(session, registry),
        session_factory=committed_world, live_submissions_enabled=True,
        clock=lambda: _SUBMIT_AT)
    task = TaskSpec(
        kind=TaskKind.APPLICATION_SUBMISSION, idempotency_key="pause-barrier-fixture",
        user_id=USER, payload={"application_id": str(app_id)},
    ).to_queued_run(as_of=_SUBMIT_AT)

    async def pause_policy() -> None:
        async with session_scope(committed_world) as session:
            policies = SqlAlchemyApplicationPolicyRepository(session)
            policy = await policies.get_default(USER)
            assert policy is not None
            await policies.upsert(policy.model_copy(update={
                "max_applications_per_day": 0, "updated_at": _SUBMIT_AT,
            }))
        pause_done.set()

    sending = asyncio.create_task(handler(task, None))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        pausing = asyncio.create_task(pause_policy())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(pause_done.wait(), timeout=0.25)
        assert adapter.submit_calls == 0
        release.set()
        await asyncio.wait_for(sending, timeout=5)
        await asyncio.wait_for(pausing, timeout=5)
    finally:
        release.set()
        if not sending.done():
            await sending

    assert adapter.submit_calls == 1
    async with session_scope(committed_world, commit=False) as session:
        policy = await SqlAlchemyApplicationPolicyRepository(session).get_default(USER)
        assert policy is not None
        assert policy.max_applications_per_day == 0


@pytest.mark.asyncio
async def test_persisted_browser_queue_reaches_worker_once(committed_world):
    app_id, _ = await _seed_two_approved_browser_applications(
        committed_world, an_autopilot_policy())
    adapter = _CountingBrowserAdapter()
    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    registry.register(adapter)

    async with session_scope(committed_world) as session:
        await SqlAlchemyOpportunityRepository(session).upsert(
            an_opportunity(application_url=_QUEUE_TARGET))
        applications = SqlAlchemyApplicationRepository(session)
        approved = await applications.get(USER, app_id)
        assert approved is not None
        await applications.upsert(approved.model_copy(update={
            "form_driver_key": "lever/1", "form_fingerprint": _QUEUE_FINGERPRINT,
            "form_capability_id": "lever-queue-fixture",
        }))
        queue = ApplicationQueueService(
            applications,
            SqlAlchemyApplicationPolicyRepository(session),
            SqlAlchemyOpportunityRepository(session),
            PersistedTaskDispatcher(SqlAlchemyTaskRunRepository(session),
                                    clock=lambda: _SUBMIT_AT),
            live_enabled=True, form_catalog=_queue_fixture_catalog())
        task_id = await queue.enqueue_approved(USER, app_id)
        assert await queue.enqueue_approved(USER, app_id) == task_id

    async with session_scope(committed_world, commit=False) as session:
        task = await SqlAlchemyTaskRunRepository(session).get_by_id(task_id)
    assert task is not None
    assert task.user_id == USER
    assert task.kind is TaskKind.APPLICATION_SUBMISSION
    assert task.max_attempts == 1
    assert task.payload == {"application_id": str(app_id)}

    handler = build_application_submission_handler(
        lambda session: _service(session, registry),
        session_factory=committed_world, live_submissions_enabled=True,
        clock=lambda: _SUBMIT_AT)
    await handler(task, None)
    with pytest.raises(TaskFailure):
        await handler(task, None)
    assert adapter.submit_calls == 1
    async with session_scope(committed_world, commit=False) as session:
        app = await SqlAlchemyApplicationRepository(session).get(USER, app_id)
        assert app.state is ApplicationState.SUBMITTED


@pytest.mark.asyncio
async def test_authenticated_api_queue_persists_task_and_worker_finishes_it(
        committed_world, tmp_path):
    adapter = _CountingBrowserAdapter()
    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    registry.register(adapter)
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        owner = next(iter(api.users.users))
        profile_id = default_candidate_profile_id(owner)
        async with session_scope(committed_world) as session:
            session.add(a_user_row(id=owner, display_name="owner"))
            await session.flush()
            await SqlAlchemyCandidateProfileRepository(session).upsert(
                a_candidate_profile(id=profile_id, user_id=owner))
            await SqlAlchemyOpportunityRepository(session).upsert(
                an_opportunity(application_url=_QUEUE_TARGET))
            policy = await SqlAlchemyApplicationPolicyRepository(session).upsert(
                an_autopilot_policy(user_id=owner))
            decision = await SqlAlchemyApplicationDecisionRepository(session).upsert(
                a_decision(user_id=owner, candidate_profile_id=profile_id,
                           policy_id=policy.id))
            await SqlAlchemyEligibilityResultRepository(session).upsert(
                an_eligibility_result(user_id=owner,
                                      candidate_profile_id=profile_id))
            approved = await SqlAlchemyApplicationRepository(session).upsert(
                _an_application(decision, state=ApplicationState.APPROVED,
                                user_id=owner, form_driver_key="lever/1",
                                form_fingerprint=_QUEUE_FINGERPRINT,
                                form_capability_id="lever-queue-fixture"))

        async def persisted_queue():
            async with session_scope(committed_world) as session:
                yield ApplicationQueueService(
                    SqlAlchemyApplicationRepository(session),
                    SqlAlchemyApplicationPolicyRepository(session),
                    SqlAlchemyOpportunityRepository(session),
                    PersistedTaskDispatcher(SqlAlchemyTaskRunRepository(session),
                                            clock=lambda: _SUBMIT_AT),
                    live_enabled=True, form_catalog=_queue_fixture_catalog())

        api.app.dependency_overrides[application_queue_service] = persisted_queue
        response = await api.write("POST", f"/applications/{approved.id}/queue")
        assert response.status_code == 202, response.text
        task_id = UUID(response.json()["task_id"])
        async with session_scope(committed_world, commit=False) as session:
            task = await SqlAlchemyTaskRunRepository(session).get_by_id(task_id)
        assert task is not None
        assert task.user_id == owner

        handler = build_application_submission_handler(
            lambda session: _service(session, registry),
            session_factory=committed_world, live_submissions_enabled=True,
            clock=lambda: _SUBMIT_AT)
        await handler(task, None)
        assert adapter.submit_calls == 1
        async with session_scope(committed_world, commit=False) as session:
            stored = await SqlAlchemyApplicationRepository(session).get(owner, approved.id)
            assert stored.state is ApplicationState.SUBMITTED


async def _assert_last_slot_is_won_exactly_once(factory, policy):
    """Race two submissions for a budget of one, and assert one — only one — landed."""
    app_a, app_b = await _seed_two_approved_browser_applications(factory, policy)
    adapter = _CountingBrowserAdapter()
    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    registry.register(adapter)

    results = await asyncio.gather(
        _submit_in_own_unit_of_work(factory, registry, app_a),
        _submit_in_own_unit_of_work(factory, registry, app_b),
        return_exceptions=True)

    # One worker submitted; the other was refused as rate-limited — not by which one,
    # because the race winner is not deterministic, but by the shape of the outcome.
    submitted = [r for r in results
                 if isinstance(r, Application) and r.state is ApplicationState.SUBMITTED]
    rate_limited = [r for r in results
                    if isinstance(r, ApplicationError)
                    and r.code is ApplicationFailureCode.APPLICATION_RATE_LIMITED]
    assert len(submitted) == 1, results
    assert len(rate_limited) == 1, results
    # The invariant the whole reservation exists for: the irreversible act ran once.
    assert adapter.submit_calls == 1

    # And it is durable: exactly one row is SUBMITTED, the loser is untouched (its unit
    # of work rolled back on the raised rate-limit), so it stays APPROVED for a retry.
    async with session_scope(factory, commit=False) as session:
        stored = SqlAlchemyApplicationRepository(session)
        read_a = await stored.get(USER, app_a)
        read_b = await stored.get(USER, app_b)
    assert read_a is not None and read_b is not None
    assert {read_a.state, read_b.state} == {
        ApplicationState.APPROVED, ApplicationState.SUBMITTED}


@pytest.mark.asyncio
async def test_two_workers_racing_the_last_daily_slot_submit_once(committed_world):
    await _assert_last_slot_is_won_exactly_once(
        committed_world, an_autopilot_policy(max_applications_per_day=1))


@pytest.mark.asyncio
async def test_two_workers_racing_the_last_weekly_slot_submit_once(committed_world):
    await _assert_last_slot_is_won_exactly_once(
        committed_world, an_autopilot_policy(max_applications_per_week=1))
