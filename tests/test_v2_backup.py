"""The backup layer's orchestration and its one rule — a credential never leaves (§45-48).

These are the unit tests: no live PostgreSQL and no libpq binaries. A fake `PgToolRunner` and a fake
`RestoreInspector` stand in for the subprocess and the engine, so what is asserted here is the
*service's* behaviour — the filename it writes, the retention it prunes, the destructive-restore it
refuses (§46), the create→restore→read→drop order a verification runs — plus the credential-safety
the connection value and the tool runner enforce: the password lives in the child's environment and
never on a command line, and nothing the layer prints or raises can echo it.

The real end-to-end dump/restore against PostgreSQL is `tests/test_v2_persistence_backup.py` (§74).
"""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.app.backup import (
    BackupArtifact,
    BackupService,
    BackupToolError,
    PostgresConnectionParams,
    RestoreTargetError,
    SubprocessPgToolRunner,
    format_backup_report,
    format_prune_report,
    format_verification_report,
)
from backend.app.backup.service import RestoreVerification

SECRET = "s3cret-p4ssw0rd"  # noqa: S105 — a test plaintext the tests assert never leaks
SOURCE = PostgresConnectionParams(
    host="db.internal", port=5432, user="app", password=SECRET, database="appdb")
HEAD = "0020"
NOW = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


class FakeRunner:
    """Records every tool call and, on demand, fails one — no subprocess, no libpq."""

    def __init__(self, *, fail_restore: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._fail_restore = fail_restore

    def dump(self, source: PostgresConnectionParams, dump_path: Path) -> None:
        self.calls.append(("dump", source.database, str(dump_path)))
        dump_path.write_bytes(b"PGDMP-fake-bytes")  # give the artifact a real size

    def restore(self, target: PostgresConnectionParams, dump_path: Path) -> None:
        self.calls.append(("restore", target.database, str(dump_path)))
        if self._fail_restore:
            raise BackupToolError(
                "pg_restore", target.redacted, returncode=1, stderr="boom")

    def create_database(self, admin: PostgresConnectionParams, name: str) -> None:
        self.calls.append(("create_database", admin.database, name))

    def drop_database(self, admin: PostgresConnectionParams, name: str) -> None:
        self.calls.append(("drop_database", admin.database, name))


class FakeInspector:
    """Returns canned answers for the two reads a verification makes."""

    def __init__(self, *, revision: str | None, counts: dict[str, int]) -> None:
        self._revision = revision
        self._counts = counts
        self.read_from: list[str] = []

    def read_migration_revision(self, params: PostgresConnectionParams) -> str | None:
        self.read_from.append(params.database)
        return self._revision

    def count_records(self, params: PostgresConnectionParams, tables):
        return {table: self._counts.get(table, 0) for table in tables}


def _service(runner: FakeRunner, inspector: FakeInspector, root: Path,
             *, retention_days: int = 30) -> BackupService:
    return BackupService(
        runner=runner, inspector=inspector, artifact_root=root,
        retention_days=retention_days, expected_revision=HEAD)


# --- the connection value: the one credential lives in the environment, never on a line ------

def test_from_url_parses_the_discrete_libpq_parts() -> None:
    params = PostgresConnectionParams.from_url(
        "postgresql+psycopg://bob:pw@host.example:5432/appdb")
    assert (params.host, params.port, params.user, params.password, params.database) == (
        "host.example", 5432, "bob", "pw", "appdb")


def test_from_url_refuses_a_non_postgresql_url() -> None:
    with pytest.raises(ValueError, match="PostgreSQL-only"):
        PostgresConnectionParams.from_url("sqlite:///./local.db")


def test_from_url_refuses_a_url_with_no_database() -> None:
    with pytest.raises(ValueError, match="must name a database"):
        PostgresConnectionParams.from_url("postgresql+psycopg://bob:pw@host.example:5432/")


def test_libpq_env_carries_the_password_and_only_the_set_parts() -> None:
    env = SOURCE.libpq_env()
    assert env == {
        "PGDATABASE": "appdb", "PGHOST": "db.internal",
        "PGPORT": "5432", "PGUSER": "app", "PGPASSWORD": SECRET}
    # A socket/peer connection sets neither host nor password, and emits neither.
    bare = PostgresConnectionParams(
        host=None, port=None, user=None, password=None, database="local")
    assert bare.libpq_env() == {"PGDATABASE": "local"}


def test_redacted_names_the_target_but_never_the_credential() -> None:
    assert SOURCE.redacted == "db.internal:5432/appdb"
    assert SECRET not in SOURCE.redacted
    # A distinctive user proves neither the user nor the password reaches the redacted form.
    distinctive = PostgresConnectionParams(
        host="db.internal", port=5432, user="zealot_user", password=SECRET, database="appdb")
    assert "zealot_user" not in distinctive.redacted
    assert SECRET not in distinctive.redacted


def test_for_database_keeps_the_server_and_credentials_changes_only_the_name() -> None:
    scratch = SOURCE.for_database("appdb_verify_x")
    assert scratch.database == "appdb_verify_x"
    assert (scratch.host, scratch.port, scratch.user, scratch.password) == (
        SOURCE.host, SOURCE.port, SOURCE.user, SOURCE.password)


def test_targets_same_database_is_host_port_db_never_the_credential() -> None:
    same = PostgresConnectionParams(
        host="db.internal", port=5432, user="other",
        password="different", database="appdb")  # noqa: S106 — a test plaintext
    other_db = SOURCE.for_database("appdb_verify_x")
    assert SOURCE.targets_same_database(same) is True   # differing creds do not matter
    assert SOURCE.targets_same_database(other_db) is False


# --- the tool runner: explicit argv, the password only in the child's environment ------------

class _Capture:
    """A `subprocess.run` stand-in that records argv+env and returns a chosen result."""

    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.argv: list[str] | None = None
        self.env: dict[str, str] | None = None

    def __call__(self, argv, *, env, capture_output, text, timeout, check):
        self.argv = list(argv)
        self.env = dict(env)
        return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr=self.stderr)


