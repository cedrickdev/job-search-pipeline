"""A failed letter resumes from the committed CV, without a second CV version."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.documents import (
    CandidateEvidenceGuard, DeterministicDocumentGenerator, LocalDocumentArtifactStore,
)
from backend.app.domain.decision import ApplicationDecisionKind
from backend.app.domain.documents import CandidateDocumentType
from backend.app.domain.identifiers import DiscoveryRunId, OpportunityId, UserId
from backend.app.domain.morning_preparation import PreparationStage
from backend.app.domain.task import TaskKind
from backend.app.infrastructure.database.models import DiscoveryRunRow, TaskRunRow
from backend.app.repositories.morning_preparation import MorningPreparationRepository
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyCandidateDocumentRepository, SqlAlchemyCandidateProfileRepository,
    SqlAlchemyOpportunityRepository, SqlAlchemySearchProfileRepository,
    SqlAlchemyTaskRunRepository,
)
from backend.app.services.documents import DocumentService
from backend.app.services.morning_documents import MorningDocumentsService
from backend.app.tasks.dispatcher import PersistedTaskDispatcher
from sqlalchemy import select
from tests.test_v2_documents import a_full_profile
from tests.v2_builders import a_search_profile, an_opportunity
from tests.v2_rows import a_user_row

NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_changed", [False, True])
async def test_failed_letter_retries_without_regenerating_the_cv(db_session, tmp_path,
                                                                policy_changed):
    owner = UserId(uuid4())
    run_id, opportunity_id = DiscoveryRunId(uuid4()), OpportunityId(uuid4())
    search = a_search_profile(user_id=owner)
    db_session.add(a_user_row(id=owner))
    await db_session.flush()
    await SqlAlchemySearchProfileRepository(db_session).upsert(search)
    await SqlAlchemyCandidateProfileRepository(db_session).upsert(a_full_profile(owner))
    await SqlAlchemyOpportunityRepository(db_session).upsert(an_opportunity(id=opportunity_id))
    db_session.add(DiscoveryRunRow(
        id=run_id, user_id=owner, search_profile_id=search.id, country="CH",
        trigger="MORNING", status="SUCCEEDED", started_at=NOW, finished_at=NOW,
        seen_count=1, matched_count=1, source_health=[], warning_codes=[]))
    await db_session.flush()
    ledger = MorningPreparationRepository(db_session)
    await ledger.start_or_get(owner, run_id, opportunity_id, now=NOW)
    await ledger.record_assessment(owner, run_id, opportunity_id,
                                   decision_id=uuid4(), now=NOW)
    assert (await ledger.get(owner, run_id, opportunity_id)).updated_at == NOW

    class FlakyGenerator(DeterministicDocumentGenerator):
        failures = 1

        async def generate_cover_letter(self, *, profile, opportunity, context):
            if self.failures:
                self.failures -= 1
                raise TimeoutError("synthetic provider outage")
            return await super().generate_cover_letter(
                profile=profile, opportunity=opportunity, context=context)

    generator = FlakyGenerator()
    connection = db_session.bind
    assert connection is not None
    factory = async_sessionmaker(bind=connection, expire_on_commit=False,
                                 join_transaction_mode="create_savepoint")

    async def document_builder(session: AsyncSession, _user_id: UserId) -> DocumentService:
        return DocumentService(
            SqlAlchemyCandidateProfileRepository(session),
            SqlAlchemyOpportunityRepository(session),
            SqlAlchemyCandidateDocumentRepository(session), generator,
            CandidateEvidenceGuard(), LocalDocumentArtifactStore(tmp_path))

    class Assessor:
        async def evaluate(self, *args, **kwargs):
            return SimpleNamespace()

    class Decider:
        kind = ApplicationDecisionKind.AUTO_APPLY

        async def decide(self, *args, **kwargs):
            return SimpleNamespace(kind=self.kind)

    decider = Decider()
    service = MorningDocumentsService(factory, document_builder=document_builder,
                                      assessor_builder=lambda _s: Assessor(),
                                      decider_builder=lambda _s: decider,
                                      dispatcher_builder=lambda session: PersistedTaskDispatcher(
                                          SqlAlchemyTaskRunRepository(session), clock=lambda: NOW))
    with pytest.raises(TimeoutError):
        await service.prepare(owner, run_id, opportunity_id, now=NOW)
    after_failure = await ledger.get(owner, run_id, opportunity_id)
    assert after_failure is not None
    assert after_failure.stage is PreparationStage.CV_RENDERED
    assert not (await db_session.scalars(select(TaskRunRow).where(
        TaskRunRow.kind == TaskKind.APPLICATION_PREPARATION))).all()
    cv_version = after_failure.resume_version_id

    with pytest.raises(LookupError):
        await service.prepare(UserId(uuid4()), run_id, opportunity_id, now=NOW)

    if policy_changed:
        decider.kind = ApplicationDecisionKind.REQUIRE_REVIEW

    completed = await service.prepare(owner, run_id, opportunity_id, now=NOW)
    assert completed.stage is (PreparationStage.REQUIRES_HUMAN if policy_changed
                               else PreparationStage.DOCUMENTS_RENDERED)
    assert completed.resume_version_id == cv_version
    document = await SqlAlchemyCandidateDocumentRepository(db_session).get_for_pair(
        owner, a_full_profile(owner).id, opportunity_id, CandidateDocumentType.RESUME)
    assert document is not None and len(document.versions) == 1
    letter = await SqlAlchemyCandidateDocumentRepository(db_session).get_for_pair(
        owner, a_full_profile(owner).id, opportunity_id, CandidateDocumentType.COVER_LETTER)
    assert (letter is None) if policy_changed else (letter is not None and len(letter.versions) == 1)
    tasks = (await db_session.scalars(select(TaskRunRow).where(
        TaskRunRow.kind == TaskKind.APPLICATION_PREPARATION))).all()
    assert len(tasks) == (0 if policy_changed else 1)
