"""`/api/v2`: recording the real world against an application, over HTTP.

Phase 15's foundational separation is enforced at this boundary by *absence*: an outcome is a
fact about the hiring process — an acknowledgement, a screen, an interview, an offer, a
rejection — and nothing here can touch the Phase 12 execution lifecycle. No route returns or
accepts an `ApplicationState`; a recruiter's "no" is a `REJECTED` outcome and never a failed
execution (§2, §8, §46, §84).

The owner is never in the path or the body — it is the account resolved from the session, so no
request can record, read, correct or retract against another user's application
(docs/ENGINEERING_STANDARDS.md §Security). A foreign or missing application reads as
`APPLICATION_NOT_FOUND` (404), and a foreign or missing outcome as `OUTCOME_NOT_FOUND` (404), so
an id cannot be probed. `occurred_at` arrives as a timezone-aware instant validated at this
boundary (a naive value is a 422, not a domain 500); `recorded_at` is the platform's to stamp.
"""
from fastapi import APIRouter, status

from backend.app.api.dependencies import CurrentSession, Now, Outcomes
from backend.app.api.schemas import (
    ApplicationOutcomeListResponse,
    ApplicationOutcomeResponse,
    CorrectOutcomeRequest,
    RecordOutcomeRequest,
)
from backend.app.domain.identifiers import ApplicationId, ApplicationOutcomeId

router = APIRouter(tags=["v2-outcomes"])


@router.post("/applications/{application_id}/outcomes",
             response_model=ApplicationOutcomeResponse,
             status_code=status.HTTP_201_CREATED)
async def record_outcome(application_id: ApplicationId, body: RecordOutcomeRequest,
                         current: CurrentSession, service: Outcomes,
                         instant: Now) -> ApplicationOutcomeResponse:
    """Record one real-world milestone against an application (§46, §81).

    201, because it records an outcome. Idempotent by the outcome's derived id: a double-clicked
    "mark as interviewed" collapses onto one row, while two genuine rounds on different days stay
    two. The application is loaded owner-first, so a foreign or missing one is a 404
    (`application_not_found`) rather than a write. This can never move the application's execution
    state — a `REJECTED` outcome leaves the Phase 12 lifecycle untouched.
    """
    outcome = await service.record(
        current.user.id, application_id, kind=body.kind, occurred_at=body.occurred_at,
        source=body.source, detail=body.detail, now=instant)
    return ApplicationOutcomeResponse.of(outcome)


@router.get("/applications/{application_id}/outcomes",
            response_model=ApplicationOutcomeListResponse)
async def list_outcomes(application_id: ApplicationId, current: CurrentSession,
                        service: Outcomes) -> ApplicationOutcomeListResponse:
    """One application's outcomes oldest-first — every status, so corrections stay in the record.

    Ownership is checked first, so a foreign or missing application is a 404 rather than an empty
    list a caller could not tell from "no outcomes yet". Superseded and retracted rows travel
    alongside effective ones: the timeline keeps the whole history, and only analytics filters to
    effective rows when it counts.
    """
    outcomes = await service.timeline(current.user.id, application_id)
    return ApplicationOutcomeListResponse.of(outcomes)


@router.post("/outcomes/{outcome_id}/correct",
             response_model=ApplicationOutcomeResponse,
             status_code=status.HTTP_201_CREATED)
async def correct_outcome(outcome_id: ApplicationOutcomeId, body: CorrectOutcomeRequest,
                          current: CurrentSession, service: Outcomes,
                          instant: Now) -> ApplicationOutcomeResponse:
    """Supersede a mistaken outcome with a corrected one (§9, §64).

    201, because the correction is a new outcome pointing back at the predecessor; the predecessor
    is flipped to `SUPERSEDED`, never deleted. The predecessor must still be effective — correcting
    an already superseded or retracted outcome is a 409 (`outcome_not_effective`), which also makes
    the operation single-shot. A foreign or missing outcome is a 404.
    """
    outcome = await service.correct(
        current.user.id, outcome_id, kind=body.kind, occurred_at=body.occurred_at,
        detail=body.detail, now=instant)
    return ApplicationOutcomeResponse.of(outcome)


@router.post("/outcomes/{outcome_id}/retract",
             response_model=ApplicationOutcomeResponse)
async def retract_outcome(outcome_id: ApplicationOutcomeId, current: CurrentSession,
                          service: Outcomes, instant: Now) -> ApplicationOutcomeResponse:
    """Take back an outcome recorded in error — a status flip to `RETRACTED` (§64).

    The row survives so "we believed this, then took it back" stays auditable. The outcome must
    still be effective (a 409 `outcome_not_effective` otherwise), and a foreign or missing one is a
    404. No execution state is touched.
    """
    outcome = await service.retract(current.user.id, outcome_id, now=instant)
    return ApplicationOutcomeResponse.of(outcome)