def _runner() -> SubprocessPgToolRunner:
    return SubprocessPgToolRunner(
        pg_dump_path="pg_dump", pg_restore_path="pg_restore", psql_path="psql")


def _assert_no_credential_in_argv(argv: list[str]) -> None:
    joined = " ".join(argv)
    assert SECRET not in joined
    assert "PGPASSWORD" not in joined


def test_dump_puts_the_connection_in_the_env_and_only_the_path_on_argv(monkeypatch, tmp_path):
    capture = _Capture()
    monkeypatch.setattr(subprocess, "run", capture)
    dump_path = tmp_path / "out.dump"

    _runner().dump(SOURCE, dump_path)

    assert capture.argv == [
        "pg_dump", "--format=custom", "--no-owner", "--no-privileges",
        "--file", str(dump_path)]
    _assert_no_credential_in_argv(capture.argv)
    assert capture.env["PGPASSWORD"] == SECRET
    assert capture.env["PGDATABASE"] == "appdb"


def test_restore_names_the_target_db_on_argv_but_not_the_password(monkeypatch, tmp_path):
    capture = _Capture()
    monkeypatch.setattr(subprocess, "run", capture)
    dump_path = tmp_path / "in.dump"

    _runner().restore(SOURCE, dump_path)

    assert capture.argv == [
        "pg_restore", "--no-owner", "--no-privileges", "--dbname", "appdb", str(dump_path)]
    _assert_no_credential_in_argv(capture.argv)
    assert capture.env["PGPASSWORD"] == SECRET


