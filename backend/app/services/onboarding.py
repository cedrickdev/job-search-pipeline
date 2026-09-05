"""Onboarding: the candidate profile, the first saved search, and the stamp.

Onboarding is not a wizard state machine here. It is two writes the user can make
in either order and as often as they like, plus one validating stamp:

1. save a candidate profile — who they are, what they speak, what they may work,
   when they are free;
2. save at least one active search — what they are looking for, and where;
3. `complete()`, which refuses unless both exist and otherwise records
   `onboarding_completed_at` on the *account*.

The stamp lives on `users` rather than on either aggregate because it is a fact
about the account's setup: a flag on the profile could not express "and a search
exists too". `complete()` re-reads both instead of trusting the client, so a
request that skipped a step cannot mark itself done.

**Drafts, not aggregates, cross the API boundary.** `CandidateProfileDraft` and
`SearchProfileDraft` are what a route accepts: the same domain value objects, minus
the id, the owner and the timestamps. That is what makes it impossible for a
request body to nominate its own `user_id` — the service supplies it from the
authenticated session, and there is no field to override
(docs/ENGINEERING_STANDARDS.md §Security: user-owned data is authorization-scoped).
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.base import DomainModel, LanguageCode, NonEmptyStr
from backend.app.domain.candidate import (
    Availability,
    CandidateProfile,
    WorkAuthorization,
)
from backend.app.domain.common import LanguageProficiency, Location, WorkloadRange
from backend.app.domain.identifiers import (
    SearchProfileId,
    UserId,
    default_candidate_profile_id,
    new_search_profile_id,
)
from backend.app.domain.opportunity import ContractType, OpportunityType, WorkplaceMode
from backend.app.domain.search import SearchArea, SearchProfile
from backend.app.domain.user import User
from backend.app.repositories.contracts import (
    CandidateProfileRepository,
    SearchProfileRepository,
    UserRepository,
)


class OnboardingError(Exception):
    """Base class for the two ways onboarding can refuse a request."""


class SearchProfileNotFound(OnboardingError):
    """No such saved search for this user — or it belongs to somebody else.

    One error for both, deliberately: telling the two apart would let a caller
    enumerate other users' search ids (docs/ENGINEERING_STANDARDS.md §Security).
    """


class OnboardingIncomplete(OnboardingError):
    """`complete()` was called before there was a profile and an active search."""

    def __init__(self, *, has_profile: bool, active_searches: int) -> None:
        super().__init__(
            f"onboarding needs a candidate profile and at least one active search; "
            f"profile={has_profile}, active searches={active_searches}")
        self.has_profile = has_profile
        self.active_searches = active_searches


class CandidateProfileDraft(DomainModel):
    """A candidate profile as submitted: no id, no owner, no timestamps.

    Every field is the domain's own value object, so the validation a request body
    gets is exactly the validation the aggregate enforces — the language code
    pattern, the availability window ordering, the permit hour cap. Nothing is
    re-specified here that `CandidateProfile` already specifies.
    """

    display_name: NonEmptyStr
    headline: NonEmptyStr | None = None
    base_location: Location | None = None
    languages: tuple[LanguageProficiency, ...] = ()
    work_authorizations: tuple[WorkAuthorization, ...] = ()
    availability: Availability | None = None

    @model_validator(mode="after")
    def _no_evidence_may_be_cited_yet(self) -> Self:
        """Refuse `evidence_ids` on a permit, with a sentence that says why.

        The evidence store is Phase 10. Without this check the aggregate would
        still refuse the profile — `_claims_rest_on_held_evidence` fails on a
        citation the profile does not hold — but the error would name an invariant
        the client cannot see, and this one names the reason.
        """
        if any(authorization.evidence_ids
               for authorization in self.work_authorizations):
            raise ValueError(
                "work authorization evidence arrives with the evidence store; "
                "submit the permit without evidence_ids")
        return self


class SearchProfileDraft(DomainModel):
    """A saved search as submitted: no id, no owner, no timestamps.

    The eight filter tuples keep the domain's convention, and it is worth repeating
    where a client can read it: **an empty list means "no restriction", not "match
    nothing"**. `areas` is the one exception — at least one is required, because an
    unbounded geographic search is what makes a discovery run sweep the planet.
    """

    name: NonEmptyStr
    is_active: bool = True
    areas: Annotated[tuple[SearchArea, ...], Field(min_length=1)]
    queries: tuple[NonEmptyStr, ...] = ()
    title_keywords: tuple[NonEmptyStr, ...] = ()
    excluded_keywords: tuple[NonEmptyStr, ...] = ()
    opportunity_types: tuple[OpportunityType, ...] = ()
    contract_types: tuple[ContractType, ...] = ()
    workplace_modes: tuple[WorkplaceMode, ...] = ()
    posting_languages: tuple[LanguageCode, ...] = ()
    workload: WorkloadRange | None = None
    source_keys: tuple[NonEmptyStr, ...] = ()


@dataclass(frozen=True, slots=True)
class OnboardingState:
    """What the frontend needs to decide which step to show.

    Counts rather than booleans for the searches, because "you have one search but
    it is paused" is a different screen from "you have none", and a boolean would
    have thrown that away.
    """

    has_profile: bool
    search_profiles: int
    active_search_profiles: int
    completed_at: datetime | None

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None

    @property
    def may_complete(self) -> bool:
        """Whether `complete()` would succeed right now."""
        return self.has_profile and self.active_search_profiles > 0


class OnboardingService:
    """The profile and search writes, and the completion stamp.

    Every method takes `user_id` — from the authenticated session, never from a
    request body — and hands it to repositories whose signatures require it. There
    is no method here that could read or write another user's row.
    """

    def __init__(self, profiles: CandidateProfileRepository,
                 searches: SearchProfileRepository, users: UserRepository) -> None:
        self._profiles = profiles
        self._searches = searches
        self._users = users

    async def profile(self, user_id: UserId) -> CandidateProfile | None:
        """This account's profile, or `None` if onboarding has not saved one."""
        return await self._profiles.get_default(user_id)

    async def save_profile(self, user_id: UserId, draft: CandidateProfileDraft, *,
                           now: datetime) -> CandidateProfile:
        """Create or replace the account's profile.

        The id is derived from the account, so this is idempotent by construction:
        a double-submitted form updates one profile rather than creating a second
        (`default_candidate_profile_id`). `evidence` and `claims` are left empty —
        they have no storage until Phase 10, and the repository refuses a profile
        that carries them rather than dropping them.
        """
        return await self._profiles.upsert(CandidateProfile(
            id=default_candidate_profile_id(user_id),
            user_id=user_id,
            display_name=draft.display_name,
            headline=draft.headline,
            base_location=draft.base_location,
            languages=draft.languages,
            work_authorizations=draft.work_authorizations,
            availability=draft.availability,
            updated_at=now))

    async def searches(self, user_id: UserId, *,
                       active_only: bool = False) -> tuple[SearchProfile, ...]:
        return await self._searches.list_for_user(user_id, active_only=active_only)

    async def create_search(self, user_id: UserId, draft: SearchProfileDraft, *,
                            now: datetime) -> SearchProfile:
        """Save a new search under a fresh id."""
        return await self._searches.upsert(
            self._search_from_draft(new_search_profile_id(), user_id, draft,
                                    created_at=now, updated_at=now))

    async def update_search(self, user_id: UserId, search_profile_id: SearchProfileId,
                            draft: SearchProfileDraft, *,
                            now: datetime) -> SearchProfile:
        """Replace one of this user's searches.

        Loads it first for two reasons that are both correctness rather than
        caution: `created_at` belongs to the row and must survive an edit, and the
        load is the authorization check — another user's id reads as absent, so this
        raises instead of writing.
        """
        existing = await self._searches.get(user_id, search_profile_id)
        if existing is None:
            raise SearchProfileNotFound(str(search_profile_id))
        return await self._searches.upsert(
            self._search_from_draft(search_profile_id, user_id, draft,
                                    created_at=existing.created_at, updated_at=now))

    async def delete_search(self, user_id: UserId,
                            search_profile_id: SearchProfileId) -> None:
        """Delete one of this user's searches, or raise if there is none."""
        if not await self._searches.delete(user_id, search_profile_id):
            raise SearchProfileNotFound(str(search_profile_id))

    async def state(self, user: User) -> OnboardingState:
        """What has been done so far, for the screen that decides the next step.

        Takes the `User` rather than a `UserId` because the completion stamp is on
        the account and the caller — an authenticated request — already holds it;
        re-reading the row to fetch a field we were handed would be a query for
        nothing.
        """
        profiles = await self._profiles.list_for_user(user.id)
        searches = await self._searches.list_for_user(user.id)
        return OnboardingState(
            has_profile=bool(profiles),
            search_profiles=len(searches),
            active_search_profiles=sum(1 for search in searches if search.is_active),
            completed_at=user.onboarding_completed_at)

    async def complete(self, user: User, *, now: datetime) -> User:
        """Stamp `onboarding_completed_at`, or refuse and say what is missing.

        Both preconditions are re-read here rather than taken from the request:
        the client tells us it finished, and the database tells us whether it did
        (docs/ENGINEERING_STANDARDS.md §Security — the server decides).

        Idempotent, and it keeps the *first* timestamp: an already-completed
        account is returned untouched, because when onboarding finished is a
        historical fact and a double-submitted button should not rewrite it.
        """
        if user.onboarding_completed_at is not None:
            return user
        state = await self.state(user)
        if not state.may_complete:
            raise OnboardingIncomplete(
                has_profile=state.has_profile,
                active_searches=state.active_search_profiles)
        return await self._users.upsert(user.model_copy(update={
            "onboarding_completed_at": now, "updated_at": now}))

    @staticmethod
    def _search_from_draft(search_profile_id: SearchProfileId, user_id: UserId,
                           draft: SearchProfileDraft, *, created_at: datetime,
                           updated_at: datetime) -> SearchProfile:
        """Assemble the aggregate a draft describes.

        Field by field rather than `SearchProfile(**draft.model_dump())`, because
        `**` on a dict switches mypy's argument checking off entirely: a field added
        to `SearchProfile` would silently take its default, and a renamed draft field
        would fail at runtime instead of here.
        """
        return SearchProfile(
            id=search_profile_id,
            user_id=user_id,
            name=draft.name,
            is_active=draft.is_active,
            areas=draft.areas,
            queries=draft.queries,
            title_keywords=draft.title_keywords,
            excluded_keywords=draft.excluded_keywords,
            opportunity_types=draft.opportunity_types,
            contract_types=draft.contract_types,
            workplace_modes=draft.workplace_modes,
            posting_languages=draft.posting_languages,
            workload=draft.workload,
            source_keys=draft.source_keys,
            created_at=created_at,
            updated_at=updated_at)
