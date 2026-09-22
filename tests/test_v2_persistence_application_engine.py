"""The Phase 12 application-engine tables, asserted against PostgreSQL.

The unit suite (`test_v2_application_engine.py`) proves the service logic over fakes;
this proves the four properties only a real database establishes for the five new
tables: an upsert is an upsert (a retried write updates one row), the UNIQUE
idempotency key makes a duplicate application collide (§36), a user-scoped read
cannot see another account's application, and the append-only trail plus the
in-flight recovery query behave as the service depends on (§41, §88).

Every test runs inside the transaction `db_session` opened and rolls back.
"""
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.app.domain.application import (
    Application,
    ApplicationState,
    build_idempotency_key,
)
from backend.app.domain.application import SubmissionOutcome
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.application_event import (
    ApplicationEvent,
    ApplicationEventType,
    SubmissionAttempt,
)
from backend.app.domain.application_failure import ApplicationFailureCode
from backend.app.domain.decision import ApplicationDecisionKind
from backend.app.domain.identifiers import (
    application_id,
    new_application_event_id,
    submission_attempt_id,
)
from backend.app.infrastructure.database.models import ApplicationRow
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyApplicationDecisionRepository,
    SqlAlchemyApplicationEventRepository,
    SqlAlchemyApplicationPolicyRepository,
    SqlAlchemyApplicationRepository,
    SqlAlchemyOpportunityRepository,
    SqlAlchemySubmissionAttemptRepository,
)
from tests.v2_builders import (
    LATER,
    NOW,
    OPPORTUNITY,
    USER,
    a_decision,
    an_autopilot_policy,
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


def _an_application(decision, **overrides) -> Application:
    key = build_idempotency_key(
        candidate_profile_id=decision.candidate_profile_id,
        channel=ApplicationChannel.BROWSER, opportunity_id=OPPORTUNITY)
    fields = {
        "id": application_id(key),
        "user_id": USER,
        "candidate_profile_id": decision.candidate_profile_id,
        "decision_id": decision.id,
        "channel": ApplicationChannel.BROWSER,
        "state": ApplicationState.PLANNED,
        "idempotency_key": key,
        "opportunity_id": OPPORTUNITY,
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
async def test_count_submitted_since_counts_only_submitted(db_session, prerequisites):
    _, decision = await _seed_decision(db_session)
    repo = SqlAlchemyApplicationRepository(db_session)
    submitted = _an_application(decision, state=ApplicationState.SUBMITTED,
                                updated_at=NOW)
    await repo.upsert(submitted)
    assert await repo.count_submitted_since(USER, NOW - timedelta(days=1)) == 1
    assert await repo.count_submitted_since(USER, NOW + timedelta(days=1)) == 0


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