def test_create_and_drop_run_against_the_maintenance_database(monkeypatch):
    capture = _Capture()
    monkeypatch.setattr(subprocess, "run", capture)

    _runner().create_database(SOURCE, "appdb_verify_abc")
    assert capture.argv[:2] == ["psql", "--no-psqlrc"]
    assert 'CREATE DATABASE "appdb_verify_abc"' in capture.argv[-1]
    assert capture.env["PGDATABASE"] == "postgres"  # not the source db

    _runner().drop_database(SOURCE, "appdb_verify_abc")
    assert 'DROP DATABASE IF EXISTS "appdb_verify_abc" WITH (FORCE)' in capture.argv[-1]
    assert capture.env["PGDATABASE"] == "postgres"


def test_create_database_refuses_an_unsafe_identifier(monkeypatch):
    # The guard runs before any subprocess; an injected name never reaches psql.
    monkeypatch.setattr(subprocess, "run", _Capture())
    with pytest.raises(ValueError, match="unsafe database identifier"):
        _runner().create_database(SOURCE, 'x"; DROP DATABASE appdb; --')


def test_a_nonzero_exit_becomes_a_typed_credential_free_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _Capture(returncode=1, stderr="fatal: nope"))
    with pytest.raises(BackupToolError) as caught:
        _runner().dump(SOURCE, Path("/tmp/x.dump"))
    message = str(caught.value)
    assert "pg_dump" in message and "db.internal:5432/appdb" in message and "fatal: nope" in message
    assert SECRET not in message


def test_a_missing_binary_and_a_timeout_are_typed(monkeypatch):
    def not_found(*a, **k):
        raise FileNotFoundError

    def times_out(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pg_dump", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", not_found)
    with pytest.raises(BackupToolError) as missing:
        _runner().dump(SOURCE, Path("/tmp/x.dump"))
    assert missing.value.returncode == 127

    monkeypatch.setattr(subprocess, "run", times_out)
    with pytest.raises(BackupToolError) as timed:
        _runner().dump(SOURCE, Path("/tmp/x.dump"))
    assert timed.value.returncode == 124


# --- the service: filenames, retention, the §46 guard, and the verification order ------------

def test_create_backup_writes_a_timestamped_dump_and_a_credential_free_receipt(tmp_path):
    runner, inspector = FakeRunner(), FakeInspector(revision=HEAD, counts={})
    service = _service(runner, inspector, tmp_path)

    artifact = service.create_backup(SOURCE, now=NOW)

    assert artifact.path.name == "jobsearch-20260301T093000Z.dump"
    assert artifact.size_bytes == len(b"PGDMP-fake-bytes")
    assert runner.calls == [("dump", "appdb", str(artifact.path))]


def test_create_backup_accepts_a_safe_label_and_refuses_an_unsafe_one(tmp_path):
    service = _service(FakeRunner(), FakeInspector(revision=HEAD, counts={}), tmp_path)
    labelled = service.create_backup(SOURCE, now=NOW, label="pre_migration")
    assert labelled.path.name == "jobsearch-20260301T093000Z-pre_migration.dump"
    with pytest.raises(ValueError, match="alphanumeric"):
        service.create_backup(SOURCE, now=NOW, label="../etc/passwd")


def test_list_backups_returns_managed_dumps_newest_first_and_ignores_strays(tmp_path):
    service = _service(FakeRunner(), FakeInspector(revision=HEAD, counts={}), tmp_path)
    older = service.create_backup(SOURCE, now=NOW - timedelta(days=2))
    newer = service.create_backup(SOURCE, now=NOW)
    (tmp_path / "README.md").write_text("not a backup")
    (tmp_path / ".gitkeep").write_text("")

    listed = [a.path.name for a in service.list_backups()]

    assert listed == [newer.path.name, older.path.name]


def test_prune_removes_only_dumps_older_than_retention(tmp_path):
    service = _service(
        FakeRunner(), FakeInspector(revision=HEAD, counts={}), tmp_path, retention_days=7)
    stale = service.create_backup(SOURCE, now=NOW - timedelta(days=10))
    fresh = service.create_backup(SOURCE, now=NOW - timedelta(days=1))
    (tmp_path / "keep.txt").write_text("unmanaged")

    removed = service.prune(now=NOW)

    assert [a.path.name for a in removed] == [stale.path.name]
    assert not stale.path.exists()
    assert fresh.path.exists()
    assert (tmp_path / "keep.txt").exists()  # a stray file is never a prune candidate


