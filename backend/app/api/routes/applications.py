"""`/api/v2/applications`: the lifecycle of an application, over HTTP.

The owner is never in the path or the body — it is the account resolved from the
session, so no request can open, read, prepare, approve, submit or cancel another
user's application (docs/ENGINEERING_STANDARDS.md §Security). Reads answer 404 for
"no such application" and "not yours" alike, so an id cannot be probed.

Every write is a `POST` that returns the application's *new state* rather than a bare
acknowledgement, because the state is exactly what a client acts on next: a `prepare`
that comes back `READY_FOR_REVIEW` shows an "Approve & Submit" control, one that comes
back `REQUIRES_HUMAN` shows the hand-off, and a `submit` that comes back
`SUBMITTED` shows a receipt. The transitions are the service's to police — the routes
only name the operation and hand it the request's instant, so a single call's gate
window and timestamps agree.
"""
from fastapi import APIRouter

from backend.app.api.dependencies import Applications, CurrentSession, Now
from backend.app.api.schemas import (
    ApplicationEventListResponse,
    ApplicationListResponse,
    ApplicationResponse,
    CreateApplicationRequest,
)
from backend.app.domain.identifiers import ApplicationId

router = APIRouter(tags=["v2-applications"])


@router.post("/applications", response_model=ApplicationResponse)
async def create_application(body: CreateApplicationRequest, current: CurrentSession,
                             service: Applications, instant: Now) -> ApplicationResponse:
    """Open an application for a posting from its stored decision (§2-3, §36).

    Idempotent: opening one twice for the same target returns the first rather than a
    second (the id is derived from the target and channel). A 409 comes back only when
    the decision is missing (decide first) or the target was already submitted (a
    genuine duplicate); otherwise the current application is returned.
    """
    application = await service.create(current.user.id, body.opportunity_id,
                                       now=instant)
    return ApplicationResponse.of(application)


@router.get("/applications", response_model=ApplicationListResponse)
async def list_applications(current: CurrentSession,
                            service: Applications) -> ApplicationListResponse:
    """This account's applications, most recently updated first."""
    applications = await service.list(current.user.id)
    return ApplicationListResponse.of(applications)


@router.get("/applications/{application_id}", response_model=ApplicationResponse)
async def read_application(application_id: ApplicationId, current: CurrentSession,
                           service: Applications) -> ApplicationResponse:
    """One application, or 404 if it is not this account's."""
    application = await service.get(current.user.id, application_id)
    return ApplicationResponse.of(application)


@router.post("/applications/{application_id}/prepare",
             response_model=ApplicationResponse)
async def prepare_application(application_id: ApplicationId, current: CurrentSession,
                              service: Applications,
                              instant: Now) -> ApplicationResponse:
    """Prepare materials and route by the gate (auto-approve, review, human, block).

    Reversible and safe to retry (§33) — it never submits. The returned state says
    what happens next.
    """
    application = await service.prepare(current.user.id, application_id, now=instant)
    return ApplicationResponse.of(application)


@router.post("/applications/{application_id}/approve",
             response_model=ApplicationResponse)
async def approve_application(application_id: ApplicationId, current: CurrentSession,
                              service: Applications,
                              instant: Now) -> ApplicationResponse:
    """A human's approval of a prepared application (§52).

    Only a READY_FOR_REVIEW application can be approved; anything else is a 409.
    """
    application = await service.approve(current.user.id, application_id, now=instant)
    return ApplicationResponse.of(application)


@router.post("/applications/{application_id}/submit",
             response_model=ApplicationResponse)
async def submit_application(application_id: ApplicationId, current: CurrentSession,
                             service: Applications,
                             instant: Now) -> ApplicationResponse:
    """Submit an approved application — the irreversible boundary (§1, §5, §80-88).

    The gate is re-evaluated against the current policy first, so an application
    approved this morning is refused this afternoon if the policy became MANUAL. An
    exhausted rate budget is a 429; a duplicate is a 409; an ambiguous send comes back
    as SUBMISSION_STATE_UNKNOWN rather than a false success.
    """
    application = await service.submit(current.user.id, application_id, now=instant)
    return ApplicationResponse.of(application)


@router.post("/applications/{application_id}/cancel",
             response_model=ApplicationResponse)
async def cancel_application(application_id: ApplicationId, current: CurrentSession,
                             service: Applications,
                             instant: Now) -> ApplicationResponse:
    """Abandon an application before it reaches the employer.

    Refused with a 409 once an application is submitted — that is a withdrawal, a
    different act — or already terminal.
    """
    application = await service.cancel(current.user.id, application_id, now=instant)
    return ApplicationResponse.of(application)


@router.get("/applications/{application_id}/events",
            response_model=ApplicationEventListResponse)
async def list_application_events(application_id: ApplicationId,
                                  current: CurrentSession,
                                  service: Applications
                                  ) -> ApplicationEventListResponse:
    """One application's append-only audit trail, oldest first (§41).

    404 when the application is not this account's — the ownership check is the same
    read the other endpoints use, so the trail cannot be read by guessing an id.
    """
    events = await service.events(current.user.id, application_id)
    return ApplicationEventListResponse.of(events)
