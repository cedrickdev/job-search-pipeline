"""Where a rendered document's bytes live, behind a storage-neutral protocol.

The domain describes a rendered PDF with a `DocumentArtifactRef` — a locator, not
the file — for the same reason `CandidateEvidence.source_document` is a label:
domain objects do not carry blobs (docs/ARCHITECTURE.md §9). This module is the
adapter that turns a locator into bytes and back.

`DocumentArtifactStore` is the port; `LocalDocumentArtifactStore` is the only
implementation Phase 10 ships — a directory on disk, one file per artifact, keyed
by the document and version ids. An object-store implementation (S3, GCS) would
satisfy the same protocol without the service above it changing, which is the
point of stating it as a protocol rather than reaching for the filesystem in the
service.

The one security concern here is path traversal: a `storage_key` is derived from
ids the service controls, but the store still refuses any key that could escape
its root, so a future caller that built a key from user input cannot write outside
the artifact directory (docs/ENGINEERING_STANDARDS.md §Security).
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from backend.app.domain.identifiers import CandidateDocumentId, DocumentVersionId


class ArtifactNotFound(Exception):
    """No artifact is stored under the requested key.

    A domain-level condition, not an HTTP concern: the API layer maps it to a 404.
    Raised by `get` so a caller distinguishes "never rendered / evicted" from an
    I/O error it should not swallow.
    """

    def __init__(self, storage_key: str) -> None:
        super().__init__(f"no stored artifact for key {storage_key!r}")
        self.storage_key = storage_key


@dataclass(frozen=True)
class StoredArtifact:
    """Bytes read back from the store, with the metadata a download needs.

    Returned by `get` so the API can stream the file with a correct
    `Content-Type` and length without the caller re-deriving them.
    """

    storage_key: str
    content: bytes
    media_type: str


@runtime_checkable
class DocumentArtifactStore(Protocol):
    """Persist and retrieve rendered document bytes.

    Deliberately small: a document service renders a PDF, `put`s it, and stores
    the returned key on the `DocumentVersion`'s artifact ref; a download handler
    `get`s it. Nothing here knows about versions' lifecycle — that is the domain's
    and the service's job — so the store stays a dumb, testable blob sink.
    """

    def key_for(self, document_id: CandidateDocumentId,
                version_id: DocumentVersionId, *, extension: str = "pdf") -> str:
        """The storage key for one version's artifact.

        Derived from the two ids so it is stable and recomputable: re-rendering a
        version overwrites its own artifact rather than leaking a new file.
        """
        ...

    def put(self, storage_key: str, content: bytes, *,
            media_type: str = "application/pdf") -> None:
        """Write `content` under `storage_key`, replacing any existing bytes."""
        ...

    def get(self, storage_key: str) -> StoredArtifact:
        """Read the bytes stored under `storage_key`, or raise `ArtifactNotFound`."""
        ...


class LocalDocumentArtifactStore:
    """A `DocumentArtifactStore` backed by a directory on the local filesystem.

    One file per artifact, named by its storage key, under `root`. The key is
    `documents/<document_id>/<version_id>.<ext>` — a shallow, id-derived layout, so
    the files are greppable in support and a version's artifact is found without a
    database lookup.

    `root` is created on first write, not construction, so instantiating the store
    is free of side effects (a test can build one against a `tmp_path` without a
    directory appearing until something is actually stored).
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def key_for(self, document_id: CandidateDocumentId,
                version_id: DocumentVersionId, *, extension: str = "pdf") -> str:
        return f"documents/{document_id}/{version_id}.{extension}"

    def put(self, storage_key: str, content: bytes, *,
            media_type: str = "application/pdf") -> None:
        path = self._resolve(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get(self, storage_key: str) -> StoredArtifact:
        path = self._resolve(storage_key)
        try:
            content = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError) as exc:
            raise ArtifactNotFound(storage_key) from exc
        return StoredArtifact(storage_key=storage_key, content=content,
                              media_type=_media_type_for(path))

    def _resolve(self, storage_key: str) -> Path:
        """Turn a storage key into an absolute path that cannot escape the root.

        The guard is not paranoia about the keys this store's own `key_for`
        produces — those are id-derived and safe — but about the store never being
        the component that lets a `..` or an absolute key reach outside the
        artifact directory. A key that resolves outside `root` is a programming
        error, so it fails loudly rather than reading or writing an arbitrary file.
        """
        candidate = (self._root / storage_key).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise ValueError(
                f"storage key {storage_key!r} escapes the artifact root")
        return candidate


def _media_type_for(path: Path) -> str:
    """The media type implied by a file's extension.

    Only the two Phase 10 produces are named; anything else is served as opaque
    bytes rather than guessed, since a wrong `Content-Type` on a download is worse
    than a generic one.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in (".htm", ".html"):
        return "text/html"
    return "application/octet-stream"
