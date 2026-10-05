"""Local reversible orchestration from assessment to prepared application."""

from datetime import datetime
from typing import Any, Protocol

from backend.app.domain.autopilot import (
    LocalAutopilotNextAction,
    LocalAutopilotReport,
    LocalAutopilotStopReason,
)
from backend.app.domain.decision import ApplicationDecisionKind
from backend.app.domain.documents import CandidateDocumentType


class _Assessor(Protocol):
    async def evaluate(self, user_id: Any, opportunity_id: Any, *, now: datetime) -> Any: ...


class _Decider(Protocol):
    async def decide(self, user_id: Any, opportunity_id: Any, *, now: datetime) -> Any: ...


class _Applications(Protocol):
    async def create(self, user_id: Any, opportunity_id: Any, *, now: datetime) -> Any: ...
    async def prepare(self, user_id: Any, app_id: Any, *, now: datetime) -> Any: ...


class _Documents(Protocol):
    async def generate(self, user_id: Any, opportunity_id: Any,
                       document_type: CandidateDocumentType, *, now: datetime,
                       language: Any = None) -> Any: ...


class LocalAutopilotService:
    """Compose existing domain services without duplicating their safety gates."""

    def __init__(self, *, assessor: _Assessor, decider: _Decider,
                 applications: _Applications, documents: _Documents) -> None:
        self._assessor = assessor
        self._decider = decider
        self._applications = applications
        self._documents = documents

    async def run(self, user_id: Any, opportunity_id: Any, *, now: datetime,
                  language: Any = None) -> LocalAutopilotReport:
        assessment = await self._assessor.evaluate(user_id, opportunity_id, now=now)
        decision = await self._decider.decide(user_id, opportunity_id, now=now)
        if decision.kind in {ApplicationDecisionKind.SKIP, ApplicationDecisionKind.SAVE}:
            return LocalAutopilotReport(
                assessment=assessment, decision=decision,
                stop_reason=LocalAutopilotStopReason.DECISION_NOT_ACTIONABLE)

        application = await self._applications.create(user_id, opportunity_id, now=now)
        resume = await self._documents.generate(
            user_id, opportunity_id, CandidateDocumentType.RESUME,
            now=now, language=language)
        cover_letter = await self._documents.generate(
            user_id, opportunity_id, CandidateDocumentType.COVER_LETTER,
            now=now, language=language)
        if not self._usable(resume) or not self._usable(cover_letter):
            return LocalAutopilotReport(
                assessment=assessment, decision=decision, resume=resume,
                cover_letter=cover_letter, application=application,
                next_action=LocalAutopilotNextAction.REVIEW_DOCUMENT,
                stop_reason=LocalAutopilotStopReason.DOCUMENT_NOT_USABLE)

        application = await self._applications.prepare(
            user_id, application.id, now=now)
        if application.state == "APPROVED":
            action = LocalAutopilotNextAction.QUEUE_BROWSER_SUBMISSION
            reason = None
        elif application.state == "REQUIRES_HUMAN":
            action = LocalAutopilotNextAction.REVIEW_APPLICATION
            reason = LocalAutopilotStopReason.HUMAN_REQUIRED
        else:
            action = LocalAutopilotNextAction.REVIEW_APPLICATION
            reason = None
        return LocalAutopilotReport(
            assessment=assessment, decision=decision, resume=resume,
            cover_letter=cover_letter, application=application,
            next_action=action, stop_reason=reason)

    @staticmethod
    def _usable(document: Any) -> bool:
        versions = tuple(getattr(document, "versions", ()))
        last = versions[-1] if versions else None
        return last is not None and last.status.value in {"VALIDATED", "RENDERED"}
