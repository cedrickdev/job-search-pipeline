"""Taking, pruning and — the part §46 insists on — *verifying* PostgreSQL backups.

This service owns the orchestration and the safety guards; the tool runner
(`backend/app/backup/tools.py`) owns the libpq subprocess work, and the inspector
(`backend/app/backup/inspector.py`) owns reading a restored database back. The division keeps this
layer testable with fakes and free of both `subprocess` and a live engine.

Three operations, and one rule under all of them — **nothing here ever emits a credential**. A
`BackupArtifact` is a path, a size and an instant; a `RestoreVerification` is a revision and a set
of counts; every failure names its target by the redacted `host:port/db`.

- **create** writes a timestamped custom-format dump under the configured directory (§45).
- **prune** deletes recognised dumps older than the retention window (§48), and only ever files it
  can identify as its own — a stray file in the directory is never touched.
- **verify** restores a dump into an *isolated* scratch database, reads back the migration revision
  and the representative record counts, and drops the scratch database again (§46). The
  destructive-restore guard refuses outright to restore over the database a dump came from, so a
  verification can never overwrite live data.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from backend.app.backup.connection import PostgresConnectionParams
from backend.app.backup.errors import RestoreTargetError
from backend.app.backup.inspector import RestoreInspector
from backend.app.backup.tools import PgToolRunner

# The dump filename carries the backup instant, so pruning reads the age from the name rather than
# trusting a filesystem mtime that a copy or a restore-from-object-store would have reset.
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_FILENAME_PREFIX = "jobsearch-"
_FILENAME_SUFFIX = ".dump"
_FILENAME_RE = re.compile(
    r"^jobsearch-(?P<stamp>\d{8}T\d{6}Z)(?:-(?P<label>[A-Za-z0-9_]+))?\.dump$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_]+$")

# The tables a restore is checked against (§46 "verify representative records"). `users` is the
# spine every owned row hangs off, so a restore that brought back an empty `users` restored nothing
# worth keeping. A deployment can widen this per call; this is the safe default.
REPRESENTATIVE_TABLES: tuple[str, ...] = ("users",)


@dataclass(frozen=True)
class BackupArtifact:
    """One dump on disk — a path, its size and when it was taken. Never a credential."""

    path: Path
    created_at: datetime
    size_bytes: int


@dataclass(frozen=True)
class RestoreVerification:
    """The result of restoring a dump into an isolated database and reading it back (§46).

    `verified` is true only when the restored schema is at the expected Alembic head — a dump that
    predates a migration restores onto the wrong schema and is not a usable backup. The record
    counts are reported for the caller (and the smoke test) to assert representative data survived.
    """

    revision: str | None
    expected_revision: str
    record_counts: Mapping[str, int]

    @property
    def verified(self) -> bool:
        return self.revision == self.expected_revision


class BackupService:
    """Take, prune and verify PostgreSQL dumps under one directory and one retention policy.

    Stateless between calls: it holds the tool runner, the restore inspector, the artifact directory
    and the retention window. `expected_revision` is the Alembic head a verified restore must land
    on — injected rather than imported so this layer stays free of Alembic (the CLI resolves the
    head from the migration scripts and passes it in).
    """

    def __init__(self, *, runner: PgToolRunner, inspector: RestoreInspector,
                 artifact_root: Path, retention_days: int, expected_revision: str) -> None:
        self._runner = runner
        self._inspector = inspector
        self._root = artifact_root
        self._retention = timedelta(days=retention_days)
        self._expected_revision = expected_revision

    def _artifact_filename(self, now: datetime, label: str | None) -> str:
        """`jobsearch-<UTC timestamp>[-<label>].dump` — the name prune later reads the age from."""
        stamp = now.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)
        if label is None:
            return f"{_FILENAME_PREFIX}{stamp}{_FILENAME_SUFFIX}"
        if not _SAFE_LABEL.match(label):
            raise ValueError(f"a backup label must be alphanumeric/underscore; got {label!r}")
        return f"{_FILENAME_PREFIX}{stamp}-{label}{_FILENAME_SUFFIX}"

    def create_backup(self, source: PostgresConnectionParams, *, now: datetime,
                      label: str | None = None) -> BackupArtifact:
        """Write a custom-format dump of `source` and return its credential-free receipt (§45)."""
        self._root.mkdir(parents=True, exist_ok=True)
        dump_path = self._root / self._artifact_filename(now, label)
        self._runner.dump(source, dump_path)
        return BackupArtifact(
            path=dump_path, created_at=now, size_bytes=dump_path.stat().st_size)

    def list_backups(self) -> tuple[BackupArtifact, ...]:
        """Every recognised dump in the directory, newest first — files we did not write, ignored.

        Only names matching the `jobsearch-<timestamp>[-label].dump` pattern are returned, so a
        README, a `.gitkeep` or an operator's ad-hoc copy is never mistaken for a managed backup
        (and, below, is never pruned).
        """
        if not self._root.is_dir():
            return ()
        artifacts: list[BackupArtifact] = []
        for entry in self._root.iterdir():
            created = self._created_at_of(entry)
            if created is not None:
                artifacts.append(BackupArtifact(
                    path=entry, created_at=created, size_bytes=entry.stat().st_size))
        return tuple(sorted(artifacts, key=lambda a: a.created_at, reverse=True))

    @staticmethod
    def _created_at_of(path: Path) -> datetime | None:
        """The backup instant encoded in a managed dump's name, or `None` if it is not ours."""
        match = _FILENAME_RE.match(path.name)
        if match is None or not path.is_file():
            return None
        return datetime.strptime(match.group("stamp"), _TIMESTAMP_FORMAT).replace(tzinfo=UTC)

    def prune(self, *, now: datetime) -> tuple[BackupArtifact, ...]:
        """Delete recognised dumps older than the retention window; return what was removed (§48).

        Age is read from the filename's embedded instant, not a filesystem mtime, so a dump copied
        or fetched back from cold storage still prunes on the day it was *taken*. Unrecognised files
        are never candidates, so pruning can only ever remove backups this service wrote.
        """
        cutoff = now.astimezone(UTC) - self._retention
        removed: list[BackupArtifact] = []
        for artifact in self.list_backups():
            if artifact.created_at < cutoff:
                artifact.path.unlink()
                removed.append(artifact)
        return tuple(removed)

    def restore(self, artifact: BackupArtifact, target: PostgresConnectionParams, *,
                protected: Sequence[PostgresConnectionParams] = ()) -> None:
        """Restore a dump into `target`, refusing any database in `protected` (the §46 guard).

        The guard runs *before* the tool does, so a protected (development/production) database is
        never touched — a fat-fingered restore aimed at live data raises `RestoreTargetError` and
        stops. `target` is expected to be an already-created, empty database.
        """
        for guarded in protected:
            if target.targets_same_database(guarded):
                raise RestoreTargetError(
                    f"refusing to restore over the protected database {target.redacted}; "
                    "restore into a separate database")
        self._runner.restore(target, artifact.path)

    def verify(self, artifact: BackupArtifact, *, source: PostgresConnectionParams,
               representative_tables: Sequence[str] = REPRESENTATIVE_TABLES,
               ) -> RestoreVerification:
        """Restore a dump into a throwaway database, read it back, then drop it (§46).

        The scratch database is created beside the source (same server, unique name), so a
        verification never needs a second cluster and never overwrites the source — which is
        asserted, not assumed: the scratch target can never equal the source, and the source is
        passed as `protected` to the very restore that runs. The scratch database is always dropped,
        even if the restore or a read fails, so a verification leaves nothing behind.
        """
        scratch_name = f"{source.database}_verify_{uuid4().hex[:12]}"
        scratch = source.for_database(scratch_name)
        self._runner.create_database(source, scratch_name)
        try:
            self.restore(artifact, scratch, protected=(source,))
            revision = self._inspector.read_migration_revision(scratch)
            counts = self._inspector.count_records(scratch, tuple(representative_tables))
            return RestoreVerification(
                revision=revision, expected_revision=self._expected_revision,
                record_counts=counts)
        finally:
            self._runner.drop_database(source, scratch_name)


