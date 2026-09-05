"""What the application needs from storage, in domain terms.

Three rules shape every signature here:

**No transaction control.** There is no `commit`, no `rollback` and no `begin`. A
repository that commits decides on its caller's behalf that a unit of work is
over, which is how an import ends up half-applied; the boundary is
`backend.app.infrastructure.database.engine.session_scope`.

**User-scoped reads take the user first.** `list_for_user(user_id, …)` cannot be
called without saying whose data is wanted, so an authorization filter is
impossible to forget in Phase 4 (docs/ENGINEERING_STANDARDS.md §Security:
authorization enforced server-side). `Opportunity` and `Company` are shared facts
and deliberately have no such parameter, and the two authentication lookups that
*establish* identity — `get_by_email` and `get_by_digest` — cannot have one.

**Writes are upserts.** Every persisted entity has an id the domain produced —
uuid5 for anything derived from V1, uuid4 otherwise — so "create" and "update"
are the same operation from the caller's point of view, and retrying a failed
import is safe by construction.

Small on purpose: Phase 2 owes the persistence foundation, and a method nobody
calls yet is a guess about Phase 6 and 12 that would have to be unguessed.
"""
from datetime import datetime
from typing import NamedTuple, Protocol, runtime_checkable

from pydantic import SecretStr

from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.common import GeoPoint
from backend.app.domain.company import Company
from backend.app.domain.identifiers import (
    CandidateProfileId,
    CompanyId,
    MatchEvaluationId,
    OpportunityId,
    SearchProfileId,
    UserId,
    UserSessionId,
)
from backend.app.domain.matching import MatchEvaluation
from backend.app.domain.opportunity import Opportunity
from backend.app.domain.search import SearchProfile
from backend.app.domain.user import User, UserSession

# Every list method is capped. An uncapped query is fine against the empty
# development database and is an outage against a real one, and the caller that
# wants everything can say so explicitly.
DEFAULT_LIMIT = 100


class OpportunityNearby(NamedTuple):
    """An opportunity and how far it is from the point that was searched.

    The distance is computed by the database, not recomputed in Python: it comes
    from the same spheroid calculation that decided the row matched, so a result
    can never be shown as 101 km away by a query for everything within 100 km.
    """

    opportunity: Opportunity
    distance_meters: float


@runtime_checkable
class CompanyRepository(Protocol):
    """Employers, with their sites."""

    async def get(self, company_id: CompanyId) -> Company | None:
        """The company and all of its locations, or `None`."""
        ...

    async def upsert(self, company: Company) -> Company:
        """Write the company and reconcile its locations.

        Returns what is now stored, which is how a caller learns that a location
        it did not include has been deleted.
        """
        ...

    async def list_near(self, center: GeoPoint, radius_meters: float, *,
                        limit: int = DEFAULT_LIMIT) -> tuple[Company, ...]:
        """Companies with at least one site within `radius_meters` of `center`.

        The Phase 6 spontaneous-application question ("who is nearby") in its
        minimal form. Phase 7 builds the explorer on top of it.
        """
        ...


@runtime_checkable
class OpportunityRepository(Protocol):
    """Postings — shared facts, so nothing here is user-scoped."""

    async def get(self, opportunity_id: OpportunityId) -> Opportunity | None: ...

    async def get_by_source(self, source_key: str,
                            external_id: str) -> Opportunity | None:
        """The posting a source already published under this identifier.

        The idempotency lookup for any importer or discovery run: it answers
        "have I seen this one before?" without depending on how the id was
        derived.
        """
        ...

    async def get_by_fingerprint(self, fingerprint: str) -> Opportunity | None:
        """The posting carrying this dedup fingerprint, whatever source found it."""
        ...

    async def upsert(self, opportunity: Opportunity) -> Opportunity: ...

    async def list_recent(self, *,
                          limit: int = DEFAULT_LIMIT) -> tuple[Opportunity, ...]:
        """Newest discoveries first — the shape of the feed."""
        ...

    async def list_near(self, center: GeoPoint, radius_meters: float, *,
                        limit: int = DEFAULT_LIMIT) -> tuple[OpportunityNearby, ...]:
        """Postings within `radius_meters` of `center`, closest first."""
        ...


@runtime_checkable
class MatchEvaluationRepository(Protocol):
    """Evaluations — user-owned, so every method names the owner.

    `user_id` is a parameter and not an ambient value on purpose. A repository
    that read the current user from a context variable would let a service forget
    the filter and still compile; here the filter is part of the call.
    """

    async def get(self, user_id: UserId,
                  evaluation_id: MatchEvaluationId) -> MatchEvaluation | None:
        """The evaluation, or `None` — including when it belongs to somebody else.

        Not found and not yours are deliberately indistinguishable: a caller that
        could tell them apart could enumerate another user's rows by id
        (docs/ENGINEERING_STANDARDS.md §Security: no cross-user data leakage).
        """
        ...

    async def get_for_pair(self, user_id: UserId,
                           candidate_profile_id: CandidateProfileId,
                           opportunity_id: OpportunityId) -> MatchEvaluation | None:
        """This profile's verdict on this posting, if it has been evaluated."""
        ...

    async def upsert(self, evaluation: MatchEvaluation) -> MatchEvaluation:
        """Write the evaluation and reconcile its dimension scores.

        The owner comes from `evaluation.user_id`, so there is no signature in
        which the row's owner and the caller's intent can disagree.
        """
        ...

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[MatchEvaluation, ...]:
        """This user's evaluations, most recently evaluated first."""
        ...


