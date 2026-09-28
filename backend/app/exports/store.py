"""Where an account export's archive lives, behind a storage-neutral protocol (§25).

An `AccountExport` domain object is a locator, not the bytes: it carries a `storage_key`
and never the archive itself, for the same reason a `DocumentVersion` carries a
`DocumentArtifactRef` rather than a PDF (docs/ARCHITECTURE.md §9). This module is the adapter
that turns that key into bytes and back, and — because §25 requires exports to expire — lets
a retention sweep delete them.

`AccountExportStore` is the port; `LocalAccountExportStore` is the only implementation M5
ships, a directory on disk with one JSON file per export keyed by owner and export id. An
object-store implementation (S3, GCS) would satisfy the same protocol without the service
above it changing — which is the whole point of §25's "do not couple the domain to the local
filesystem so production object storage can replace it later".

The one security concern is path traversal: a `storage_key` is derived from ids the service
controls, but the store still refuses any key that could escape its root, exactly as
`LocalDocumentArtifactStore` does, so the store is never the component that lets a `..` reach
outside the export directory (docs/ENGINEERING_STANDARDS.md §Security).
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from backend.app.domain.identifiers import AccountExportId, UserId

# The media type an export archive is served under. JSON today; stated once so the store and
# the download handler agree on it without a scattered literal.
ACCOUNT_EXPORT_MEDIA_TYPE = "application/json"


class ExportNotFound(Exception):
    """No archive is stored under the requested key.

    A domain-level condition, not an HTTP concern: the API layer maps it to a 404/409. Raised
    by `get` so a caller distinguishes "never produced / already expired" from an I/O error it
    should not swallow.
    """

    def __init__(self, storage_key: str) -> None:
        super().__init__(f"no stored account export for key {storage_key!r}")
        self.storage_key = storage_key


@dataclass(frozen=True)
class StoredExport:
    """Bytes read back from the store, with the metadata a download needs.

    Returned by `get` so the API can stream the archive with a correct `Content-Type` and
    length without the caller re-deriving them.
    """

    storage_key: str
    content: bytes
    media_type: str


@runtime_checkable
class AccountExportStore(Protocol):
    """Persist, retrieve and delete account-export archives (§25).

    Deliberately small: the export service gathers a user's data, serializes it, `put`s the
    bytes and stores the returned key on the `AccountExport`; a download handler `get`s it; a
    retention sweep `delete`s it. Nothing here knows about an export's lifecycle — that is the
    domain's and the service's job — so the store stays a dumb, testable blob sink.
    """

    def key_for(self, user_id: UserId, export_id: AccountExportId) -> str:
        """The storage key for one export's archive.

        Derived from the owner and export ids so it is stable and owner-scoped: an export's
        archive is found without a database lookup, and one user's key can never name another
        user's file.
        """
        ...

    def put(self, storage_key: str, content: bytes, *,
            media_type: str = ACCOUNT_EXPORT_MEDIA_TYPE) -> None:
        """Write `content` under `storage_key`, replacing any existing bytes."""
        ...

    def get(self, storage_key: str) -> StoredExport:
        """Read the bytes stored under `storage_key`, or raise `ExportNotFound`."""
        ...

    def delete(self, storage_key: str) -> bool:
        """Remove the archive under `storage_key`; return whether bytes were removed.

        Idempotent: deleting a key that is already gone is a no-op that returns `False` rather
        than raising, so a retention sweep rerun reaches the same state (§31).
        """
        ...


class LocalAccountExportStore:
    """An `AccountExportStore` backed by a directory on the local filesystem.

    One file per archive, named by its storage key, under `root`. The key is
    `exports/<user_id>/<export_id>.json` — a shallow, owner-scoped, id-derived layout, so the
    files are greppable in support and an export's archive is found without a database lookup.

    `root` is created on first write, not construction, so instantiating the store is free of
    side effects (a test can build one against a `tmp_path` without a directory appearing until
    something is actually stored).
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def key_for(self, user_id: UserId, export_id: AccountExportId) -> str:
        return f"exports/{user_id}/{export_id}.json"

    def put(self, storage_key: str, content: bytes, *,
            media_type: str = ACCOUNT_EXPORT_MEDIA_TYPE) -> None:
        path = self._resolve(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get(self, storage_key: str) -> StoredExport:
        path = self._resolve(storage_key)
        try:
            content = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError) as exc:
            raise ExportNotFound(storage_key) from exc
        return StoredExport(storage_key=storage_key, content=content,
                            media_type=ACCOUNT_EXPORT_MEDIA_TYPE)

    def delete(self, storage_key: str) -> bool:
        path = self._resolve(storage_key)
        try:
            path.unlink()
        except (FileNotFoundError, IsADirectoryError):
            return False
        return True

    def _resolve(self, storage_key: str) -> Path:
        """Turn a storage key into an absolute path that cannot escape the root.

        The guard is not paranoia about the keys this store's own `key_for` produces — those
        are id-derived and safe — but about the store never being the component that lets a `..`
        or an absolute key reach outside the export directory. A key that resolves outside
        `root` is a programming error, so it fails loudly rather than touching an arbitrary file.
        """
        candidate = (self._root / storage_key).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise ValueError(f"storage key {storage_key!r} escapes the export root")
        return candidate
