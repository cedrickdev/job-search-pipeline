"""A synthetic partial Swiss discovery drains through the general lane only."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.discovery.orchestrator import DiscoveryOrchestrator
from backend.app.discovery.registry import SourceRegistry
from backend.app.documents import (
    CandidateEvidenceGuard, DeterministicDocumentGenerator, LocalDocumentArtifactStore,
)
from backend.app.domain.decision import ApplicationDecisionKind
from backend.app.domain.documents import CandidateDocumentType
from backend.app.domain.identifiers import SearchProfileId, UserId
from backend.app.domain.morning_preparation import PreparationStage
from backend.app.domain.task import TaskKind, TaskLane, TaskStatus
from backend.app.infrastructure.database.models import TaskRunRow
from backend.app.repositories.morning_preparation import MorningPreparationRepository
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyCandidateDocumentRepository, SqlAlchemyCandidateProfileRepository,
    SqlAlchemyOpportunityRepository, SqlAlchemySearchProfileRepository,
    SqlAlchemyTaskRunRepository,
)
from backend.app.services.discovery_catalog import DiscoveryCatalogService, DiscoveryEnqueueService
from backend.app.services.documents import DocumentService
from backend.app.services.morning_documents import MorningDocumentsService
from backend.app.services.morning_preparation import MorningPreparationService
from backend.app.tasks.dispatcher import PersistedTaskDispatcher
from backend.app.tasks.handlers import (
    build_document_generation_handler, build_matching_handler,
    build_opportunity_discovery_handler,
)
from backend.app.tasks.settings import TaskQueueSettings
from backend.app.tasks.worker import TaskWorker
from country_packs.contracts import SourceBinding
from country_packs.registry import CountryPackRegistry
from tests.test_v2_documents import a_full_profile
from tests.v2_builders import a_search_profile, an_opportunity
from tests.v2_discovery import FakeSource, a_metadata, a_pack, a_result
from tests.v2_rows import a_user_row

@pytest.mark.asyncio
async def test_partial_discovery_worker_restart_keeps_one_cv_and_letter(
        db_engine, tmp_path):
    owner, other = UserId(uuid4()), UserId(uuid4())
    search_id = SearchProfileId(uuid4())
    clock = [datetime.now(UTC) + timedelta(minutes=1)]
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    posting = an_opportunity()
    healthy = FakeSource(a_metadata("healthy"), result=a_result("healthy", posting))
    unavailable = FakeSource(a_metadata("unavailable"), error=TimeoutError("fixture only"))
    sources = SourceRegistry()
    sources.register_all((healthy, unavailable))
    pack = a_pack("CH", SourceBinding(source_key="healthy"),
                  SourceBinding(source_key="unavailable"))
    orchestrator = DiscoveryOrchestrator(
        registry=sources, packs=CountryPackRegistry((pack,)),
        clock=lambda: clock[0])

    async with factory.begin() as session:
        session.add_all((a_user_row(id=owner), a_user_row(id=other)))
        await session.flush()
        await SqlAlchemySearchProfileRepository(session).upsert(a_search_profile(
            id=search_id, user_id=owner, queries=(), source_keys=("healthy", "unavailable")))
        await SqlAlchemyCandidateProfileRepository(session).upsert(a_full_profile(owner))
        searches = SqlAlchemySearchProfileRepository(session)
        dispatcher = PersistedTaskDispatcher(SqlAlchemyTaskRunRepository(session),
                                             clock=lambda: clock[0])
        discovery_id = await DiscoveryEnqueueService(
            searches, dispatcher, clock=lambda: clock[0]).enqueue_discovery(
                owner, search_id, trigger="MORNING")

    catalog = DiscoveryCatalogService(factory, orchestrator,
                                      clock=lambda: clock[0], prepare_discovered=True)

    class Assessor:
        async def evaluate(self, *args, **kwargs):
            return SimpleNamespace()

    class Decider:
        async def decide(self, *args, **kwargs):
            return SimpleNamespace(id=uuid4(), kind=ApplicationDecisionKind.PREPARE)

    class FlakyGenerator(DeterministicDocumentGenerator):
        failures = 1

        async def generate_cover_letter(self, *, profile, opportunity, context):
            if self.failures:
                self.failures -= 1
                raise TimeoutError("synthetic generation interruption")
            return await super().generate_cover_letter(
                profile=profile, opportunity=opportunity, context=context)

    generator = FlakyGenerator()

    def matching_factory(session: AsyncSession) -> MorningPreparationService:
        return MorningPreparationService(
            ledger=MorningPreparationRepository(session), assessor=Assessor(),
            decider=Decider(), dispatcher=PersistedTaskDispatcher(
                SqlAlchemyTaskRunRepository(session), clock=lambda: clock[0]))

    async def document_builder(session: AsyncSession, _user_id: UserId) -> DocumentService:
        return DocumentService(
            SqlAlchemyCandidateProfileRepository(session),
            SqlAlchemyOpportunityRepository(session),
            SqlAlchemyCandidateDocumentRepository(session), generator,
            CandidateEvidenceGuard(), LocalDocumentArtifactStore(tmp_path))

    documents = MorningDocumentsService(
        factory, document_builder=document_builder,
        assessor_builder=lambda _session: Assessor(),
        decider_builder=lambda _session: Decider())
    handlers = {
        TaskKind.OPPORTUNITY_DISCOVERY: build_opportunity_discovery_handler(
            lambda _session: catalog),
        TaskKind.MATCHING: build_matching_handler(matching_factory,
                                                  clock=lambda: clock[0]),
        TaskKind.DOCUMENT_GENERATION: build_document_generation_handler(
            lambda _session: documents, clock=lambda: clock[0]),
    }
    settings = TaskQueueSettings(max_attempts=2, retry_base_delay_seconds=2)
    worker = TaskWorker(session_factory=factory, lane=TaskLane.GENERAL,
                        worker_id="first", handlers=handlers, settings=settings,
                        clock=lambda: clock[0])
    assert await worker.run_until_idle() == 3

    async with factory() as session:
        run = await catalog.get_run(owner, discovery_id)
        assert run is not None and run.status.value == "PARTIAL"
        preparations = await MorningPreparationRepository(session).list_for_run(
            owner, discovery_id)
        assert len(preparations) == 1
        checkpoint = preparations[0]
        task_failures = (await session.execute(select(TaskRunRow.failure_detail).where(
            TaskRunRow.kind == TaskKind.DOCUMENT_GENERATION))).scalars().all()
        assert checkpoint.stage is PreparationStage.CV_RENDERED, task_failures
        assert await MorningPreparationRepository(session).list_for_run(
            other, discovery_id) == ()
        cv_id = checkpoint.resume_version_id
        opportunity_id = checkpoint.opportunity_id
        first_cv = await SqlAlchemyCandidateDocumentRepository(session).get_for_pair(
            owner, a_full_profile(owner).id, opportunity_id,
            CandidateDocumentType.RESUME)
        assert first_cv is not None and len(first_cv.versions) == 1

    clock[0] += timedelta(seconds=3)
    restarted = TaskWorker(session_factory=factory, lane=TaskLane.GENERAL,
                           worker_id="restarted", handlers=handlers, settings=settings,
                           clock=lambda: clock[0])
    assert await restarted.run_until_idle() == 1

    async with factory() as session:
        final = await MorningPreparationRepository(session).get(
            owner, discovery_id, opportunity_id)
        assert final is not None and final.stage is PreparationStage.DOCUMENTS_RENDERED
        assert final.resume_version_id == cv_id
        documents_repo = SqlAlchemyCandidateDocumentRepository(session)
        cv = await documents_repo.get_for_pair(
            owner, a_full_profile(owner).id, opportunity_id, CandidateDocumentType.RESUME)
        letter = await documents_repo.get_for_pair(
            owner, a_full_profile(owner).id, opportunity_id,
            CandidateDocumentType.COVER_LETTER)
        assert cv is not None and letter is not None
        assert len(cv.versions) == len(letter.versions) == 1
        assert final.cover_letter_version_id == letter.versions[0].id
        assert (await session.execute(select(func.count()).select_from(TaskRunRow).where(
            TaskRunRow.kind == TaskKind.APPLICATION_SUBMISSION))).scalar_one() == 0
        assert (await session.execute(select(func.count()).select_from(TaskRunRow).where(
            TaskRunRow.lane == TaskLane.BROWSER))).scalar_one() == 0
        assert (await session.execute(select(func.count()).select_from(TaskRunRow).where(
            TaskRunRow.status == TaskStatus.DEAD_LETTERED))).scalar_one() == 0
