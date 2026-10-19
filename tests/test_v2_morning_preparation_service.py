"""Morning assessment records intent, then queues documents without sending."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.domain.decision import ApplicationDecisionKind
from backend.app.domain.identifiers import DiscoveryRunId, OpportunityId, UserId
from backend.app.domain.morning_preparation import PreparationStage
from backend.app.domain.task import TaskKind
from backend.app.infrastructure.database.models import DiscoveryRunRow, TaskRunRow
from backend.app.repositories.morning_preparation import MorningPreparationRepository
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyOpportunityRepository, SqlAlchemySearchProfileRepository,
    SqlAlchemyTaskRunRepository,
)
from backend.app.services.morning_preparation import MorningPreparationService
from backend.app.tasks.dispatcher import PersistedTaskDispatcher
from tests.v2_builders import a_search_profile, an_opportunity
from tests.v2_rows import a_user_row

NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected_stage,task_count", [
    (ApplicationDecisionKind.AUTO_APPLY, PreparationStage.MATCHED, 1),
    (ApplicationDecisionKind.REQUIRE_REVIEW, PreparationStage.REQUIRES_HUMAN, 0),
    (ApplicationDecisionKind.SKIP, PreparationStage.SKIPPED, 0),
])
async def test_assessment_queues_documents_only_for_actionable_decision(
        db_session, kind, expected_stage, task_count):
    owner, other = UserId(uuid4()), UserId(uuid4())
    run_id, opportunity_id = DiscoveryRunId(uuid4()), OpportunityId(uuid4())
    search = a_search_profile(user_id=owner)
    db_session.add_all((a_user_row(id=owner), a_user_row(id=other)))
    await db_session.flush()
    await SqlAlchemySearchProfileRepository(db_session).upsert(search)
    await SqlAlchemyOpportunityRepository(db_session).upsert(an_opportunity(id=opportunity_id))
    db_session.add(DiscoveryRunRow(
        id=run_id, user_id=owner, search_profile_id=search.id, country="CH",
        trigger="MORNING", status="SUCCEEDED", started_at=NOW, finished_at=NOW,
        seen_count=1, matched_count=1, source_health=[], warning_codes=[]))
    await db_session.flush()
    ledger = MorningPreparationRepository(db_session)
    await ledger.start_or_get(owner, run_id, opportunity_id, now=NOW)

    class Assessor:
        async def evaluate(self, user_id, opportunity, *, now):
            assert user_id == owner and opportunity == opportunity_id and now == NOW
            return SimpleNamespace()

    class Decider:
        async def decide(self, user_id, opportunity, *, now):
            assert user_id == owner and opportunity == opportunity_id and now == NOW
            return SimpleNamespace(id=uuid4(), kind=kind)

    service = MorningPreparationService(
        ledger=ledger, assessor=Assessor(), decider=Decider(),
        dispatcher=PersistedTaskDispatcher(SqlAlchemyTaskRunRepository(db_session),
                                           clock=lambda: NOW))
    outcome = await service.assess_and_decide(owner, run_id, opportunity_id, now=NOW)
    assert outcome.stage is expected_stage
    tasks = (await db_session.scalars(select(TaskRunRow).where(
        TaskRunRow.user_id == owner, TaskRunRow.kind == TaskKind.DOCUMENT_GENERATION))).all()
    assert len(tasks) == task_count
    assert await ledger.get(other, run_id, opportunity_id) is None
    assert not (await db_session.scalars(select(TaskRunRow).where(
        TaskRunRow.kind == TaskKind.APPLICATION_SUBMISSION))).all()
