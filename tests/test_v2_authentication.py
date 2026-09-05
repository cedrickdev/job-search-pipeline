# tests/test_v2_authentication.py
"""`AuthenticationService`: the login flow, its refusals, and the session window.

These run against `tests/v2_fakes.py` and a frozen clock, which is the point of
the service layer taking both as arguments. Every property asserted here is a
security property stated in `backend/app/services/authentication.py`, and the
tests are written so that removing the property breaks a named test rather than
degrading behaviour silently:

* enumeration — an unknown address and a wrong password are indistinguishable;
* disclosure — "locked" and "disabled" are only said to a correct password;
* the lockout is temporary, does not compound, and cannot be extended by a third
  party;
* the session lifetime is absolute, so the `last_seen_at` touch cannot extend it.

Passwords here are obvious fixtures. They exist to be hashed by real Argon2, so
each test costs a few hashes — which is why the module reuses two accounts rather
than registering one per assertion where it can.
"""
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from backend.app.core.settings import AuthSettings
from backend.app.core.tokens import digest_of, new_token
from backend.app.domain.identifiers import new_user_id, new_user_session_id
from backend.app.domain.user import UserSession, UserStatus
from backend.app.services.authentication import (
    SESSION_TOUCH_INTERVAL,
    AccountDisabled,
    AccountLocked,
    AuthenticationService,
    EmailAlreadyRegistered,
    InvalidCredentials,
    csrf_token_matches,
)
from tests.v2_fakes import FakeSessionRepository, FakeUserRepository

NOW = datetime(2026, 4, 1, 8, 0, tzinfo=UTC)
EMAIL = "candidate@example.com"
OTHER_EMAIL = "someone.else@example.com"
PASSWORD = SecretStr("correct horse battery staple")
WRONG_PASSWORD = SecretStr("incorrect horse battery staple")


def build_service(
    settings: AuthSettings | None = None,
) -> tuple[AuthenticationService, FakeUserRepository, FakeSessionRepository]:
    """A service over fresh fakes, plus the fakes, so a test can inspect the store."""
    users = FakeUserRepository()
    sessions = FakeSessionRepository()
    service = AuthenticationService(users, sessions, settings or AuthSettings())
    return service, users, sessions


@pytest.mark.asyncio
async def test_register_signs_the_new_account_in() -> None:
    """Registration returns a live session, because onboarding is the next screen."""
    service, users, sessions = build_service()

    signed_in = await service.register(
        email=EMAIL, password=PASSWORD, display_name="Candidate", now=NOW)

    assert signed_in.user.email == EMAIL
    assert signed_in.session.user_id == signed_in.user.id
    assert signed_in.session.is_usable(NOW)
    assert len(users.users) == 1
    assert len(sessions.sessions) == 1


@pytest.mark.asyncio
async def test_register_stores_a_hash_and_not_the_password() -> None:
    """The stored credential must be an Argon2id hash, verifiable but not readable."""
    service, users, _ = build_service()

    signed_in = await service.register(
        email=EMAIL, password=PASSWORD, display_name=None, now=NOW)

    stored = users.users[signed_in.user.id].password_hash.get_secret_value()
    assert stored.startswith("$argon2id$")
    assert PASSWORD.get_secret_value() not in stored


@pytest.mark.asyncio
async def test_register_normalizes_the_address_it_stores() -> None:
    """`Candidate@Example.COM ` and `candidate@example.com` are one account."""
    service, _, _ = build_service()

    signed_in = await service.register(
        email="  Candidate@Example.COM  ", password=PASSWORD, display_name=None,
        now=NOW)

    assert signed_in.user.email == EMAIL
    with pytest.raises(EmailAlreadyRegistered):
        await service.register(email=EMAIL, password=PASSWORD, display_name=None,
                               now=NOW)


@pytest.mark.asyncio
async def test_register_refuses_a_duplicate_address() -> None:
    service, _, _ = build_service()
    await service.register(email=EMAIL, password=PASSWORD, display_name=None, now=NOW)

    with pytest.raises(EmailAlreadyRegistered):
        await service.register(email=EMAIL, password=WRONG_PASSWORD,
                               display_name=None, now=NOW)


@pytest.mark.asyncio
async def test_the_two_tokens_are_independent_secrets() -> None:
    """The CSRF cookie is JS-readable, so it must not be the session token."""
    service, _, _ = build_service()

    signed_in = await service.register(
        email=EMAIL, password=PASSWORD, display_name=None, now=NOW)

    assert signed_in.token.get_secret_value() != signed_in.csrf_token.get_secret_value()
    assert digest_of(signed_in.token).get_secret_value() == \
        signed_in.session.token_digest.get_secret_value()
    assert digest_of(signed_in.csrf_token).get_secret_value() == \
        signed_in.session.csrf_token_digest.get_secret_value()


