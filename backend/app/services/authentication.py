"""Registration, login, per-request session lookup, logout.

Four decisions here are security properties rather than style, and each one is
written down beside the code that implements it:

**No account state is disclosed to somebody who cannot prove they own the
account.** A login with an unknown address, a wrong password, a locked account or
a disabled account all return the same `InvalidCredentials` *unless the password
was correct*. Only after a correct password does `log_in` say "locked" or
"disabled" — at which point the caller has demonstrated ownership, and a clear
message is help rather than a leak. Registration is the one place existence is
necessarily disclosed: refusing a duplicate address requires saying so, and the
alternative (an email with a link) needs mail delivery, which is a later phase.

**The unknown-address path costs what a real one costs.** `spend_verification_time`
runs an Argon2 verification against a throwaway digest, so the response time does
not distinguish a registered address from an unregistered one
(docs/AUTHENTICATION.md §Enumeration).

**A lockout is temporary and does not compound.** Ten failures lock the account
for fifteen minutes; attempts made *during* a lock are refused without counting,
so an attacker cannot extend somebody else's lock indefinitely, and once the
window passes the counter starts again from zero rather than re-locking on the
next typo.

**The session lifetime is absolute.** `last_seen_at` is bookkeeping, not a sliding
expiry: touching it cannot extend a session, which is what makes a stolen cookie
finite and what makes the touch safe to perform on a read request.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from pydantic import SecretStr

from backend.app.core.passwords import (
    hash_password,
    spend_verification_time,
    verify_password,
)
from backend.app.core.settings import AuthSettings
from backend.app.core.tokens import digest_of, digests_match, new_token
from backend.app.domain.identifiers import UserId, new_user_id, new_user_session_id
from backend.app.domain.user import User, UserSession, UserStatus
from backend.app.repositories.contracts import SessionRepository, UserRepository

# How stale `last_seen_at` may get before a request refreshes it. Five minutes,
# because the alternative is an `UPDATE` on every authenticated request for a
# column nothing decides anything from.
SESSION_TOUCH_INTERVAL: Final[timedelta] = timedelta(minutes=5)


class AuthenticationError(Exception):
    """Base class for every refusal this service can produce.

    A class per outcome, rather than one error with a code, because the API layer
    maps them to different status codes and a `match` on an exception type is
    checkable — a string code is not.
    """


class EmailAlreadyRegistered(AuthenticationError):
    """Registration refused: that address already has an account."""


class InvalidCredentials(AuthenticationError):
    """Login refused, and deliberately without saying why.

    Raised for an unknown address, a wrong password, and for a locked or disabled
    account *when the password was also wrong*. One exception type for all four is
    the point: a caller cannot map back to which it was.
    """


class AccountDisabled(AuthenticationError):
    """The password was right, but an operator has disabled the account."""


class AccountLocked(AuthenticationError):
    """The password was right, but too many recent failures locked the account."""

    def __init__(self, locked_until: datetime) -> None:
        super().__init__(f"account locked until {locked_until.isoformat()}")
        self.locked_until = locked_until


@dataclass(frozen=True, slots=True)
class SignedInUser:
    """The outcome of a register or login: an account, a session, and two tokens.

    `token` and `csrf_token` are the *raw* values, and this is the only place in
    the backend that holds them: they exist to be written to two cookies by the
    route and are never stored, logged or returned in a body. Both are `SecretStr`,
    so even the dataclass repr prints `SecretStr('**********')` rather than a
    working credential.
    """

    user: User
    session: UserSession
    token: SecretStr
    csrf_token: SecretStr


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """Who is making this request, and under which session."""

    user: User
    session: UserSession


def csrf_token_matches(session: UserSession, header_token: str | None) -> bool:
    """Whether the `X-CSRF-Token` header matches the session's stored digest.

    This is what upgrades a double-submit cookie into a check against server-side
    state. A plain double submit compares a header to a cookie, so anything that
    can *write* the cookie — a sibling subdomain, for instance — can satisfy it;
    comparing against the digest issued with this session cannot be satisfied by
    planting a cookie value.

    A missing or empty header is a mismatch rather than an error: the caller
    rejects the request either way, and there is nothing to tell the client about
    the difference.
    """
    if not header_token:
        return False
    return digests_match(digest_of(SecretStr(header_token)),
                         session.csrf_token_digest)


class AuthenticationService:
    """The login flow, over two repositories and a settings object.

    Nothing here reads a clock or commits. `now` is a parameter on every method
    for the same reason the domain models take it: the lockout window and session
    expiry are then testable without freezing time, and the API layer samples the
    instant once per request so every timestamp written by one request agrees.
    """

    def __init__(self, users: UserRepository, sessions: SessionRepository,
                 settings: AuthSettings) -> None:
        self._users = users
        self._sessions = sessions
        self._settings = settings

    async def register(self, *, email: str, password: SecretStr,
                       display_name: str | None, now: datetime) -> SignedInUser:
        """Create an account and sign it in.

        Signing in immediately is a deliberate product decision: onboarding is the
        next screen, and a registration that returned to a login form would ask
        for the same password twice for nothing.

        Raises `EmailAlreadyRegistered` when the address is taken. The check is a
        read followed by a write, so two simultaneous registrations can both pass
        it — `uq_users_email` is what actually decides, and the loser's flush
        raises an integrity error the API turns into the same 409. The read exists
        to make the common case a clean error rather than a database exception.
        """
        if await self._users.get_by_email(email) is not None:
            raise EmailAlreadyRegistered(email)
        # `hash_password` enforces the length band as well as the request schema
        # does. Two checks, on purpose: the schema is the API's contract, and this
        # one is what a future CLI or importer cannot bypass.
        user = await self._users.upsert(User(
            id=new_user_id(), email=email, password_hash=hash_password(password),
            display_name=display_name, created_at=now, updated_at=now))
        return await self._sign_in(user, now)

    async def log_in(self, *, email: str, password: SecretStr,
                     now: datetime) -> SignedInUser:
        """Verify a password and issue a session.

        The order of the checks is the enumeration property: the password is
        verified *before* the account's state is consulted, so a caller who does
        not know the password sees `InvalidCredentials` whatever the state of the
        account — or whether it exists at all.
        """
        user = await self._users.get_by_email(email)
        if user is None:
            # Equalize the timing, then refuse. Without this the response time
            # tells an attacker which addresses are registered.
            spend_verification_time(password)
            raise InvalidCredentials(email)
        check = verify_password(password, user.password_hash)
        if not check.matched:
            if not user.is_locked(now):
                # Attempts made while a lock is in force are refused without
                # counting, so nobody can extend another person's lockout.
                await self._record_failed_attempt(user, now)
            raise InvalidCredentials(email)
        if user.status is not UserStatus.ACTIVE:
            raise AccountDisabled(email)
        if user.locked_until is not None and user.is_locked(now):
            raise AccountLocked(user.locked_until)
        signed_in = user.model_copy(update={
            # A correct password against an out-of-date hash is the one moment
            # rehashing is free: the plaintext is in hand exactly here.
            "password_hash": (hash_password(password) if check.needs_rehash
                              else user.password_hash),
            "failed_login_attempts": 0,
            "locked_until": None,
            "last_login_at": now,
            "updated_at": now})
        return await self._sign_in(await self._users.upsert(signed_in), now)

    async def authenticate(self, *, token: SecretStr,
                           now: datetime) -> AuthenticatedSession | None:
        """Resolve a raw session token into a user, or `None`.

        `None` covers every reason a cookie may not be honoured — unknown, expired,
        revoked, or belonging to a disabled account — because the API's answer is
        the same 401 for all of them, and a caller that could tell them apart could
        probe the session table.

        A `DISABLED` account's existing sessions stop working here rather than being
        revoked: one `UPDATE status` is then enough to stop an account, without an
        operator having to remember a second statement.
        """
        session = await self._sessions.get_by_digest(digest_of(token))
        if session is None or not session.is_usable(now):
            return None
        user = await self._users.get(session.user_id)
        if user is None or user.status is not UserStatus.ACTIVE:
            return None
        return AuthenticatedSession(user=user, session=await self._touch(session, now))

    async def log_out(self, *, session: UserSession, now: datetime) -> bool:
        """Revoke this session; `True` if it was still live.

        Scoped to the session's own user by the repository, so a logout cannot
        revoke somebody else's session even if an id were guessed, and idempotent,
        so a double-submitted logout is not an error.
        """
        return await self._sessions.revoke(session.user_id, session.id, now)

    async def log_out_everywhere(self, *, user_id: UserId, now: datetime) -> int:
        """Revoke every live session of an account; returns how many there were.

        Phase 4 exposes no route for it. It is here because it is the operation a
        password change must perform to be worth anything, and because an operator
        disabling an account should be able to end its sessions in one call rather
        than relying on `authenticate` refusing them one request at a time.
        """
        return await self._sessions.revoke_all_for_user(user_id, now)

    async def _sign_in(self, user: User, now: datetime) -> SignedInUser:
        """Mint a session for an account whose credentials have been accepted."""
        # Two calls, not one value used twice: `UserSession` refuses a session
        # whose two digests are equal, precisely so this cannot be economized.
        token = new_token()
        csrf_token = new_token()
        session = await self._sessions.upsert(UserSession(
            id=new_user_session_id(),
            user_id=user.id,
            token_digest=digest_of(token),
            csrf_token_digest=digest_of(csrf_token),
            issued_at=now,
            # Absolute, not sliding. A sliding window keeps a stolen session alive
            # for as long as it is used, which is the opposite of what expiry is
            # for (docs/AUTHENTICATION.md §Sessions).
            expires_at=now + self._settings.session_lifetime,
            last_seen_at=now))
        return SignedInUser(user=user, session=session, token=token,
                            csrf_token=csrf_token)

    async def _record_failed_attempt(self, user: User, now: datetime) -> None:
        """Count one failure, and lock the account if that was the last allowance.

        A lock that has already expired resets the counter instead of adding to it:
        without that, the first typo after a lockout would immediately re-lock, and
        an account that once hit the limit would effectively have an allowance of
        one for ever.
        """
        lock_expired = user.locked_until is not None and user.locked_until <= now
        attempts = (0 if lock_expired else user.failed_login_attempts) + 1
        locked = attempts >= self._settings.max_failed_logins
        await self._users.upsert(user.model_copy(update={
            "failed_login_attempts": attempts,
            "locked_until": now + self._settings.lockout_duration if locked else None,
            "updated_at": now}))

    async def _touch(self, session: UserSession, now: datetime) -> UserSession:
        """Refresh `last_seen_at`, at most once per `SESSION_TOUCH_INTERVAL`.

        This is the one write a GET request performs, and it is not a state change
        in the sense the CSRF rules mean: it records that the caller's *own* session
        was used, it is idempotent, and — because `expires_at` is absolute — it
        cannot extend the session's life. Nothing an attacker could trigger
        cross-site gains anything from it.
        """
        if now - session.last_seen_at < SESSION_TOUCH_INTERVAL:
            return session
        return await self._sessions.upsert(
            session.model_copy(update={"last_seen_at": now}))
