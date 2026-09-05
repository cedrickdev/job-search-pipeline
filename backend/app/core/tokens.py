"""Session and CSRF tokens: minted here, hashed here, never stored in the clear.

Two functions and a constant, and the whole point is that they are the only ones.
A second place that minted a token would eventually mint a shorter one, and a
second place that hashed one would eventually forget the encoding.

**Why SHA-256 and not Argon2.** A password is a low-entropy secret a human chose,
so verifying it must be deliberately slow. A session token is 32 bytes from
`secrets.token_urlsafe`, which is 256 bits of uniform randomness with no
dictionary to guess from: a slow KDF would add nothing an attacker has to defeat,
while costing ~100 ms on the session lookup that happens on *every* authenticated
request. SHA-256 is the right tool for hashing a high-entropy secret, and it keeps
the stored value a fixed 64 hex characters — which is what
`backend.app.domain.user.SessionTokenDigest` validates and what the unique index
covers.

**Why hash at all, if the digest is not reversible into a password.** Because a
database dump, a log line or a backup that contained raw session tokens would be a
set of working credentials. Hashed, it is a set of values that cannot be replayed.

The raw token exists in exactly two places in its life: the response that sets the
cookie, and the request header the browser sends back. Everything in between —
the model, the row, the index — holds a digest.
"""
import hashlib
import secrets
from typing import Final

from pydantic import SecretStr

# 32 bytes, which `token_urlsafe` renders as 43 URL-safe characters. Deliberately
# not 64 hex characters: the length difference is what makes a raw token fail
# `SessionTokenDigest` validation, so confusing the two cannot reach the database.
TOKEN_BYTES: Final[int] = 32


def new_token() -> SecretStr:
    """Mint one cryptographically random token.

    Used for both the session token and the CSRF token, and called twice per
    login: `UserSession` refuses a session whose two digests are equal, so the
    two values must come from two calls rather than one.
    """
    return SecretStr(secrets.token_urlsafe(TOKEN_BYTES))


def digest_of(token: SecretStr) -> SecretStr:
    """The stored form of a token: its SHA-256, lower-case hex.

    `SecretStr` in and `SecretStr` out. The digest is not a password, but it is
    still a live session identifier, and wrapping it is what stops one appearing
    in a traceback or a `model_dump_json()`.
    """
    raw = token.get_secret_value().encode("utf-8")
    return SecretStr(hashlib.sha256(raw).hexdigest())


def digests_match(left: SecretStr, right: SecretStr) -> bool:
    """Compare two digests in constant time.

    `secrets.compare_digest` rather than `==` for the CSRF check: `==` on strings
    returns as soon as two characters differ, and the timing of that return leaks
    how much of a guessed prefix was right. It matters less for a 256-bit value
    than the habit is worth — this is the comparison every future reader will copy.
    """
    return secrets.compare_digest(left.get_secret_value(), right.get_secret_value())
