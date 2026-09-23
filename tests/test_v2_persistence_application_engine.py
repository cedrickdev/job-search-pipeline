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
from backend.app.application_engine.contracts import (
    AdapterCapabilities,
    AdapterPreparation,
    ApplicationContext,
)
from backend.app.application_engine.registry import ApplicationAdapterRegistry
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
    new_application_event_id,
    submission_attempt_id,
)
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
)
from backend.app.services.applications import ApplicationService
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



