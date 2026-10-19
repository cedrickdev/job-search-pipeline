"""Synthetic, database-backed Swiss dossier to browser-lane acceptance.

The browser dispatcher is in-process and never contacts an employer. Real
Chromium form behavior is covered separately by test_v2_browser_automation.
"""

import asyncio
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.application_engine.adapters.browser import BrowserApplicationAdapter
from backend.app.application_engine.adapters.generic import GenericManualAdapter
from backend.app.application_engine.browser.form_capabilities import (
    FormCapability, FormCapabilityCatalog,
)
from backend.app.application_engine.registry import ApplicationAdapterRegistry
from backend.app.application_engine.task_dispatcher import (
    BrowserTaskKind, BrowserTaskOutcome, BrowserTaskResult,
)
from backend.app.domain.application import ApplicationState
from backend.app.domain.documents import CandidateDocumentType, CoverLetterDocument, EvidenceBackedText
from backend.app.domain.identifiers import DiscoveryRunId
from backend.app.domain.task import TaskKind
from backend.app.infrastructure.database.mappers import task_run_to_domain
from backend.app.infrastructure.database.models import (
    ApplicationRow, DiscoveryRunRow, SubmissionAttemptRow, TaskRunRow,
)
from backend.app.infrastructure.database.engine import create_session_factory, session_scope
from backend.app.repositories.morning_preparation import MorningPreparationRepository
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyApplicationDecisionRepository, SqlAlchemyApplicationEventRepository,
    SqlAlchemyApplicationPolicyRepository, SqlAlchemyApplicationRepository,
    SqlAlchemyCandidateDocumentRepository, SqlAlchemyCandidateProfileRepository,
    SqlAlchemyEligibilityResultRepository, SqlAlchemyMatchEvaluationRepository,
    SqlAlchemyOpportunityRepository, SqlAlchemySearchProfileRepository,
    SqlAlchemySubmissionAttemptRepository, SqlAlchemyTaskRunRepository,
)
from backend.app.services.application_queue import ApplicationQueueService
from backend.app.services.applications import ApplicationService
from backend.app.services.morning_application import MorningApplicationService
from backend.app.tasks.dispatcher import PersistedTaskDispatcher
from backend.app.tasks.handlers import build_application_submission_handler
from tests.v2_builders import (
    EVIDENCE, OPPORTUNITY, OTHER_USER, USER, a_candidate_profile, a_decision,
    a_rendered_document, a_search_profile, an_autopilot_policy,
    an_eligibility_result, an_evaluation, an_opportunity,
)
from tests.v2_rows import a_user_row

