"""Accounts and sessions — the identity half of "V2 is multi-user".

Phase 2 created a `users` table with a display name and nothing else, so that
user-owned rows could carry a real foreign key before anything could
authenticate. This module is the model those columns were waiting for.

Three decisions shape it, and each one is a constraint on what the rest of the
system can accidentally do:

**A credential is never a plain string.** `password_hash` and every token digest
that identifies a live session are `SecretStr`, so `repr(user)`,
`model_dump_json()` and any log line that formats a model print
`SecretStr('**********')` instead of the value. Reading one takes an explicit
`.get_secret_value()`, which is greppable — the audit question becomes "who
unwraps this?" rather than "where might this have been logged?"
(docs/ENGINEERING_STANDARDS.md §Security: redact secrets from errors and logs).

**A session is a digest, not a token.** The value handed to the browser exists
only in the response that sets the cookie; what is modelled, stored and compared
is its SHA-256. `SessionTokenDigest` is shaped to refuse anything that is not a
64-character hex digest, so a mistake that put the raw token where its hash
belongs fails validation instead of persisting a credential in plaintext.

**Nothing here reads a clock.** `is_usable` takes the instant to compare against,
like every other time-dependent domain method, so expiry is testable without
freezing time and a service cannot disagree with the model about what "now" is.
"""
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, EmailStr, Field, SecretStr, model_validator

from backend.app.domain.base import DomainModel, NonEmptyStr, UtcDatetime
from backend.app.domain.identifiers import UserId, UserSessionId


def normalize_email(value: str) -> str:
    """Trim and lower-case, so one person has one account.

    The local part of an address is case-sensitive per RFC 5321 and treated
    case-insensitively by every mail provider in practice. Following the RFC here
    would mean `Ada@example.com` and `ada@example.com` could both register, then
    both fail to log in as the other — a support problem with no upside. The
    normalized form is what is stored and what the unique index covers.

    Public, because a lookup by email has to apply exactly the same rule as the
    write did: `SqlAlchemyUserRepository.get_by_email` calls this rather than
    lower-casing on its own, so the two can never drift apart.
    """
    return value.strip().lower()


# A deliverable address, normalized. `EmailStr` needs the `email-validator`
# package, which is why it is a declared runtime dependency rather than a test
# extra: this is the login identifier.
EmailAddress = Annotated[EmailStr, AfterValidator(normalize_email)]


def _require_sha256_hex(value: SecretStr) -> SecretStr:
    """Refuse anything that is not a lower-case SHA-256 digest.

    The guard that matters: `secrets.token_urlsafe(32)` produces 43 characters of
    URL-safe base64, so a raw session token assigned to a digest field is
    rejected here rather than written to the database in plaintext.
    """
    digest = value.get_secret_value()
    if len(digest) != 64 or not all(char in "0123456789abcdef" for char in digest):
        raise ValueError("expected a lower-case hex SHA-256 digest (64 characters)")
    return value


# The stored form of a session token or a CSRF token: its SHA-256, hex-encoded.
# Wrapped in `SecretStr` even though a digest is not reversible — it still
# identifies a live session, and the point of the wrapper is that no code path
# prints one by accident.
SessionTokenDigest = Annotated[SecretStr, AfterValidator(_require_sha256_hex)]


class UserStatus(StrEnum):
    """Whether the account may be used at all.

    Two members, because two is what Phase 4 can enforce honestly. `DISABLED`
    refuses both a login and an already-issued session, so an operator can stop
    an account with one `UPDATE` — the mechanism is live and tested even though no
    endpoint sets it. A `PENDING_VERIFICATION` member would be decoration until
    the phase that can actually send an email.
    """

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class User(DomainModel):
    """One account.

    Deliberately not a candidate: `CandidateProfile` is what the platform knows
    about a person, and one account legitimately owns more than one profile
    (docs/ARCHITECTURE.md §5). What lives here is what authentication needs — an
    identifier, a credential, and the counters that decide whether a login
    attempt is allowed to proceed.

    `onboarding_completed_at` is on the account rather than on the profile
    because it is a fact about the *account's* setup: onboarding produces a
    profile and a search profile, so a flag on either one could not express
    "both are done".
    """

    id: UserId
    email: EmailAddress
    # Argon2id, as produced by `backend.app.core.passwords`. The domain does not
    # know the algorithm and must not: rehashing on a parameter change is a core
    # concern, and a model that parsed the string would have to be migrated with
    # it.
    password_hash: SecretStr
    status: UserStatus = UserStatus.ACTIVE
    display_name: NonEmptyStr | None = None
    # Stays NULL for every account in Phase 4: nothing sends mail yet, and a
    # column that claimed verification without a verifier would be a lie the
    # authorization layer might later trust.
    email_verified_at: UtcDatetime | None = None
    last_login_at: UtcDatetime | None = None
    failed_login_attempts: Annotated[int, Field(ge=0)] = 0
    locked_until: UtcDatetime | None = None
    onboarding_completed_at: UtcDatetime | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _timestamps_are_ordered(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("User updated_at must not precede created_at")
        return self

    def is_locked(self, as_of: datetime) -> bool:
        """Whether a temporary lockout is still in force at `as_of`.

        Temporary by design: a permanent lock on repeated failures is a denial of
        service anybody can trigger against a known address. The window expires,
        and `status` is the switch for a decision a human took.
        """
        return self.locked_until is not None and self.locked_until > as_of

    def may_authenticate(self, as_of: datetime) -> bool:
        """Whether a password check should even be attempted."""
        return self.status is UserStatus.ACTIVE and not self.is_locked(as_of)

    @property
    def has_completed_onboarding(self) -> bool:
        return self.onboarding_completed_at is not None


class UserSession(DomainModel):
    """One authenticated browser, as the server remembers it.

    Server-side and revocable, which is the reason it exists as a table at all: a
    self-contained signed token cannot be logged out before it expires, and
    "revoke my other sessions" is a requirement any account holder eventually
    has. The cost is one indexed lookup per request, which is the correct trade
    for a session store.

    The CSRF digest lives here rather than in a second table because a CSRF token
    has exactly the lifetime of the session it protects. Keeping it beside the
    session is also what upgrades the double-submit cookie into a check against
    server-side state: a token planted in the cookie jar by a sibling host cannot
    match this digest (docs/AUTHENTICATION.md §CSRF).
    """

    id: UserSessionId
    user_id: UserId
    token_digest: SessionTokenDigest
    csrf_token_digest: SessionTokenDigest
    issued_at: UtcDatetime
    expires_at: UtcDatetime
    last_seen_at: UtcDatetime
    revoked_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _window_is_coherent(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("UserSession expires_at must follow issued_at")
        if self.last_seen_at < self.issued_at:
            raise ValueError("UserSession last_seen_at must not precede issued_at")
        return self

    @model_validator(mode="after")
    def _the_two_digests_differ(self) -> Self:
        """A session token and its CSRF token must not be the same value.

        The CSRF token is delivered in a cookie JavaScript can read, and the
        session token in one it cannot. Issuing the same secret twice would hand
        the session to any script on the page — so the invariant is asserted where
        it cannot be forgotten rather than trusted to the issuing code.
        """
        if self.token_digest.get_secret_value() == \
                self.csrf_token_digest.get_secret_value():
            raise ValueError("session and CSRF tokens must be independently generated")
        return self

    def is_usable(self, as_of: datetime) -> bool:
        """Neither revoked nor expired at `as_of`."""
        return self.revoked_at is None and self.expires_at > as_of
