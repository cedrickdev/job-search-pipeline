"""Resume evidence-grounded CV and letter generation at committed checkpoints."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.domain.decision import ApplicationDecisionKind
from backend.app.domain.documents import CandidateDocumentType, DocumentStatus
from backend.app.domain.identifiers import DiscoveryRunId, OpportunityId, UserId
from backend.app.domain.morning_preparation import Preparation, PreparationStage
from backend.app.domain.task import TaskKind, TaskSpec
from backend.app.infrastructure.database.engine import session_scope
from backend.app.repositories.morning_preparation import MorningPreparationRepository
from backend.app.services.documents import DocumentService
from backend.app.tasks.dispatcher import TaskDispatcher


class MorningDocumentsService:
    """Commit the CV before attempting the letter; retry reads exact checkpoint IDs."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *,
        document_builder: Callable[[AsyncSession, UserId], Awaitable[DocumentService]],
        assessor_builder: Callable[[AsyncSession], Any],
        decider_builder: Callable[[AsyncSession], Any],
        dispatcher_builder: Callable[[AsyncSession], TaskDispatcher] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._document_builder = document_builder
        self._assessor_builder = assessor_builder
        self._decider_builder = decider_builder
        self._dispatcher_builder = dispatcher_builder

    async def prepare(self, user_id: UserId, run_id: DiscoveryRunId,
                      opportunity_id: OpportunityId, *, now: datetime) -> Preparation:
        # Each document and its ledger checkpoint commit atomically. A worker crash
        # after the CV commit cannot cause a second CV version on redelivery.
        for document_type, expected_stage in (
            (CandidateDocumentType.RESUME, PreparationStage.MATCHED),
            (CandidateDocumentType.COVER_LETTER, PreparationStage.CV_RENDERED),
        ):
            async with session_scope(self._session_factory) as session:
                ledger = MorningPreparationRepository(session)
                current = await ledger.lock(user_id, run_id, opportunity_id)
                if current.stage in {PreparationStage.DOCUMENTS_RENDERED,
                                     PreparationStage.REQUIRES_HUMAN,
                                     PreparationStage.SKIPPED,
                                     PreparationStage.FAILED}:
                    return current
                if current.stage is not expected_stage:
                    continue

                await self._assessor_builder(session).evaluate(
                    user_id, opportunity_id, now=now)
                decision = await self._decider_builder(session).decide(
                    user_id, opportunity_id, now=now)
                if decision.kind not in {ApplicationDecisionKind.PREPARE,
                                         ApplicationDecisionKind.AUTO_APPLY}:
                    return await ledger.advance(
                        user_id, run_id, opportunity_id,
                        PreparationStage.REQUIRES_HUMAN, now=now,
                        reason_code="POLICY_CHANGED")

                documents = await self._document_builder(session, user_id)
                result = await documents.generate(
                    user_id, opportunity_id, document_type, now=now)
                version = result.versions[-1]
                if (result.user_id != user_id or result.opportunity_id != opportunity_id
                        or result.document_type is not document_type):
                    raise ValueError("document does not belong to the task owner and opportunity")
                if version.status is not DocumentStatus.RENDERED or version.artifact is None:
                    return await ledger.advance(
                        user_id, run_id, opportunity_id,
                        PreparationStage.REQUIRES_HUMAN, now=now,
                        reason_code="DOCUMENT_GUARD_REJECTED")
                recorded = await ledger.record_document_version(
                    user_id, run_id, opportunity_id, document_type.value,
                    version.id, now=now)
                if (recorded.stage is PreparationStage.DOCUMENTS_RENDERED
                        and self._dispatcher_builder is not None):
                    await self._dispatcher_builder(session).enqueue(TaskSpec(
                        kind=TaskKind.APPLICATION_PREPARATION,
                        idempotency_key=(f"morning-application:{user_id}:"
                                         f"{run_id}:{opportunity_id}"),
                        user_id=user_id,
                        payload={"discovery_run_id": str(run_id),
                                 "opportunity_id": str(opportunity_id)},
                        max_attempts=2,
                    ))

        async with session_scope(self._session_factory, commit=False) as session:
            completed = await MorningPreparationRepository(session).get(
                user_id, run_id, opportunity_id)
            if completed is None:
                raise LookupError("owner's preparation not found")
            return completed
