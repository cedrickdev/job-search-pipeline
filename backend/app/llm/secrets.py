"""Encrypting a provider credential at rest, with a key that never touches the DB.

A remote LLM connection carries an API key, and that key is stored — so it is stored
encrypted (docs/LLM_PROVIDER_ARCHITECTURE.md §21). The rules this module enforces are
the ones that make "encrypted at rest" mean something rather than being decoration:

- **An established AEAD, never home-grown crypto.** Fernet — AES-128-CBC with an
  HMAC-SHA256 authentication tag — from the `cryptography` library. It is
  authenticated, so a tampered ciphertext fails to decrypt rather than yielding
  garbage a caller might use, and it is a scheme with a specification and a review
  history, which a hand-rolled XOR is not (docs/ENGINEERING_STANDARDS.md §Security).
- **The master key comes from the environment, and is never persisted or returned.**
  It is read once at startup into a `SecretCipher`; it is not a column, not part of
  any API response, not written to a log. A database dump therefore contains
  ciphertext and a version tag and nothing that decrypts them.
- **Only the ciphertext and a `secret_version` are stored.** The version tags which
  key encrypted a value, so a key rotation can re-encrypt old rows without guessing
  which key each used — a value at `secret_version` N is decryptable by the key
  registered under N.

`SecretCipher` is a Protocol so a test can substitute a trivial in-memory cipher and
the persistence tests do not need a real key ceremony, while production wires
`FernetSecretCipher` built from the configured key. The redaction rule for *display*
lives elsewhere (a stored secret is surfaced as `has_api_key: true`, never as its
value, §22); this module only encrypts and decrypts.
"""
from typing import Protocol, runtime_checkable

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

# The version tag written beside every ciphertext this module produces. A single
# active key today; a rotation registers a second cipher under version 2 and
# re-encrypts version-1 rows, and the tag is what lets that happen without a guess.
CURRENT_SECRET_VERSION = 1


class SecretDecryptionError(Exception):
    """A stored ciphertext could not be decrypted with the key for its version.

    A tampered value, a wrong or rotated-away master key, or a version this process
    has no key for. Raised rather than returning a broken plaintext, because a
    credential that silently decrypts to garbage would be sent to a provider as if it
    were real. The message names no key material.
    """

    def __init__(self, secret_version: int) -> None:
        super().__init__(
            f"the stored secret at version {secret_version} could not be decrypted")
        self.secret_version = secret_version


@runtime_checkable
class SecretCipher(Protocol):
    """Encrypts and decrypts a provider credential at a known version.

    The seam persistence depends on: a service holds a `SecretCipher`, encrypts a
    key before it is stored and decrypts it before a provider is built, and never
    sees the master key itself. A test substitutes an in-memory implementation; the
    contract is exactly these three members.
    """

    @property
    def version(self) -> int:
        """The `secret_version` this cipher writes and can read."""
        ...

    def encrypt(self, plaintext: SecretStr) -> str:
        """Encrypt a secret to an opaque, storable string."""
        ...

    def decrypt(self, ciphertext: str, *, secret_version: int) -> SecretStr:
        """Decrypt a stored ciphertext, or raise `SecretDecryptionError`."""
        ...


class FernetSecretCipher:
    """A `SecretCipher` over Fernet, keyed from the configured master key.

    Holds the active cipher and any older ciphers a rotation registered, keyed by
    version, so a value stored under an earlier version still decrypts. `encrypt`
    always writes at the active version; `decrypt` selects the cipher for the value's
    stored version and fails loudly if it holds none.
    """

    def __init__(self, key: str, *, version: int = CURRENT_SECRET_VERSION,
                 older: dict[int, str] | None = None) -> None:
        self._version = version
        self._ciphers: dict[int, Fernet] = {version: Fernet(key.encode("utf-8"))}
        for old_version, old_key in (older or {}).items():
            self._ciphers[old_version] = Fernet(old_key.encode("utf-8"))

    @property
    def version(self) -> int:
        return self._version

    def encrypt(self, plaintext: SecretStr) -> str:
        token = self._ciphers[self._version].encrypt(
            plaintext.get_secret_value().encode("utf-8"))
        return token.decode("ascii")

    def decrypt(self, ciphertext: str, *, secret_version: int) -> SecretStr:
        cipher = self._ciphers.get(secret_version)
        if cipher is None:
            raise SecretDecryptionError(secret_version)
        try:
            plaintext = cipher.decrypt(ciphertext.encode("ascii"))
        except (InvalidToken, ValueError) as exc:
            raise SecretDecryptionError(secret_version) from exc
        return SecretStr(plaintext.decode("utf-8"))


def generate_master_key() -> str:
    """A fresh Fernet master key, url-safe base64. For an operator's key ceremony.

    Not called at runtime — a deployment generates one, stores it in a secret
    manager, and sets it in the environment. Exposed here (and by a tiny CLI) so the
    key is produced by the same library that consumes it, rather than by an ad-hoc
    `openssl` line an operator might get wrong.
    """
    return Fernet.generate_key().decode("ascii")
