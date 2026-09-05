"""`/api/v2/me`: the candidate profile and the saved searches.

Every path here begins with `me`, and that is the authorization model rather than a
naming style. There is no `/users/{id}/profile` to protect, because the owner is
never in the path — it comes from the session, so a request cannot ask about
somebody else's data even incorrectly
(docs/ENGINEERING_STANDARDS.md §Security: user-owned data is authorization-scoped).

`PUT` for the profile and not `POST`: the account has exactly one, its id is derived
from the account, and the write is idempotent — which is what `PUT` means. The saved
searches are a genuine collection, so they get the usual four.

The `{search_profile_id}` routes do not check ownership themselves. They pass the id
and the session's user to the service, whose repository scopes the query, and a
search belonging to another account comes back absent and turns into the same 404 as
one that does not exist. A check in the handler would be a second place for the rule
to live, and the two would eventually disagree.
"""
from fastapi import APIRouter, Query, status

from backend.app.api.dependencies import CurrentSession, Now, Onboarding
from backend.app.api.errors import ApiError
from backend.app.api.schemas import (
    CandidateProfileResponse,
    SearchProfileListResponse,
    SearchProfileResponse,
)
from backend.app.domain.identifiers import SearchProfileId
from backend.app.services.onboarding import CandidateProfileDraft, SearchProfileDraft

router = APIRouter(prefix="/me", tags=["v2-me"])


@router.get("/profile", response_model=CandidateProfileResponse)
async def read_profile(current: CurrentSession,
                       service: Onboarding) -> CandidateProfileResponse:
    """This account's candidate profile.

    404 when onboarding has not saved one yet. Not an empty 200: "no profile" is a
    different state from "a profile with no fields filled in", and the onboarding
    screen has to tell them apart.
    """
    profile = await service.profile(current.user.id)
    if profile is None:
        raise _no_profile_yet()
    return CandidateProfileResponse.of(profile)


@router.put("/profile", response_model=CandidateProfileResponse)
async def save_profile(body: CandidateProfileDraft, current: CurrentSession,
                       service: Onboarding, instant: Now) -> CandidateProfileResponse:
    """Create or replace this account's candidate profile.

    The draft *is* the request model — the same value objects the aggregate holds,
    minus the id, the owner and the timestamp. There is no `user_id` field for a
    body to set, which is the point of the draft existing at all.
    """
    saved = await service.save_profile(current.user.id, body, now=instant)
    return CandidateProfileResponse.of(saved)


@router.get("/search-profiles", response_model=SearchProfileListResponse)
async def list_search_profiles(
        current: CurrentSession, service: Onboarding,
        active_only: bool = Query(default=False,
                                  description="Return only searches that are active."),
) -> SearchProfileListResponse:
    searches = await service.searches(current.user.id, active_only=active_only)
    return SearchProfileListResponse(
        search_profiles=tuple(SearchProfileResponse.of(search) for search in searches))


@router.post("/search-profiles", response_model=SearchProfileResponse,
             status_code=status.HTTP_201_CREATED)
async def create_search_profile(body: SearchProfileDraft, current: CurrentSession,
                                service: Onboarding,
                                instant: Now) -> SearchProfileResponse:
    created = await service.create_search(current.user.id, body, now=instant)
    return SearchProfileResponse.of(created)


@router.put("/search-profiles/{search_profile_id}",
            response_model=SearchProfileResponse)
async def update_search_profile(search_profile_id: SearchProfileId,
                                body: SearchProfileDraft, current: CurrentSession,
                                service: Onboarding,
                                instant: Now) -> SearchProfileResponse:
    """Replace one saved search, keeping its id and its creation time."""
    updated = await service.update_search(current.user.id, search_profile_id, body,
                                          now=instant)
    return SearchProfileResponse.of(updated)


@router.delete("/search-profiles/{search_profile_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def delete_search_profile(search_profile_id: SearchProfileId,
                                current: CurrentSession,
                                service: Onboarding) -> None:
    """Delete one saved search.

    Not idempotent, on purpose: a second `DELETE` answers 404 rather than 204. A
    client that deleted a search twice is working from a stale list, and telling it
    so is more useful than pretending the second call did something.
    """
    await service.delete_search(current.user.id, search_profile_id)


def _no_profile_yet() -> ApiError:
    """404 for "onboarding has not saved a profile", with its own error code.

    A distinct code from `search_profile_not_found` because the frontend acts on it
    differently: this one sends the user to the onboarding form.
    """
    return ApiError(status.HTTP_404_NOT_FOUND, "candidate_profile_not_found",
                    "this account has not saved a candidate profile yet")
