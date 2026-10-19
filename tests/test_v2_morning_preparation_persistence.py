"""A run's preparation checkpoints are durable and invisible to other owners."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from backend.app.domain.identifiers import DiscoveryRunId, OpportunityId, UserId
from backend.app.domain.morning_preparation import PreparationStage
from backend.app.infrastructure.database.models import DiscoveryRunRow
from backend.app.repositories.morning_preparation import MorningPreparationRepository
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyOpportunityRepository, SqlAlchemySearchProfileRepository,
)
from tests.v2_builders import a_search_profile, an_opportunity
from tests.v2_rows import a_user_row


NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_owner_checkpoint_survives_duplicate_start_and_hides_from_other_user(db_session):
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
        seen_count=1, matched_count=1, source_health=[], warning_codes=[],
    ))
    await db_session.flush()

    repo = MorningPreparationRepository(db_session)
    started = await repo.start_or_get(owner, run_id, opportunity_id, now=NOW)
    assert started.stage is PreparationStage.DISCOVERED
    assert await repo.start_or_get(owner, run_id, opportunity_id, now=NOW) == started
    assert await repo.get(other, run_id, opportunity_id) is None
    with pytest.raises(LookupError):
        await repo.start_or_get(other, run_id, opportunity_id, now=NOW)

    decision_id = uuid4()
    matched = await repo.record_assessment(owner, run_id, opportunity_id,
                                           decision_id=decision_id, now=NOW)
    assert matched.decision_id == decision_id
    assert matched.stage is PreparationStage.MATCHED
    cv = uuid4()
    checkpoint = await repo.record_document_version(
        owner, run_id, opportunity_id, "RESUME", cv, now=NOW)
    assert checkpoint.resume_version_id == cv
    assert (await repo.get(owner, run_id, opportunity_id)).resume_version_id == cv

    assert [item.opportunity_id for item in await repo.list_for_run(owner, run_id)] == [opportunity_id]
    assert await repo.list_for_run(other, run_id) == ()
