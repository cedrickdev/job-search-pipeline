"""Durable handoff from exact Morning Run dossier to browser preparation."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.application_engine.adapters.browser import BrowserApplicationAdapter
from backend.app.application_engine.adapters.generic import GenericManualAdapter
from backend.app.application_engine.registry import ApplicationAdapterRegistry
from backend.app.application_engine.task_dispatcher import (
    BrowserTaskOutcome,
    BrowserTaskResult,
    FakeTaskDispatcher,
)
from backend.app.domain.application import ApplicationState
from backend.app.domain.documents import (
    CandidateDocumentType,
    CoverLetterDocument,
    EvidenceBackedText,
)
from backend.app.domain.identifiers import DiscoveryRunId
from backend.app.domain.task import TaskKind, TaskLane, lane_for_kind
from backend.app.infrastructure.database.models import (
    ApplicationRow,
    DiscoveryRunRow,
    TaskRunRow,
)
from backend.app.repositories.morning_preparation import MorningPreparationRepository
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
    SqlAlchemySearchProfileRepository,
    SqlAlchemySubmissionAttemptRepository,
    SqlAlchemyTaskRunRepository,
)
from backend.app.services.applications import ApplicationService
from backend.app.services.morning_application import MorningApplicationService
from backend.app.tasks.dispatcher import PersistedTaskDispatcher
from tests.v2_builders import (
    EVIDENCE,
    OPPORTUNITY,
    OTHER_USER,
    USER,
    a_candidate_profile,
    a_decision,
    a_rendered_document,
    a_search_profile,
    an_autopilot_policy,
    an_opportunity,
)
from tests.v2_rows import a_user_row

NOW = datetime(2026, 10, 13, 8, tzinfo=UTC)


def test_application_preparation_runs_only_in_browser_lane() -> None:
    assert lane_for_kind(TaskKind.APPLICATION_PREPARATION) is TaskLane.BROWSER


def test_service_exposes_durable_preparation_handoff() -> None:
    assert callable(MorningApplicationService.prepare)


@pytest.mark.asyncio
async def test_committed_application_is_reused_and_exact_versions_are_pinned(db_session) -> None:
    connection = db_session.bind
    assert connection is not None
    factory = async_sessionmaker(
        bind=connection, expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    run_id = DiscoveryRunId(uuid4())
    cv = a_rendered_document(storage_key="fixture-cv")
    letter = a_rendered_document(
        storage_key="fixture-letter", document_type=CandidateDocumentType.COVER_LETTER,
        content=CoverLetterDocument(
            signature="Fixture Candidate",
            body=(EvidenceBackedText(text="Built a platform", evidence_ids=(EVIDENCE,)),),
        ),
    )
    async with factory.begin() as session:
        session.add_all((a_user_row(id=USER), a_user_row(id=OTHER_USER)))
        await session.flush()
        await SqlAlchemyCandidateProfileRepository(session).upsert(a_candidate_profile())
        await SqlAlchemySearchProfileRepository(session).upsert(a_search_profile())
        await SqlAlchemyOpportunityRepository(session).upsert(an_opportunity())
        policy = await SqlAlchemyApplicationPolicyRepository(session).upsert(
            an_autopilot_policy())
        await SqlAlchemyApplicationDecisionRepository(session).upsert(
            a_decision(policy_id=policy.id))
        docs = SqlAlchemyCandidateDocumentRepository(session)
        await docs.upsert(cv)
        await docs.upsert(letter)
        session.add(DiscoveryRunRow(
            id=run_id, user_id=USER, search_profile_id=a_search_profile().id,
            country="CH", trigger="MORNING", status="SUCCEEDED", started_at=NOW,
            finished_at=NOW, seen_count=1, matched_count=1,
            source_health=[], warning_codes=[],
        ))
        await session.flush()
        ledger = MorningPreparationRepository(session)
        await ledger.start_or_get(USER, run_id, OPPORTUNITY, now=NOW)
        await ledger.record_assessment(
            USER, run_id, OPPORTUNITY, decision_id=a_decision().id, now=NOW)
        await ledger.record_document_version(
            USER, run_id, OPPORTUNITY, CandidateDocumentType.RESUME.value,
            cv.versions[0].id, now=NOW)
        await ledger.record_document_version(
            USER, run_id, OPPORTUNITY, CandidateDocumentType.COVER_LETTER.value,
            letter.versions[0].id, now=NOW)

    class CommittedReadDispatcher(FakeTaskDispatcher):
        async def run(self, task):
            async with factory() as read_session:
                visible = await SqlAlchemyApplicationRepository(read_session).get(
                    USER, task.application_id)
                assert visible is not None, "browser child cannot read uncommitted application"
            return await super().run(task)

    dispatcher = CommittedReadDispatcher(default=BrowserTaskResult(
        outcome=BrowserTaskOutcome.COMPLETED, driver_key="lever/1",
        form_fingerprint="a" * 64,
    ))
    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    registry.register(BrowserApplicationAdapter(dispatcher))

    interrupted = [True]

    class InterruptingApplicationService(ApplicationService):
        async def prepare(self, *args, **kwargs):
            if interrupted[0]:
                interrupted[0] = False
                raise RuntimeError("synthetic crash after committed create")
            return await super().prepare(*args, **kwargs)

    def application_builder(session: AsyncSession) -> ApplicationService:
        return InterruptingApplicationService(
            applications=SqlAlchemyApplicationRepository(session),
            events=SqlAlchemyApplicationEventRepository(session),
            attempts=SqlAlchemySubmissionAttemptRepository(session),
            decisions=SqlAlchemyApplicationDecisionRepository(session),
            policies=SqlAlchemyApplicationPolicyRepository(session),
            matches=SqlAlchemyMatchEvaluationRepository(session),
            eligibilities=SqlAlchemyEligibilityResultRepository(session),
            profiles=SqlAlchemyCandidateProfileRepository(session),
            opportunities=SqlAlchemyOpportunityRepository(session),
            documents=SqlAlchemyCandidateDocumentRepository(session), registry=registry,
        )

    service = MorningApplicationService(
        factory, application_builder=application_builder,
        task_dispatcher_builder=lambda session: PersistedTaskDispatcher(
            SqlAlchemyTaskRunRepository(session), clock=lambda: NOW),
    )
    assert await service.reconcile_ready() == 1
    assert await service.reconcile_ready() == 0
    async with factory() as session:
        queued = (await session.execute(select(TaskRunRow).where(
            TaskRunRow.kind == TaskKind.APPLICATION_PREPARATION))).scalars().all()
        assert len(queued) == 1
        assert queued[0].lane is TaskLane.BROWSER
    with pytest.raises(LookupError):
        await service.prepare(OTHER_USER, run_id, OPPORTUNITY, now=NOW)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        await service.prepare(USER, run_id, OPPORTUNITY, now=NOW)
    first = await service.prepare(USER, run_id, OPPORTUNITY, now=NOW)
    second = await service.prepare(USER, run_id, OPPORTUNITY, now=NOW)
    assert second.id == first.id
    assert {pin.document_type: pin.version_id for pin in first.pinned_documents} == {
        CandidateDocumentType.RESUME: cv.versions[0].id,
        CandidateDocumentType.COVER_LETTER: letter.versions[0].id,
    }
    async with factory() as session:
        count = (await session.execute(select(func.count()).select_from(ApplicationRow))).scalar_one()
    assert count == 1
    async with factory.begin() as session:
        stored = await SqlAlchemyApplicationRepository(session).get(USER, first.id)
        assert stored is not None
        await SqlAlchemyApplicationRepository(session).upsert(stored.model_copy(update={
            "state": ApplicationState.SUBMITTED,
        }))
    terminal = await service.prepare(USER, run_id, OPPORTUNITY, now=NOW)
    assert terminal.state is ApplicationState.SUBMITTED
