"""`/api/v2/onboarding`: which step is left, and the stamp that ends it.

Two routes, and the split between them is the whole design. `GET` reports state and
`POST /complete` changes it — the completion stamp is a write, so it cannot be a
side effect of the screen that displays progress. Nothing about the `GET` mutates
anything the client can observe.

`POST /complete` is the one route that re-reads what the client claims to have done.
It takes no body at all: there is nothing a caller could send that would make it
succeed, because the profile and the active search are read from the database. A
request that skipped a step gets 409 with the counts, which is the same information
the `GET` reports — so the button and the refusal can never disagree.
"""
from fastapi import APIRouter

from backend.app.api.dependencies import CurrentSession, Now, Onboarding
from backend.app.api.schemas import (
    AccountResponse,
    OnboardingStateResponse,
)

router = APIRouter(prefix="/onboarding", tags=["v2-onboarding"])


@router.get("", response_model=OnboardingStateResponse)
async def read_state(current: CurrentSession,
                     service: Onboarding) -> OnboardingStateResponse:
    """What has been saved so far, and whether finishing would succeed."""
    return OnboardingStateResponse.of(await service.state(current.user))


@router.post("/complete", response_model=AccountResponse)
async def complete(current: CurrentSession, service: Onboarding,
                   instant: Now) -> AccountResponse:
    """Record that onboarding is done, or refuse with 409 and say what is missing.

    Returns the account rather than the onboarding state, because the stamp lives on
    the account and the client's cached copy of it is now stale — answering with the
    new account is what lets the frontend update without a second request.
    """
    return AccountResponse.of(await service.complete(current.user, now=instant))