# Phase 4. The four contracts below are what authentication and onboarding need,
# and nothing more. Two of them break the "user first" convention, both for the
# same reason: `UserRepository.get_by_email` and `SessionRepository.get_by_digest`
# are how a caller *establishes* which user it is talking about, so there is no
# `user_id` to pass yet. Every other method here takes one.


@runtime_checkable
class UserRepository(Protocol):
    """Accounts, looked up by the two things a login flow has: an id or an email."""

    async def get(self, user_id: UserId) -> User | None: ...

    async def get_by_email(self, email: str) -> User | None:
        """The account registered under this address, or `None`.

        The caller passes the address as the user typed it; the implementation
        normalizes it the same way `EmailAddress` does, so a login with
        `Ada@Example.com` finds the account stored as `ada@example.com` rather
        than reporting no such user.
        """
        ...

    async def upsert(self, user: User) -> User:
        """Write the account.

        Also the update path for the lockout counters and `last_login_at`, which
        is why there is no separate `record_failed_login`: those are fields of the
        aggregate, and a second write path would be a second place to forget
        `updated_at`.
        """
        ...


@runtime_checkable
class SessionRepository(Protocol):
    """Server-side sessions: issue one, find one by its digest, revoke them.

    Every method speaks in digests. The raw token never reaches this layer, so
    there is no signature here that could accidentally store one
    (docs/AUTHENTICATION.md §Sessions).
    """

    async def get_by_digest(self, token_digest: SecretStr) -> UserSession | None:
        """The session whose stored digest matches, whatever its state.

        Expired and revoked sessions are returned rather than filtered out: the
        caller holds the clock (`UserSession.is_usable`), and a repository that
        applied its own `now()` would make expiry untestable and disagree with the
        service the moment the two clocks differed by a millisecond.
        """
        ...

    async def upsert(self, session: UserSession) -> UserSession:
        """Write the session — the issue path and the `last_seen_at` touch."""
        ...

    async def revoke(self, user_id: UserId, session_id: UserSessionId,
                     revoked_at: datetime) -> bool:
        """Revoke one of this user's sessions; `True` if it was live until now.

        Scoped by `user_id` so a logout cannot revoke somebody else's session by
        id, and idempotent: revoking an already-revoked session returns `False`
        and leaves the original instant intact.
        """
        ...

    async def revoke_all_for_user(self, user_id: UserId,
                                  revoked_at: datetime) -> int:
        """Revoke every live session this user has; returns how many were live.

        The "log me out everywhere" primitive, and what a future password change
        must call. Phase 4 uses it to make a `DISABLED` account's sessions stop.
        """
        ...

    async def delete_expired(self, as_of: datetime, *, limit: int = DEFAULT_LIMIT) -> int:
        """Delete sessions that expired before `as_of`; returns how many.

        Capped like every other bulk method. Nothing schedules this in Phase 4 —
        it is the housekeeping a worker will call, and it exists now so the table
        does not grow without a stated way to prune it.
        """
        ...


@runtime_checkable
class CandidateProfileRepository(Protocol):
    """Candidate profiles — user-owned, so `user_id` comes first everywhere."""

    async def get(self, user_id: UserId,
                  profile_id: CandidateProfileId) -> CandidateProfile | None:
        """The profile, or `None` — including when it belongs to somebody else.

        Not found and not yours are indistinguishable here for the same reason as
        in `MatchEvaluationRepository.get`.
        """
        ...

    async def get_default(self, user_id: UserId) -> CandidateProfile | None:
        """The profile onboarding created for this account, if it exists.

        A lookup by owner rather than by the derived id, so it keeps working the
        day an account has a second profile — at which point this becomes "the
        first one", not "the only one".
        """
        ...

    async def upsert(self, profile: CandidateProfile) -> CandidateProfile:
        """Write the profile and reconcile its languages, permits and slots.

        Raises `ValueError` if the profile carries evidence or claims: there is
        nowhere to put them until Phase 10, and writing the profile without them
        would silently break the claim-to-evidence link on the way back out.
        """
        ...

    async def list_for_user(
            self, user_id: UserId, *,
            limit: int = DEFAULT_LIMIT) -> tuple[CandidateProfile, ...]:
        """This user's profiles, oldest first — the order they were created in."""
        ...


@runtime_checkable
class SearchProfileRepository(Protocol):
    """Saved searches — user-owned, and the unit discovery will iterate over."""

    async def get(self, user_id: UserId,
                  search_profile_id: SearchProfileId) -> SearchProfile | None: ...

    async def upsert(self, profile: SearchProfile) -> SearchProfile:
        """Write the search and reconcile its areas."""
        ...

    async def delete(self, user_id: UserId, search_profile_id: SearchProfileId) -> bool:
        """Delete one of this user's searches; `True` if a row was removed.

        The only hard delete in this module, and it is right: a saved search is
        the user's own list, and an "archived" search they cannot remove is a bug
        report waiting to happen. Its areas go with it by `ON DELETE CASCADE`.
        """
        ...

    async def list_for_user(self, user_id: UserId, *, active_only: bool = False,
                            limit: int = DEFAULT_LIMIT) -> tuple[SearchProfile, ...]:
        """This user's searches, newest first.

        `active_only` is what onboarding completion checks and what a discovery
        run will want; it is a parameter rather than a separate method so the
        index on `(user_id, is_active)` serves one query shape.
        """
        ...



