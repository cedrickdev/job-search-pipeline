"""Recording the real world — outcomes are observed, never allowed to touch execution state.

The "observe" link of the spine, and the module that carries Phase 15's foundational
distinction into the service layer (§2, §84): an `ApplicationOutcome` is a fact about the
*hiring process* — an acknowledgement, a screen, an interview round, an offer, a rejection —
and recording one **never** touches the Phase 12 `ApplicationState`. This service holds an
`ApplicationRepository` only to *read* it (the ownership check below); it has no method, and
no import, that could move an application to `FAILED`. A recruiter's "no" is a `REJECTED`
outcome and nothing more.

Three operations, each mapping to one domain move so the append-only history stays auditable:

- **record** a milestone — idempotent by the outcome's derived id, so a double-clicked "mark
  as interviewed" collapses onto one row while two genuine rounds on different days stay two;
- **correct** a milestone — a *new* outcome that supersedes the mistaken one (pointing back
  with `supersedes_id`) while the predecessor is flipped to `SUPERSEDED`, never deleted;
- **retract** a milestone — a status flip to `RETRACTED`, the row surviving so "we believed
  this, then took it back" stays in the record.

Every method takes `user_id` from the authenticated session and verifies the application
belongs to that account before writing — the load is the authorization check, so an outcome
can never be recorded against another user's application, and a foreign application reads as
absent (`APPLICATION_NOT_FOUND`) exactly as a missing one does (§7-9, §46, §64).
"""
from datetime import datetime

from backend.app.career.errors import CareerError, CareerErrorCode
from backend.app.domain.identifiers import (
    ApplicationId,
    ApplicationOutcomeId,
    UserId,
    application_outcome_id,
)
from backend.app.domain.outcome import (
    ApplicationOutcome,
    OutcomeKind,
    OutcomeSource,
    OutcomeStatus,
    build_outcome_key,
)
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    ApplicationOutcomeRepository,
    ApplicationRepository,
)


class OutcomeService:
    """Record, correct, retract and list one account's real-world application outcomes.

    Holds the outcome store it writes and the application store it reads for ownership. No
    clock in the constructor — each method takes `now`, so a single request's `recorded_at`
    stamps agree — the convention every V2 service keeps.
    """

    def __init__(self, outcomes: ApplicationOutcomeRepository,
                 applications: ApplicationRepository) -> None:
        self._outcomes = outcomes
        self._applications = applications

    async def record(self, user_id: UserId, application_id: ApplicationId, *,
                     kind: OutcomeKind, occurred_at: datetime,
                     source: OutcomeSource = OutcomeSource.MANUAL_USER,
                     detail: str | None = None,
                     now: datetime) -> ApplicationOutcome:
        """Record one milestone, idempotent by its derived id (§46, §81).

        The application is loaded first — the ownership check, so recording against another
        account's application raises `APPLICATION_NOT_FOUND` rather than writing. The
        `outcome_key` is composed by the domain from the fact itself, so re-recording the same
        once-only milestone collapses onto one row and a repeatable one (a second interview on
        another day) is a distinct row. `occurred_at` is when it happened in the world;
        `recorded_at` is `now`, kept apart because the gap is signal (§8).
        """
        await self._require_application(user_id, application_id)
        outcome_key = build_outcome_key(kind=kind, occurred_at=occurred_at)
        return await self._outcomes.upsert(ApplicationOutcome(
            id=application_outcome_id(application_id, outcome_key),
            user_id=user_id,
            application_id=application_id,
            kind=kind,
            source=source,
            status=OutcomeStatus.EFFECTIVE,
            outcome_key=outcome_key,
            occurred_at=occurred_at,
            recorded_at=now,
            detail=detail))

    async def correct(self, user_id: UserId, outcome_id: ApplicationOutcomeId, *,
                      kind: OutcomeKind, occurred_at: datetime,
                      detail: str | None = None,
                      now: datetime) -> ApplicationOutcome:
        """Supersede a mistaken outcome with a corrected one (§9, §64).

        The predecessor is loaded owner-scoped and must still be `EFFECTIVE` — correcting an
        already superseded or retracted outcome raises `OUTCOME_NOT_EFFECTIVE`, which also
        makes the operation naturally single-shot. The correction is a new outcome on the same
        application, keyed on `correction:{predecessor}` so re-issuing the same correction
        after a failed flush lands on the one row; the predecessor is then flipped to
        `SUPERSEDED`. Both writes are one request's unit of work, so the pair is atomic.
        """
        predecessor = await self._require_effective_outcome(user_id, outcome_id)
        correction_key = build_outcome_key(
            kind=kind, occurred_at=occurred_at, supersedes_id=outcome_id)
        correction = await self._outcomes.upsert(ApplicationOutcome(
            id=application_outcome_id(predecessor.application_id, correction_key),
            user_id=user_id,
            application_id=predecessor.application_id,
            kind=kind,
            source=predecessor.source,
            status=OutcomeStatus.EFFECTIVE,
            outcome_key=correction_key,
            occurred_at=occurred_at,
            recorded_at=now,
            supersedes_id=outcome_id,
            detail=detail))
        await self._outcomes.upsert(predecessor.superseded(at=now))
        return correction

    async def retract(self, user_id: UserId, outcome_id: ApplicationOutcomeId, *,
                      now: datetime) -> ApplicationOutcome:
        """Take back an outcome recorded in error — a status flip to `RETRACTED` (§64).

        Loaded owner-scoped and required `EFFECTIVE`; the row stays so the retraction is
        itself auditable, and `recorded_at` advances to `now` because the retraction is the
        latest thing known about the fact.
        """
        outcome = await self._require_effective_outcome(user_id, outcome_id)
        return await self._outcomes.upsert(outcome.retracted(at=now))

    async def timeline(self, user_id: UserId, application_id: ApplicationId, *,
                       limit: int = DEFAULT_LIMIT) -> tuple[ApplicationOutcome, ...]:
        """One application's outcomes oldest-first — every status, for a surface to render.

        Ownership is checked first so a foreign or missing application raises
        `APPLICATION_NOT_FOUND` rather than returning an empty list a caller could not tell
        from "no outcomes yet". Superseded and retracted rows are returned alongside effective
        ones: the timeline keeps the correction history; only the analytics layer filters to
        effective rows when it counts.
        """
        await self._require_application(user_id, application_id)
        return await self._outcomes.list_for_application(
            user_id, application_id, limit=limit)

    async def _require_application(self, user_id: UserId,
                                   application_id: ApplicationId) -> None:
        """Refuse unless the application is this account's — the load is the check."""
        if await self._applications.get(user_id, application_id) is None:
            raise CareerError(CareerErrorCode.APPLICATION_NOT_FOUND, "no such application")

    async def _require_effective_outcome(self, user_id: UserId,
                                         outcome_id: ApplicationOutcomeId
                                         ) -> ApplicationOutcome:
        """Load an outcome owner-scoped and require it still be believed, or refuse."""
        outcome = await self._outcomes.get(user_id, outcome_id)
        if outcome is None:
            raise CareerError(CareerErrorCode.OUTCOME_NOT_FOUND, "no such outcome")
        if not outcome.is_effective:
            raise CareerError(
                CareerErrorCode.OUTCOME_NOT_EFFECTIVE,
                f"an outcome in status {outcome.status.value} cannot be modified")
        return outcome
