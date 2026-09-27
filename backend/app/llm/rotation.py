"""Re-encrypting every stored credential under a new key version (Phase 16 §22).

Rotation is the operational counterpart of `backend.app.llm.secrets`: that module
encrypts a credential at the active version and tags the row with it; this one walks the
rows a previous key wrote and brings them up to the active version, so an old key can
finally be retired. It never invents crypto and never logs a secret — it decrypts a value
under the key for its stored version, re-encrypts it under the active version, and
verifies the new ciphertext reads back to the same plaintext *before* it is persisted, so
a value that cannot be read back is reported rather than written.

The three rules that make a rotation safe to run and safe to re-run:

- **Idempotent.** A row already at the active version is left untouched, so a second run
  (or a run after a crash) does no work and produces the same state.
- **Fail-closed, never destructive.** A row whose previous key is not configured cannot be
  decrypted; it becomes a typed `CredentialRotationFailure` — naming the connection and
  the version, never the value — and is left exactly as it was, so a missing key loses no
  credential. The operator supplies the key and re-runs.
- **No silent downgrade.** A row stored under a version *newer* than the active key means
  the active version was set too low; rotating it down would weaken it, so it is a failure,
  not a write.

The plaintext exists only for the instant between decrypt and re-encrypt, as a
`SecretStr` that does not print in a traceback, and never enters the report or the store.
"""
from collections.abc import Sequence
from dataclasses import dataclass, field

from backend.app.domain.identifiers import LLMConnectionId
from backend.app.llm.connection import LLMConnection
from backend.app.llm.secrets import SecretCipher, SecretDecryptionError
from backend.app.repositories.contracts import CredentialRotationStore

# A generous default page size for keyset paging. Credential tables are small (one row
# per configured API connection), so this reads the whole table in one page in practice
# while still bounding memory for a deployment that somehow has many.
DEFAULT_ROTATION_BATCH: int = 100


@dataclass(frozen=True)
class CredentialRotationFailure:
    """One credential a rotation could not safely re-encrypt — secret-free by design.

    Names the connection and the version it was stored under so an operator can act,
    and a `reason` that is a fixed sentence, never the ciphertext or the plaintext. A
    failure means the row was left exactly as it was: a rotation never writes a value it
    could not first read back.
    """

    connection_id: LLMConnectionId
    stored_version: int
    reason: str


@dataclass(frozen=True)
class CredentialRotationReport:
    """The tally a rotation returns — counts and typed failures, no key material.

    `rotated` is the number brought up to `active_version`; on a `dry_run` it is the
    number that *would* be, since nothing is written. `already_current` were at the
    active version already (the idempotent case). `failed` mirrors `len(failures)`.
    """

    active_version: int
    scanned: int
    rotated: int
    already_current: int
    dry_run: bool = False
    failures: tuple[CredentialRotationFailure, ...] = field(default_factory=tuple)

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def is_clean(self) -> bool:
        """Whether every credential is now (or would be) at the active version."""
        return self.failed == 0