def format_backup_report(artifact: BackupArtifact) -> str:
    """A data-free receipt for a created dump — path, instant and size, never a credential."""
    stamp = artifact.created_at.astimezone(UTC).isoformat()
    return (
        f"backup written\n"
        f"  path {artifact.path}\n"
        f"  at   {stamp}\n"
        f"  size {artifact.size_bytes} bytes")


def format_prune_report(removed: Sequence[BackupArtifact]) -> str:
    """A data-free tally of a prune — how many dumps were removed, and their names."""
    lines = [f"prune removed {len(removed)} dump(s)"]
    for artifact in removed:
        lines.append(f"  {artifact.path.name}")
    return "\n".join(lines)


def format_verification_report(verification: RestoreVerification) -> str:
    """The §46 verdict, credential-free: whether the restore verified, its revision and counts.

    Names no host and no database — a verification is reported by *what it proved* (the schema
    landed at the expected revision, the representative rows survived), never by where it ran.
    """
    verdict = "VERIFIED" if verification.verified else "NOT VERIFIED"
    lines = [
        f"restore verification: {verdict}",
        f"  revision {verification.revision or '<none>'} "
        f"(expected {verification.expected_revision})",
    ]
    for table, count in verification.record_counts.items():
        lines.append(f"  {table}: {count} row(s)")
    return "\n".join(lines)