@pytest.mark.asyncio
async def test_log_in_with_the_right_password_issues_a_second_session() -> None:
    """Logging in again does not disturb the first session: two browsers, two rows."""
    service, _, sessions = build_service()
    first = await service.register(email=EMAIL, password=PASSWORD, display_name=None,
                                   now=NOW)

    second = await service.log_in(email=EMAIL, password=PASSWORD,
                                  now=NOW + timedelta(hours=1))

    assert second.session.id != first.session.id
    assert len(sessions.sessions) == 2
    assert sessions.sessions[first.session.id].revoked_at is None
    assert second.user.last_login_at == NOW + timedelta(hours=1)
    assert second.user.failed_login_attempts == 0


@pytest.mark.asyncio
async def test_an_unknown_address_and_a_wrong_password_are_indistinguishable() -> None:
    """The enumeration property: one exception type, and no account is created."""
    service, users, _ = build_service()
    await service.register(email=EMAIL, password=PASSWORD, display_name=None, now=NOW)

    with pytest.raises(InvalidCredentials):
        await service.log_in(email=OTHER_EMAIL, password=PASSWORD, now=NOW)
    with pytest.raises(InvalidCredentials):
        await service.log_in(email=EMAIL, password=WRONG_PASSWORD, now=NOW)

    assert len(users.users) == 1


@pytest.mark.asyncio
async def test_a_failed_login_against_an_unknown_address_writes_nothing() -> None:
    """Timing is equalized by spending hash time, not by inserting a row."""
    service, users, sessions = build_service()

    with pytest.raises(InvalidCredentials):
        await service.log_in(email=EMAIL, password=PASSWORD, now=NOW)

    assert users.users == {}
    assert sessions.sessions == {}


@pytest.mark.asyncio
async def test_failures_accumulate_and_the_tenth_locks_the_account() -> None:
    service, users, _ = build_service()
    signed_in = await service.register(email=EMAIL, password=PASSWORD,
                                       display_name=None, now=NOW)

    for attempt in range(1, 10):
        with pytest.raises(InvalidCredentials):
            await service.log_in(email=EMAIL, password=WRONG_PASSWORD, now=NOW)
        stored = users.users[signed_in.user.id]
        assert stored.failed_login_attempts == attempt
        # Nine failures are counted and forgiven. The lock arrives on the tenth,
        # which is the loop's exit condition rather than one of its iterations.
        assert not stored.is_locked(NOW)

    with pytest.raises(InvalidCredentials):
        await service.log_in(email=EMAIL, password=WRONG_PASSWORD, now=NOW)
    locked = users.users[signed_in.user.id]
    assert locked.failed_login_attempts == 10
    assert locked.locked_until == NOW + timedelta(minutes=15)
    assert locked.is_locked(NOW)


@pytest.mark.asyncio
async def test_a_correct_password_against_a_locked_account_says_locked() -> None:
    """Only ownership unlocks the disclosure — and no session is issued."""
    service, users, sessions = build_service(AuthSettings(max_failed_logins=1))
    signed_in = await service.register(email=EMAIL, password=PASSWORD,
                                       display_name=None, now=NOW)
    sessions.sessions.clear()
    with pytest.raises(InvalidCredentials):
        await service.log_in(email=EMAIL, password=WRONG_PASSWORD, now=NOW)

    with pytest.raises(AccountLocked) as refusal:
        await service.log_in(email=EMAIL, password=PASSWORD, now=NOW)

    assert refusal.value.locked_until == NOW + timedelta(minutes=15)
    assert sessions.sessions == {}
    assert users.users[signed_in.user.id].last_login_at is None


@pytest.mark.asyncio
async def test_attempts_during_a_lock_do_not_extend_it() -> None:
    """Otherwise anybody could keep a known address locked out for ever."""
    service, users, _ = build_service(AuthSettings(max_failed_logins=1))
    signed_in = await service.register(email=EMAIL, password=PASSWORD,
                                       display_name=None, now=NOW)
    with pytest.raises(InvalidCredentials):
        await service.log_in(email=EMAIL, password=WRONG_PASSWORD, now=NOW)
    locked_until = users.users[signed_in.user.id].locked_until

    for minute in range(1, 6):
        with pytest.raises(InvalidCredentials):
            await service.log_in(email=EMAIL, password=WRONG_PASSWORD,
                                 now=NOW + timedelta(minutes=minute))

    stored = users.users[signed_in.user.id]
    assert stored.locked_until == locked_until
    assert stored.failed_login_attempts == 1