NOW = datetime(2026, 10, 13, 8, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("ats,host,driver_key", [
    ("lever", "jobs.lever.co", "lever/1"),
    ("greenhouse", "boards.greenhouse.io", "greenhouse/1"),
])
@pytest.mark.parametrize("confirmed", [True, False])
async def test_exact_swiss_dossier_queues_one_fixture_attempt(
    db_session, ats: str, host: str, driver_key: str, confirmed: bool,
) -> None:
    connection = db_session.bind
    assert connection is not None
    factory = async_sessionmaker(
        bind=connection, expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    run_id = DiscoveryRunId(uuid4())
    target = f"https://{host}/fixture/role/apply"
    fingerprint = "a" * 64
    catalog = FormCapabilityCatalog(forms=(FormCapability(
        id=f"{ats}-fixture", ats=ats, employer="Fixture SA",
        target_url=target, driver_key=driver_key, form_fingerprint=fingerprint,
        allowed_required_fields=frozenset(), allowed_upload_slots=frozenset(),
        conditions_url="https://fixture.example/terms", review_id="fixture-review",
        reviewed_at=date(2026, 10, 13), access_decision="approved",
    ),))
    cv = a_rendered_document(storage_key=f"{ats}-cv")
    letter = a_rendered_document(
        storage_key=f"{ats}-letter",
        document_type=CandidateDocumentType.COVER_LETTER,
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
        await SqlAlchemyOpportunityRepository(session).upsert(
            an_opportunity(application_url=target))
        policy = await SqlAlchemyApplicationPolicyRepository(session).upsert(
            an_autopilot_policy())
        await SqlAlchemyApplicationDecisionRepository(session).upsert(
            a_decision(policy_id=policy.id))
        await SqlAlchemyMatchEvaluationRepository(session).upsert(an_evaluation())
        await SqlAlchemyEligibilityResultRepository(session).upsert(
            an_eligibility_result())
        documents = SqlAlchemyCandidateDocumentRepository(session)
        await documents.upsert(cv)
        await documents.upsert(letter)
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

    class FixtureBrowser:
        submit_calls = 0

        async def run(self, task):
            if task.kind is BrowserTaskKind.PREPARE:
                async with factory() as read_session:
                    assert await SqlAlchemyApplicationRepository(read_session).get(
                        USER, task.application_id) is not None
                return BrowserTaskResult(
                    outcome=BrowserTaskOutcome.COMPLETED,
                    driver_key=driver_key, form_fingerprint=fingerprint,
                )
            self.submit_calls += 1
            return BrowserTaskResult(
                outcome=(BrowserTaskOutcome.COMPLETED if confirmed
                         else BrowserTaskOutcome.STATE_UNKNOWN),
                driver_key=driver_key, form_fingerprint=fingerprint,
                submission_confirmed=confirmed,
                confirmation_reference="FIXTURE-RECEIPT" if confirmed else None,
            )

    browser = FixtureBrowser()
    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    registry.register(BrowserApplicationAdapter(browser))

    def applications(session: AsyncSession) -> ApplicationService:
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
            registry=registry, form_catalog=catalog,
        )

    def queue(session: AsyncSession) -> ApplicationQueueService:
        return ApplicationQueueService(
            SqlAlchemyApplicationRepository(session),
            SqlAlchemyApplicationPolicyRepository(session),
            SqlAlchemyOpportunityRepository(session),
            PersistedTaskDispatcher(SqlAlchemyTaskRunRepository(session), clock=lambda: NOW),
            live_enabled=True, form_catalog=catalog,
        )

    service = MorningApplicationService(
        factory, application_builder=applications, queue_builder=queue,
        task_dispatcher_builder=lambda session: PersistedTaskDispatcher(
            SqlAlchemyTaskRunRepository(session), clock=lambda: NOW),
        live_enabled=True,
    )
    assert await service.reconcile_ready() == 1
    assert await service.reconcile_ready() == 0
    with pytest.raises(LookupError):
        await service.prepare(OTHER_USER, run_id, OPPORTUNITY, now=NOW)
    prepared = await service.prepare(USER, run_id, OPPORTUNITY, now=NOW)
    assert prepared.state is ApplicationState.APPROVED
    assert prepared.form_capability_id == f"{ats}-fixture"
    assert {pin.document_type: pin.version_id for pin in prepared.pinned_documents} == {
        CandidateDocumentType.RESUME: cv.versions[0].id,
        CandidateDocumentType.COVER_LETTER: letter.versions[0].id,
    }
    assert (await service.prepare(USER, run_id, OPPORTUNITY, now=NOW)).id == prepared.id

    async with session_scope(factory, commit=False) as session:
        task_row = (await session.execute(select(TaskRunRow).where(
            TaskRunRow.kind == TaskKind.APPLICATION_SUBMISSION))).scalar_one()
        task = task_run_to_domain(task_row)
    handler = build_application_submission_handler(
        applications, session_factory=factory,
        live_submissions_enabled=True, clock=lambda: NOW,
    )
    async with factory() as handler_session:
        await handler(task, handler_session)
    async with session_scope(factory, commit=False) as session:
        current = await SqlAlchemyApplicationRepository(session).get(USER, prepared.id)
        attempts = (await session.execute(select(func.count()).select_from(
            SubmissionAttemptRow))).scalar_one()
        apps = (await session.execute(select(func.count()).select_from(
            ApplicationRow))).scalar_one()
    assert current is not None
    assert current.state is (ApplicationState.SUBMITTED if confirmed
                             else ApplicationState.SUBMISSION_STATE_UNKNOWN)
    assert apps == attempts == browser.submit_calls == 1


@pytest.mark.asyncio
async def test_two_committed_runs_for_same_offer_create_one_application_and_queue(
    db_engine,
) -> None:
    """Independent connections serialize a same-owner, same-offer handoff."""
    factory = create_session_factory(db_engine)
    run_ids = (DiscoveryRunId(uuid4()), DiscoveryRunId(uuid4()))
    target = "https://jobs.lever.co/fixture/role/apply"
    fingerprint = "a" * 64
    catalog = FormCapabilityCatalog(forms=(FormCapability(
        id="lever-fixture", ats="lever", employer="Fixture SA",
        target_url=target, driver_key="lever/1", form_fingerprint=fingerprint,
        allowed_required_fields=frozenset(), allowed_upload_slots=frozenset(),
        conditions_url="https://fixture.example/terms", review_id="fixture-review",
        reviewed_at=date(2026, 10, 13), access_decision="approved",
    ),))
    cv = a_rendered_document(storage_key="concurrent-cv")
    letter = a_rendered_document(
        storage_key="concurrent-letter",
        document_type=CandidateDocumentType.COVER_LETTER,
        content=CoverLetterDocument(
            signature="Fixture Candidate",
            body=(EvidenceBackedText(text="Built a platform", evidence_ids=(EVIDENCE,)),),
        ),
    )

    class FixtureBrowser:
        prepare_calls = 0

        async def run(self, task):
            if task.kind is BrowserTaskKind.PREPARE:
                self.prepare_calls += 1
            return BrowserTaskResult(
                outcome=BrowserTaskOutcome.COMPLETED,
                driver_key="lever/1", form_fingerprint=fingerprint,
            )

    browser = FixtureBrowser()
    registry = ApplicationAdapterRegistry(fallback=GenericManualAdapter())
    registry.register(BrowserApplicationAdapter(browser))

    def applications(session: AsyncSession) -> ApplicationService:
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
            registry=registry, form_catalog=catalog,
        )

    def queue(session: AsyncSession) -> ApplicationQueueService:
        return ApplicationQueueService(
            SqlAlchemyApplicationRepository(session),
            SqlAlchemyApplicationPolicyRepository(session),
            SqlAlchemyOpportunityRepository(session),
            PersistedTaskDispatcher(SqlAlchemyTaskRunRepository(session), clock=lambda: NOW),
            live_enabled=True, form_catalog=catalog,
        )

    try:
        async with session_scope(factory) as session:
            session.add(a_user_row(id=USER))
            await session.flush()
            await SqlAlchemyCandidateProfileRepository(session).upsert(a_candidate_profile())
            await SqlAlchemySearchProfileRepository(session).upsert(a_search_profile())
            await SqlAlchemyOpportunityRepository(session).upsert(
                an_opportunity(application_url=target))
            policy = await SqlAlchemyApplicationPolicyRepository(session).upsert(
                an_autopilot_policy(max_applications_per_day=1))
            await SqlAlchemyApplicationDecisionRepository(session).upsert(
                a_decision(policy_id=policy.id))
            await SqlAlchemyMatchEvaluationRepository(session).upsert(an_evaluation())
            await SqlAlchemyEligibilityResultRepository(session).upsert(
                an_eligibility_result())
            documents = SqlAlchemyCandidateDocumentRepository(session)
            await documents.upsert(cv)
            await documents.upsert(letter)
            for run_id in run_ids:
                session.add(DiscoveryRunRow(
                    id=run_id, user_id=USER, search_profile_id=a_search_profile().id,
                    country="CH", trigger="MORNING", status="SUCCEEDED",
                    started_at=NOW, finished_at=NOW, seen_count=1, matched_count=1,
                    source_health=[], warning_codes=[],
                ))
            await session.flush()
            ledger = MorningPreparationRepository(session)
            for run_id in run_ids:
                await ledger.start_or_get(USER, run_id, OPPORTUNITY, now=NOW)
                await ledger.record_assessment(
                    USER, run_id, OPPORTUNITY, decision_id=a_decision().id, now=NOW)
                await ledger.record_document_version(
                    USER, run_id, OPPORTUNITY, CandidateDocumentType.RESUME.value,
                    cv.versions[0].id, now=NOW)
                await ledger.record_document_version(
                    USER, run_id, OPPORTUNITY, CandidateDocumentType.COVER_LETTER.value,
                    letter.versions[0].id, now=NOW)

        service = MorningApplicationService(
            factory, application_builder=applications, queue_builder=queue,
            live_enabled=True,
        )
        results = await asyncio.gather(*(
            service.prepare(USER, run_id, OPPORTUNITY, now=NOW)
            for run_id in run_ids
        ))
        assert results[0].id == results[1].id
        assert results[0].state is ApplicationState.APPROVED
        assert browser.prepare_calls == 1
        async with session_scope(factory, commit=False) as session:
            app_count = (await session.execute(select(func.count()).select_from(
                ApplicationRow))).scalar_one()
            queue_count = (await session.execute(select(func.count()).select_from(
                TaskRunRow).where(
                    TaskRunRow.kind == TaskKind.APPLICATION_SUBMISSION))).scalar_one()
            attempt_count = (await session.execute(select(func.count()).select_from(
                SubmissionAttemptRow))).scalar_one()
        assert (app_count, queue_count, attempt_count) == (1, 1, 0)
    finally:
        # This is the dedicated disposable test database, never the candidate DB.
        async with db_engine.begin() as connection:
            await connection.execute(text(
                "TRUNCATE users, opportunities RESTART IDENTITY CASCADE"))