class CredentialRotationService:
    """Walks every stored credential and re-encrypts the stale ones at the active version.

    Holds the `CredentialRotationStore` it pages over — all owners, credentials only —
    and the `SecretCipher` built with the active key and any rotation `previous_keys`. It
    never sees a master key itself; the cipher does the crypto and this orchestrates the
    walk, the verification and the write.
    """

    def __init__(self, store: CredentialRotationStore, cipher: SecretCipher, *,
                 batch_size: int = DEFAULT_ROTATION_BATCH) -> None:
        self._store = store
        self._cipher = cipher
        self._batch_size = batch_size

    async def rotate(self, *, dry_run: bool = False) -> CredentialRotationReport:
        """Bring every stored credential up to the active version; report what happened.

        Pages by id so the walk is bounded and cannot reprocess a row it just rewrote.
        On `dry_run` the decrypt/re-encrypt/verify still runs for every stale row — so a
        dry run proves the configured keys can actually rotate the table — but nothing is
        persisted.
        """
        active = self._cipher.version
        scanned = rotated = already = 0
        failures: list[CredentialRotationFailure] = []
        after_id: LLMConnectionId | None = None
        while True:
            batch = await self._store.list_credentialed(
                after_id=after_id, limit=self._batch_size)
            if not batch:
                break
            for connection in batch:
                scanned += 1
                after_id = connection.id
                outcome = self._plan(connection, active)
                if isinstance(outcome, CredentialRotationFailure):
                    failures.append(outcome)
                elif outcome is None:
                    already += 1
                else:
                    if not dry_run:
                        await self._store.reencrypt(
                            connection.id, ciphertext=outcome, secret_version=active)
                    rotated += 1
            if len(batch) < self._batch_size:
                break
        return CredentialRotationReport(
            active_version=active, scanned=scanned, rotated=rotated,
            already_current=already, dry_run=dry_run, failures=tuple(failures))

    def _plan(self, connection: LLMConnection,
              active: int) -> str | None | CredentialRotationFailure:
        """The new ciphertext for a stale row, `None` if current, or a typed failure.

        Verifies the round trip before returning the ciphertext, so the caller only ever
        persists a value it has read back to the original under the active key.
        """
        ciphertext = connection.encrypted_api_key
        version = connection.secret_version
        if ciphertext is None or version is None:
            # The store returns only credentialed rows and the CHECK makes the pair
            # both-or-neither, so this is unreachable; kept because the type is optional.
            return None
        if version == active:
            return None
        if version > active:
            return CredentialRotationFailure(
                connection_id=connection.id, stored_version=version,
                reason=(f"stored under version {version}, newer than the active version "
                        f"{active}; a rotation never downgrades — raise the active "
                        "version to at least the newest stored version"))
        return self._reencrypt(connection.id, ciphertext, version, active)

    def _reencrypt(self, connection_id: LLMConnectionId, ciphertext: str,
                   stored_version: int, active: int) -> str | CredentialRotationFailure:
        try:
            plaintext = self._cipher.decrypt(ciphertext, secret_version=stored_version)
        except SecretDecryptionError:
            return CredentialRotationFailure(
                connection_id=connection_id, stored_version=stored_version,
                reason=(f"could not be decrypted with a key for version {stored_version}; "
                        "supply that previous key and re-run"))
        new_ciphertext = self._cipher.encrypt(plaintext)
        try:
            verified = self._cipher.decrypt(new_ciphertext, secret_version=active)
        except SecretDecryptionError:
            return CredentialRotationFailure(
                connection_id=connection_id, stored_version=stored_version,
                reason="the re-encrypted value did not decrypt under the active key")
        if verified.get_secret_value() != plaintext.get_secret_value():
            return CredentialRotationFailure(
                connection_id=connection_id, stored_version=stored_version,
                reason="the re-encrypted value did not round-trip to the original")
        return new_ciphertext


def format_rotation_report(report: CredentialRotationReport) -> str:
    """A human-readable, secret-free summary of a rotation, for the CLI.

    Lists each failure by connection id, stored version and reason — enough for an
    operator to act, and nothing that could leak a credential.
    """
    verb = "would rotate" if report.dry_run else "rotated"
    lines = [
        f"credential rotation ({'dry run' if report.dry_run else 'applied'})",
        f"  active version   {report.active_version}",
        f"  scanned          {report.scanned}",
        f"  {verb:<15}{report.rotated}",
        f"  already current  {report.already_current}",
        f"  failed           {report.failed}",
    ]
    lines.extend(_failure_lines(report.failures))
    return "\n".join(lines)


def _failure_lines(failures: Sequence[CredentialRotationFailure]) -> list[str]:
    if not failures:
        return []
    lines = ["  failures:"]
    for failure in failures:
        lines.append(
            f"    {failure.connection_id} (v{failure.stored_version}): {failure.reason}")
    return lines


__all__ = [
    "DEFAULT_ROTATION_BATCH", "CredentialRotationFailure", "CredentialRotationReport",
    "CredentialRotationService", "format_rotation_report",
]