@pytest.mark.asyncio
async def test_an_expired_lock_lets_the_right_password_in() -> None:
    service, _, _ = build_service(AuthSettings(max_failed_logins=1))
    await service.register(email=EMAIL, password=PASSWORD, display_name=None, now=NOW)
    with pytest.raises(InvalidCredentials):
        await service.log_in(email=EMAIL, password=WRONG_PASSWORD, now=NOW)
    after_lock = NOW + timedelta(minutes=16)

    signed_in = await service.log_in(email=EMAIL, password=PASSWORD, now=after_lock)

    assert signed_in.user.locked_until is None
    assert signed_in.user.failed_login_attempts == 0


@pytest.mark.asyncio
async def test_an_expired_lock_resets_the_counter_instead_of_re_locking() -> None:
    """A single typo after a lockout must not lock the account again."""
    service, users, _ = build_service(AuthSettings(max_failed_logins=2))
    signed_in = await service.register(email=EMAIL, password=PASSWORD,
                                       display_name=None, now=NOW)
    for _ in range(2):
        with pytest.raises(InvalidCredentials):
            await service.log_in(email=EMAIL, password=WRONG_PASSWORD, now=NOW)
    assert users.users[signed_in.user.id].is_locked(NOW)
    after_lock = NOW + timedelta(minutes=16)

    with pytest.raises(InvalidCredentials):
        await service.log_in(email=EMAIL, password=WRONG_PASSWORD, now=after_lock)

    stored = users.users[signed_in.user.id]
    assert stored.failed_login_attempts == 1
    assert stored.locked_until is None


@pytest.mark.asyncio
async def test_a_disabled_account_is_refused_only_after_a_correct_password() -> None:
    service, users, _ = build_service()
    signed_in = await service.register(email=EMAIL, password=PASSWORD,
                                       display_name=None, now=NOW)
    await users.upsert(signed_in.user.model_copy(
        update={"status": UserStatus.DISABLED}))

    with pytest.raises(InvalidCredentials):
        await service.log_in(email=EMAIL, password=WRONG_PASSWORD, now=NOW)
    with pytest.raises(AccountDisabled):
        await service.log_in(email=EMAIL, password=PASSWORD, now=NOW)


@pytest.mark.asyncio
async def test_authenticate_resolves_a_live_token() -> None:
    service, _, _ = build_service()
    signed_in = await service.register(email=EMAIL, password=PASSWORD,
                                       display_name=None, now=NOW)

    resolved = await service.authenticate(token=signed_in.token, now=NOW)

    assert resolved is not None
    assert resolved.user.id == signed_in.user.id
    assert resolved.session.id == signed_in.session.id


@pytest.mark.asyncio
async def test_authenticate_refuses_an_unknown_token() -> None:
    service, _, _ = build_service()
    await service.register(email=EMAIL, password=PASSWORD, display_name=None, now=NOW)

    assert await service.authenticate(token=new_token(), now=NOW) is None


@pytest.mark.asyncio
async def test_authenticate_refuses_an_expired_session() -> None:
    """The lifetime is absolute: seven days after issue, the cookie is dead."""
    service, _, _ = build_service()
    signed_in = await service.register(email=EMAIL, password=PASSWORD,
                                       display_name=None, now=NOW)

    assert await service.authenticate(
        token=signed_in.token, now=NOW + timedelta(hours=167)) is not None
    assert await service.authenticate(
        token=signed_in.token, now=NOW + timedelta(hours=169)) is None


@pytest.mark.asyncio
async def test_authenticate_refuses_a_revoked_session() -> None:
    service, _, _ = build_service()
    signed_in = await service.register(email=EMAIL, password=PASSWORD,
                                       display_name=None, now=NOW)

    assert await service.log_out(session=signed_in.session, now=NOW) is True

    assert await service.authenticate(token=signed_in.token, now=NOW) is None


@pytest.mark.asyncio
async def test_authenticate_refuses_a_disabled_accounts_live_session() -> None:
    """Disabling an account stops its existing cookies without a second statement."""
    service, users, _ = build_service()
    signed_in = await service.register(email=EMAIL, password=PASSWORD,
                                       display_name=None, now=NOW)
    await users.upsert(signed_in.user.model_copy(
        update={"status": UserStatus.DISABLED}))

    assert await service.authenticate(token=signed_in.token, now=NOW) is None