def test_restore_refuses_a_protected_database(tmp_path):
    runner = FakeRunner()
    service = _service(runner, FakeInspector(revision=HEAD, counts={}), tmp_path)
    artifact = BackupArtifact(path=tmp_path / "d.dump", created_at=NOW, size_bytes=1)

    with pytest.raises(RestoreTargetError, match="db.internal:5432/appdb"):
        service.restore(artifact, SOURCE, protected=(SOURCE,))
    assert runner.calls == []  # the guard runs before the tool


def test_restore_into_a_separate_database_runs_the_tool(tmp_path):
    runner = FakeRunner()
    service = _service(runner, FakeInspector(revision=HEAD, counts={}), tmp_path)
    artifact = BackupArtifact(path=tmp_path / "d.dump", created_at=NOW, size_bytes=1)

    service.restore(artifact, SOURCE.for_database("scratch"), protected=(SOURCE,))

    assert runner.calls == [("restore", "scratch", str(artifact.path))]


def test_verify_creates_restores_reads_then_drops_an_isolated_scratch(tmp_path):
    runner = FakeRunner()
    inspector = FakeInspector(revision=HEAD, counts={"users": 3})
    service = _service(runner, inspector, tmp_path)
    artifact = service.create_backup(SOURCE, now=NOW)
    runner.calls.clear()

    result = service.verify(artifact, source=SOURCE)

    kinds = [call[0] for call in runner.calls]
    assert kinds == ["create_database", "restore", "drop_database"]
    scratch_name = runner.calls[0][2]
    assert scratch_name.startswith("appdb_verify_") and scratch_name != "appdb"
    # The restore targeted the scratch, never the source, and read the scratch back.
    assert runner.calls[1][1] == scratch_name
    assert inspector.read_from == [scratch_name]
    assert result.verified is True
    assert result.record_counts == {"users": 3}


def test_verify_always_drops_the_scratch_even_when_the_restore_fails(tmp_path):
    runner = FakeRunner(fail_restore=True)
    service = _service(runner, FakeInspector(revision=HEAD, counts={}), tmp_path)
    artifact = service.create_backup(SOURCE, now=NOW)
    runner.calls.clear()

    with pytest.raises(BackupToolError):
        service.verify(artifact, source=SOURCE)

    assert [call[0] for call in runner.calls] == ["create_database", "restore", "drop_database"]


def test_verify_is_not_verified_when_the_revision_is_behind_head(tmp_path):
    runner = FakeRunner()
    service = _service(runner, FakeInspector(revision="0019", counts={"users": 1}), tmp_path)
    artifact = service.create_backup(SOURCE, now=NOW)

    result = service.verify(artifact, source=SOURCE)

    assert result.verified is False
    assert result.revision == "0019" and result.expected_revision == HEAD


# --- the reports: a verdict an operator can act on, never a credential -----------------------

def test_reports_are_credential_free() -> None:
    artifact = BackupArtifact(
        path=Path("/var/backups/jobsearch-20260301T093000Z.dump"),
        created_at=NOW, size_bytes=42)
    verification = RestoreVerification(
        revision=HEAD, expected_revision=HEAD, record_counts={"users": 5})

    backup_report = format_backup_report(artifact)
    prune_report = format_prune_report((artifact,))
    verify_report = format_verification_report(verification)

    assert "jobsearch-20260301T093000Z.dump" in backup_report
    assert "1 dump(s)" in prune_report
    assert "VERIFIED" in verify_report and "users: 5" in verify_report
    for report in (backup_report, prune_report, verify_report):
        assert SECRET not in report
        assert "PGPASSWORD" not in report
