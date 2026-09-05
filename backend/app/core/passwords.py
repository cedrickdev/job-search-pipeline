"""Password hashing, and the two rules a caller must not be able to get wrong.

**Argon2id, with the library's own defaults.** `PasswordHasher()` is
`m=64 MiB, t=3, p=4` in argon2-cffi 25.x — the RFC 9106 second recommended
parameter set, which the maintainers track. Hard-coding numbers here would freeze
2026's cost into the code; taking the default means a dependency bump raises it,
and `verify_password` reports when a stored hash was made with the older
parameters so the next successful login re-hashes it. That is why
`check_needs_rehash` is part of the return value rather than an optional extra a
caller may forget to ask about.

**A hash is a `SecretStr` on both sides.** The plaintext arrives as one and the
digest leaves as one, so neither can reach a log line, a traceback or a
`model_dump_json()` without an explicit `get_secret_value()`
(docs/ENGINEERING_STANDARDS.md §Security). The only place in the backend that
unwraps either is this module.

There are no composition rules — no required digit, no required symbol. NIST
SP 800-63B advises against them: they push users towards `Password1!` and rule out
long passphrases for no measured gain. Length is what is checked, at both ends:
`MINIMUM_PASSWORD_LENGTH` because short passwords are guessable, and
`MAXIMUM_PASSWORD_LENGTH` because Argon2 will happily spend a second hashing a
one-megabyte string, which is a denial of service anybody can post.
"""
import secrets
from typing import Final, NamedTuple

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from pydantic import SecretStr

# 12 rather than 8: an eight-character password is within reach of an offline
# attack even against Argon2 if the hash ever leaks, and a length minimum is the
# one requirement users can satisfy with a passphrase.
MINIMUM_PASSWORD_LENGTH: Final[int] = 12

# Generous, and still a bound. 1024 characters is longer than any passphrase
# anybody types and short enough that hashing it costs what hashing 20 costs.
MAXIMUM_PASSWORD_LENGTH: Final[int] = 1024

# One hasher for the process: it is stateless and thread-safe, and constructing
# one per call would allocate the 64 MiB buffer each time.
_HASHER: Final[PasswordHasher] = PasswordHasher()

# A hash of a value no password can equal, used to spend the same time on an
# unknown address as on a known one. Built on first use rather than at import,
# because paying 50 ms at import would tax `scripts/dump_openapi.py` and every
# test that only builds the app.
_UNKNOWN_USER_HASH: list[str] = []


class PasswordCheck(NamedTuple):
    """The outcome of one verification.

    Two fields because a correct password against an outdated hash is both a
    success and a maintenance task, and returning a bare `bool` is how the second
    half gets lost.
    """

    matched: bool
    needs_rehash: bool


def hash_password(password: SecretStr) -> SecretStr:
    """Hash a plaintext password with Argon2id.

    Raises `ValueError` on a password outside the accepted length band. The check
    lives here, not only in the API schema, so a service or a future CLI cannot
    store a one-character password by bypassing the HTTP layer.
    """
    plaintext = password.get_secret_value()
    if len(plaintext) < MINIMUM_PASSWORD_LENGTH:
        raise ValueError(
            f"password must be at least {MINIMUM_PASSWORD_LENGTH} characters")
    if len(plaintext) > MAXIMUM_PASSWORD_LENGTH:
        raise ValueError(
            f"password must be at most {MAXIMUM_PASSWORD_LENGTH} characters")
    return SecretStr(_HASHER.hash(plaintext))


def verify_password(password: SecretStr, stored_hash: SecretStr) -> PasswordCheck:
    """Check a password against a stored hash.

    Never raises for a wrong password, and never raises for a malformed stored
    hash either: both are `matched=False`. An exception here would have to be
    handled by the login route, and the handler that distinguishes "wrong
    password" from "corrupt hash" in a response is the handler that tells an
    attacker which addresses are registered.
    """
    try:
        _HASHER.verify(stored_hash.get_secret_value(), password.get_secret_value())
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return PasswordCheck(matched=False, needs_rehash=False)
    # `check_needs_rehash` parses the parameters recorded in the digest, so this
    # is a comparison against the current cost, not a guess about its age.
    return PasswordCheck(
        matched=True,
        needs_rehash=_HASHER.check_needs_rehash(stored_hash.get_secret_value()))


def spend_verification_time(password: SecretStr) -> None:
    """Hash-verify against a throwaway digest, and discard the result.

    Called on the "no such account" path. Without it, a login attempt for an
    unknown address returns in a fraction of the time one for a known address
    takes, and that difference is a working account-enumeration oracle — which
    matters more here than usual, because the identifier is an email address.

    Equalizing the time rather than the response is the point: the response is
    already identical (docs/AUTHENTICATION.md §Enumeration).
    """
    if not _UNKNOWN_USER_HASH:
        # Not a credential: 32 random bytes generated in this process, never
        # stored and never compared against anything. Its only purpose is to give
        # `verify` a well-formed digest to do its full work against and fail.
        _UNKNOWN_USER_HASH.append(_HASHER.hash(secrets.token_urlsafe(32)))
    verify_password(password, SecretStr(_UNKNOWN_USER_HASH[0]))