@pytest.mark.asyncio
async def test_the_touch_is_throttled_and_cannot_extend_the_session() -> None:
    service, _, sessions = build_service()
    signed_in = await service.register(email=EMAIL, password=PASSWORD,
                                       display_name=None, now=NOW)
    expires_at = signed_in.session.expires_at

    soon = NOW + SESSION_TOUCH_INTERVAL - timedelta(seconds=1)
    resolved = await service.authenticate(token=signed_in.token, now=soon)
    assert resolved is not None
    assert resolved.session.last_seen_at == NOW

    later = NOW + SESSION_TOUCH_INTERVAL + timedelta(seconds=1)
    resolved = await service.authenticate(token=signed_in.token, now=later)
    assert resolved is not None
    assert resolved.session.last_seen_at == later
    assert resolved.session.expires_at == expires_at
    assert sessions.sessions[signed_in.session.id].expires_at == expires_at


@pytest.mark.asyncio
async def test_log_out_is_idempotent_and_scoped_to_its_own_user() -> None:
    service, _, _ = build_service()
    mine = await service.register(email=EMAIL, password=PASSWORD, display_name=None,
                                  now=NOW)
    theirs = await service.register(email=OTHER_EMAIL, password=PASSWORD,
                                    display_name=None, now=NOW)

    assert await service.log_out(session=mine.session, now=NOW) is True
    assert await service.log_out(session=mine.session, now=NOW) is False

    # The other account's session is untouched by any of that.
    assert await service.authenticate(token=theirs.token, now=NOW) is not None


@pytest.mark.asyncio
async def test_log_out_cannot_revoke_another_users_session() -> None:
    """The forged pairing — my user id, their session id — must revoke nothing."""
    service, _, sessions = build_service()
    mine = await service.register(email=EMAIL, password=PASSWORD, display_name=None,
                                  now=NOW)
    theirs = await service.register(email=OTHER_EMAIL, password=PASSWORD,
                                    display_name=None, now=NOW)

    forged = theirs.session.model_copy(update={"user_id": mine.user.id})
    assert await service.log_out(session=forged, now=NOW) is False

    assert sessions.sessions[theirs.session.id].revoked_at is None


@pytest.mark.asyncio
async def test_log_out_everywhere_revokes_this_account_only() -> None:
    service, _, _ = build_service()
    mine = await service.register(email=EMAIL, password=PASSWORD, display_name=None,
                                  now=NOW)
    second = await service.log_in(email=EMAIL, password=PASSWORD, now=NOW)
    theirs = await service.register(email=OTHER_EMAIL, password=PASSWORD,
                                    display_name=None, now=NOW)

    assert await service.log_out_everywhere(user_id=mine.user.id, now=NOW) == 2

    assert await service.authenticate(token=mine.token, now=NOW) is None
    assert await service.authenticate(token=second.token, now=NOW) is None
    assert await service.authenticate(token=theirs.token, now=NOW) is not None
    assert await service.log_out_everywhere(user_id=mine.user.id, now=NOW) == 0


@pytest.mark.asyncio
async def test_expired_sessions_can_be_swept() -> None:
    """`delete_expired` is the housekeeping half of an absolute lifetime."""
    service, _, sessions = build_service()
    signed_in = await service.register(email=EMAIL, password=PASSWORD,
                                       display_name=None, now=NOW)

    assert await sessions.delete_expired(NOW + timedelta(hours=1)) == 0
    assert await sessions.delete_expired(NOW + timedelta(days=8)) == 1
    assert signed_in.session.id not in sessions.sessions


def test_csrf_token_matches_only_the_token_issued_with_the_session() -> None:
    """A planted cookie value cannot satisfy a check against server-side state."""
    token, csrf_token, other = new_token(), new_token(), new_token()
    session = _session_with(token, csrf_token)

    assert csrf_token_matches(session, csrf_token.get_secret_value()) is True
    assert csrf_token_matches(session, other.get_secret_value()) is False
    # The session token is not an acceptable CSRF token, even though the caller
    # holds it: the two digests are checked against different columns.
    assert csrf_token_matches(session, token.get_secret_value()) is False


def test_a_missing_csrf_header_is_a_mismatch_not_an_error() -> None:
    session = _session_with(new_token(), new_token())

    assert csrf_token_matches(session, None) is False
    assert csrf_token_matches(session, "") is False


def _session_with(token: SecretStr, csrf_token: SecretStr) -> UserSession:
    """A session carrying these two tokens' digests, for the CSRF matcher tests."""
    return UserSession(
        id=new_user_session_id(), user_id=new_user_id(),
        token_digest=digest_of(token), csrf_token_digest=digest_of(csrf_token),
        issued_at=NOW, expires_at=NOW + timedelta(days=7), last_seen_at=NOW)
