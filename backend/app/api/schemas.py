"""Request and response models: the boundary the domain does not cross.

Two rules, and between them they are most of what this module is for.

**Nothing leaves as a domain aggregate.** `User` holds a password hash and
`UserSession` holds two token digests. They are `SecretStr`, so a `model_dump_json`
would render `"**********"` rather than the value — but a response model that has no
field for them cannot leak them even if that changed, and a reader can verify the
guarantee by reading twenty lines instead of auditing every route. `AccountResponse`
is the whole of what an authenticated client learns about its own account.

**Nothing arrives as a domain aggregate either.** A request body is a draft
(`CandidateProfileDraft`, `SearchProfileDraft`) or one of the small models here, and
none of them has a `user_id`, an `id` or a timestamp. The service supplies the owner
from the session, so there is no field for a request to override
(docs/ENGINEERING_STANDARDS.md §Security: user-owned data is authorization-scoped).

The password band is stated here as well as in `backend.app.core.passwords`. Two
checks on purpose: this one is the API's contract and produces a 422 a form can
show, and that one is what a future CLI or importer cannot bypass.
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr

from backend.app.core.passwords import MAXIMUM_PASSWORD_LENGTH, MINIMUM_PASSWORD_LENGTH
from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.identifiers import CandidateProfileId, SearchProfileId, UserId
from backend.app.domain.search import SearchProfile
from backend.app.domain.user import User, UserStatus
from backend.app.services.onboarding import (
    CandidateProfileDraft,
    OnboardingState,
    SearchProfileDraft,
)

Password = SecretStr


class ApiModel(BaseModel):
    """The base every schema here shares.

    `extra="forbid"` is the half that matters: a request body carrying a field the
    model does not declare is a client that believes something untrue about this
    API, and answering 422 is more useful than silently ignoring it. It is also
    what makes "there is no `user_id` field to override" a checked statement rather
    than an observation about the current field list.
    """

    model_config = ConfigDict(extra="forbid")


class RegisterRequest(ApiModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: EmailStr
    password: Password = Field(min_length=MINIMUM_PASSWORD_LENGTH,
                               max_length=MAXIMUM_PASSWORD_LENGTH)


class LoginRequest(ApiModel):
    email: EmailStr
    # No length band on login, deliberately: the band is a policy for *choosing* a
    # password, and applying it here would tell an attacker that a submitted value
    # was too short to be anybody's password — one bit of the search space, given
    # away for nothing.
    password: Password


class AccountResponse(ApiModel):
    """What a client learns about its own account. No credential fields exist."""

    id: UserId
    email: EmailStr
    display_name: str | None
    status: UserStatus
    onboarding_completed_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, user: User) -> "AccountResponse":
        return cls(id=user.id, email=user.email, display_name=user.display_name,
                   status=user.status,
                   onboarding_completed_at=user.onboarding_completed_at,
                   created_at=user.created_at)


class SessionResponse(ApiModel):
    """The current session's window — issued, expires — and nothing identifying it.

    No session id and no digest. The client has the cookie; an id in a body would
    only be useful to something that wanted to name somebody else's session.
    """

    issued_at: datetime
    expires_at: datetime
    last_seen_at: datetime


class SignedInResponse(ApiModel):
    """The reply to register, login and `GET /auth/session`.

    The tokens are absent from this model by construction — they leave in two
    `Set-Cookie` headers, written by `backend.app.api.cookies`, which is the only
    place in the process that touches their raw values.
    """

    account: AccountResponse
    session: SessionResponse


class CandidateProfileResponse(ApiModel):
    """A saved profile, echoed back with the two fields the draft could not carry.

    `evidence` and `claims` are not here either: they have no storage until Phase
    10, and a field that was always `[]` would read as "this candidate has no
    evidence" rather than "this system does not keep any yet".
    """

    id: CandidateProfileId
    user_id: UserId
    updated_at: datetime
    profile: CandidateProfileDraft

    @classmethod
    def of(cls, profile: CandidateProfile) -> "CandidateProfileResponse":
        return cls(
            id=profile.id, user_id=profile.user_id, updated_at=profile.updated_at,
            profile=CandidateProfileDraft(
                display_name=profile.display_name,
                headline=profile.headline,
                base_location=profile.base_location,
                languages=profile.languages,
                work_authorizations=profile.work_authorizations,
                availability=profile.availability))


class SearchProfileResponse(ApiModel):
    """A saved search, echoed back with its id and timestamps."""

    id: SearchProfileId
    user_id: UserId
    created_at: datetime
    updated_at: datetime
    search: SearchProfileDraft

    @classmethod
    def of(cls, profile: SearchProfile) -> "SearchProfileResponse":
        return cls(
            id=profile.id, user_id=profile.user_id, created_at=profile.created_at,
            updated_at=profile.updated_at,
            search=SearchProfileDraft(
                name=profile.name,
                is_active=profile.is_active,
                areas=profile.areas,
                queries=profile.queries,
                title_keywords=profile.title_keywords,
                excluded_keywords=profile.excluded_keywords,
                opportunity_types=profile.opportunity_types,
                contract_types=profile.contract_types,
                workplace_modes=profile.workplace_modes,
                posting_languages=profile.posting_languages,
                workload=profile.workload,
                source_keys=profile.source_keys))


class SearchProfileListResponse(ApiModel):
    """A wrapper, not a bare array.

    A top-level JSON array cannot grow a field, so the first time this needs a
    count or a cursor it would have to become a breaking change. It also keeps
    every V2 response an object, which is one rule for the client to hold.
    """

    search_profiles: tuple[SearchProfileResponse, ...]


class OnboardingStateResponse(ApiModel):
    """Which step the frontend should show, and whether finishing is possible.

    `may_complete` is computed here rather than in the client, so the button's
    enabled state and the server's 409 can never disagree.
    """

    has_profile: bool
    search_profiles: int
    active_search_profiles: int
    completed_at: datetime | None
    is_complete: bool
    may_complete: bool

    @classmethod
    def of(cls, state: OnboardingState) -> "OnboardingStateResponse":
        return cls(has_profile=state.has_profile,
                   search_profiles=state.search_profiles,
                   active_search_profiles=state.active_search_profiles,
                   completed_at=state.completed_at,
                   is_complete=state.is_complete,
                   may_complete=state.may_complete)
