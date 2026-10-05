"""Typed result of the local reversible candidate workflow."""

from enum import StrEnum
from typing import Any


class LocalAutopilotNextAction(StrEnum):
    QUEUE_BROWSER_SUBMISSION = "QUEUE_BROWSER_SUBMISSION"
    REVIEW_DOCUMENT = "REVIEW_DOCUMENT"
    REVIEW_APPLICATION = "REVIEW_APPLICATION"
    NONE = "NONE"


class LocalAutopilotStopReason(StrEnum):
    DECISION_NOT_ACTIONABLE = "DECISION_NOT_ACTIONABLE"
    DOCUMENT_NOT_USABLE = "DOCUMENT_NOT_USABLE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


class LocalAutopilotReport:
    """A local run report; submission is intentionally not part of this object."""

    def __init__(self, *, assessment: Any, decision: Any,
                 resume: Any = None, cover_letter: Any = None,
                 application: Any = None,
                 next_action: LocalAutopilotNextAction = LocalAutopilotNextAction.NONE,
                 stop_reason: LocalAutopilotStopReason | None = None) -> None:
        self.assessment = assessment
        self.decision = decision
        self.resume = resume
        self.cover_letter = cover_letter
        self.application = application
        self.next_action = next_action
        self.stop_reason = stop_reason
