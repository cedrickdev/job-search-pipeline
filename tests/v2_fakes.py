# tests/v2_fakes.py
"""In-memory repositories, so the Phase 4 services can be tested without a socket.

`backend.app.services` takes repository `Protocol`s and the current instant as
arguments precisely to make this possible: the lockout window, session expiry,
CSRF check and onboarding gate are decisions the service makes, and a test that
needed PostgreSQL to observe them would be testing the driver as well.

Two properties are deliberate rather than incidental, because a fake that lacks
them proves the wrong thing:

**Stored objects are copied on the way in and out.** The real repositories go
through SQLAlchemy rows, so a caller cannot mutate the store by holding on to
what it saved. A dict of shared references would let a test pass because two
names point at one object — and the same code would fail against Postgres.

**Every read is scoped by `user_id` exactly where the contract says it is.** These
fakes are what the cross-user isolation tests run against, so a `get` that
ignored its `user_id` argument would quietly make those tests vacuous.
"""
from datetime import datetime

from pydantic import SecretStr

from backend.app.core.tokens import digests_match
from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.identifiers import (
    CandidateProfileId,
    SearchProfileId,
    UserId,
    UserSessionId,
)
from backend.app.domain.search import SearchProfile
from backend.app.domain.user import User, UserSession, normalize_email
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    CandidateProfileRepository,
    SearchProfileRepository,
    SessionRepository,
    UserRepository,
)


def _implements_contracts() -> tuple[
        UserRepository, SessionRepository, CandidateProfileRepository,
        SearchProfileRepository]:
    """Structural conformance, the same guard `sqlalchemy_repositories` carries.

    A fake whose signature drifted from the `Protocol` would still run — Python
    does not care — and the service tests would go on passing against an interface
    the production repositories no longer have. This return type is the check.
    `mypy` does not cover `tests/` by configuration, so run it explicitly:
    `mypy tests/v2_fakes.py`.
    """
    return (FakeUserRepository(), FakeSessionRepository(),
            FakeCandidateProfileRepository(), FakeSearchProfileRepository())


class FakeUserRepository:
    """Accounts, keyed by id, with the email index the login flow needs."""

    def __init__(self) -> None:
        self.users: dict[UserId, User] = {}

    async def get(self, user_id: UserId) -> User | None:
        return self.users.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        # `normalize_email`, not `.lower()`: the real repository applies the
        # domain's rule so a lookup cannot drift from the write, and a fake that
        # lower-cased on its own would hide a drift rather than reproduce it.
        wanted = normalize_email(email)
        return next((user for user in self.users.values() if user.email == wanted),
                    None)

    async def upsert(self, user: User) -> User:
        stored = user.model_copy(deep=True)
        self.users[stored.id] = stored
        return stored


class FakeSessionRepository:
    """Sessions, and the revocation and expiry operations, over one dict.

    `get_by_digest` compares in constant time through `digests_match` for the same
    reason the production path does — not because a test can be attacked, but
    because using the digest as a dict key would let a fake accept a token the real
    lookup rejects.
    """

    def __init__(self) -> None:
        self.sessions: dict[UserSessionId, UserSession] = {}

    async def get_by_digest(self, token_digest: SecretStr) -> UserSession | None:
        for session in self.sessions.values():
            if digests_match(token_digest, session.token_digest):
                return session.model_copy(deep=True)
        return None

    async def upsert(self, session: UserSession) -> UserSession:
        stored = session.model_copy(deep=True)
        self.sessions[stored.id] = stored
        return stored

    async def revoke(self, user_id: UserId, session_id: UserSessionId,
                     revoked_at: datetime) -> bool:
        session = self.sessions.get(session_id)
        if session is None or session.user_id != user_id \
                or session.revoked_at is not None:
            return False
        self.sessions[session_id] = session.model_copy(
            update={"revoked_at": revoked_at})
        return True

    async def revoke_all_for_user(self, user_id: UserId,
                                  revoked_at: datetime) -> int:
        live = [session for session in self.sessions.values()
                if session.user_id == user_id and session.revoked_at is None]
        for session in live:
            self.sessions[session.id] = session.model_copy(
                update={"revoked_at": revoked_at})
        return len(live)

    async def delete_expired(self, as_of: datetime, *,
                             limit: int = DEFAULT_LIMIT) -> int:
        doomed = sorted((session for session in self.sessions.values()
                         if session.expires_at < as_of),
                        key=lambda session: session.expires_at)[:limit]
        for session in doomed:
            del self.sessions[session.id]
        return len(doomed)


class FakeCandidateProfileRepository:
    """Profiles, with the Phase 10 refusal the real repository performs.

    The refusal is reproduced rather than skipped because `OnboardingService`
    relies on it: `save_profile` leaves `evidence` and `claims` empty, and a fake
    that accepted them would let a future change start dropping them silently.
    """

    def __init__(self) -> None:
        self.profiles: dict[CandidateProfileId, CandidateProfile] = {}

    async def get(self, user_id: UserId,
                  profile_id: CandidateProfileId) -> CandidateProfile | None:
        profile = self.profiles.get(profile_id)
        if profile is None or profile.user_id != user_id:
            return None
        return profile.model_copy(deep=True)

    async def get_default(self, user_id: UserId) -> CandidateProfile | None:
        return next(iter(await self.list_for_user(user_id)), None)

    async def upsert(self, profile: CandidateProfile) -> CandidateProfile:
        if profile.evidence or profile.claims:
            raise ValueError(
                "candidate evidence and claims have no V2 persistence yet (Phase 10)")
        stored = profile.model_copy(deep=True)
        self.profiles[stored.id] = stored
        return stored

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT
                            ) -> tuple[CandidateProfile, ...]:
        # Insertion order, which is oldest-first and therefore what the real
        # query's `ORDER BY created_at, id` produces. `CandidateProfile` has no
        # `created_at` of its own — the column is a server default — so the dict's
        # order is the only thing a fake can honestly sort by. It is also stable
        # across a re-upsert, which is what `get_default` depends on.
        return tuple(profile.model_copy(deep=True)
                     for profile in self.profiles.values()
                     if profile.user_id == user_id)[:limit]


class FakeSearchProfileRepository:
    """Saved searches, with the `active_only` filter and the scoped delete."""

    def __init__(self) -> None:
        self.searches: dict[SearchProfileId, SearchProfile] = {}

    async def get(self, user_id: UserId,
                  search_profile_id: SearchProfileId) -> SearchProfile | None:
        search = self.searches.get(search_profile_id)
        if search is None or search.user_id != user_id:
            return None
        return search.model_copy(deep=True)

    async def upsert(self, profile: SearchProfile) -> SearchProfile:
        stored = profile.model_copy(deep=True)
        self.searches[stored.id] = stored
        return stored

    async def delete(self, user_id: UserId,
                     search_profile_id: SearchProfileId) -> bool:
        search = self.searches.get(search_profile_id)
        if search is None or search.user_id != user_id:
            return False
        del self.searches[search_profile_id]
        return True

    async def list_for_user(self, user_id: UserId, *, active_only: bool = False,
                            limit: int = DEFAULT_LIMIT) -> tuple[SearchProfile, ...]:
        mine = [search.model_copy(deep=True) for search in self.searches.values()
                if search.user_id == user_id
                and (search.is_active or not active_only)]
        # Newest first, like the real query.
        mine.sort(key=lambda search: (search.created_at, search.id), reverse=True)
        return tuple(mine[:limit])
