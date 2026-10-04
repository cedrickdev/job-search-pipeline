"""Identity of one committed, owner-scoped submission attempt.

The browser task never carries candidate content or authority. A reservation is
created by the application service and committed before an irreversible action.
"""
from dataclasses import dataclass

from backend.app.domain.identifiers import ApplicationId, SubmissionAttemptId, UserId


@dataclass(frozen=True)
class SubmissionReservation:
    user_id: UserId
    application_id: ApplicationId
    attempt_id: SubmissionAttemptId
