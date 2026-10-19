"""Reversible, owner-scoped Morning Run assessment and document handoff."""

from datetime import datetime

from backend.app.domain.decision import ApplicationDecisionKind
from backend.app.domain.identifiers import DiscoveryRunId, OpportunityId, UserId
from backend.app.domain.morning_preparation import Preparation, PreparationStage
from backend.app.domain.task import TaskKind, TaskSpec
from backend.app.repositories.morning_preparation import MorningPreparationRepository
from backend.app.services.application_decisions import ApplicationDecisionService
from backend.app.services.assessment import AssessmentService
from backend.app.tasks.dispatcher import TaskDispatcher


class MorningPreparationService:
    """Assess a persisted run member and enqueue only reversible document work."""

    def __init__(self, *, ledger: MorningPreparationRepository,
                 assessor: AssessmentService, decider: ApplicationDecisionService,
                 dispatcher: TaskDispatcher) -> None:
        self._ledger = ledger
        self._assessor = assessor
        self._decider = decider
        self._dispatcher = dispatcher

    async def assess_and_decide(self, user_id: UserId, run_id: DiscoveryRunId,
                                opportunity_id: OpportunityId, *,
                                now: datetime) -> Preparation:
        current = await self._ledger.get(user_id, run_id, opportunity_id)
        if current is None:
            raise LookupError("owner's discovery member not found")
        if current.stage is not PreparationStage.DISCOVERED:
            return current

        await self._assessor.evaluate(user_id, opportunity_id, now=now)
        decision = await self._decider.decide(user_id, opportunity_id, now=now)
        if decision.kind in {ApplicationDecisionKind.SKIP, ApplicationDecisionKind.SAVE}:
            return await self._ledger.advance(
                user_id, run_id, opportunity_id, PreparationStage.SKIPPED,
                now=now, reason_code="DECISION_NOT_ACTIONABLE")
        if decision.kind is ApplicationDecisionKind.REQUIRE_REVIEW:
            return await self._ledger.advance(
                user_id, run_id, opportunity_id, PreparationStage.REQUIRES_HUMAN,
                now=now, reason_code="ELIGIBILITY_REVIEW_REQUIRED")
        if decision.kind not in {ApplicationDecisionKind.PREPARE,
                                 ApplicationDecisionKind.AUTO_APPLY}:
            return await self._ledger.advance(
                user_id, run_id, opportunity_id, PreparationStage.REQUIRES_HUMAN,
                now=now, reason_code="DECISION_REQUIRES_HUMAN")

        matched = await self._ledger.record_assessment(
            user_id, run_id, opportunity_id, decision_id=decision.id, now=now)
        await self._dispatcher.enqueue(TaskSpec(
            kind=TaskKind.DOCUMENT_GENERATION, user_id=user_id,
            idempotency_key=f"morning-docs:{user_id}:{run_id}:{opportunity_id}",
            payload={"discovery_run_id": str(run_id),
                     "opportunity_id": str(opportunity_id)},
        ))
        return matched
