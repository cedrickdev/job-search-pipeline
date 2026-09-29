"""`python -m backend.app.cli.run_backup` — take, list, prune and verify PostgreSQL dumps (§45-48).

The operational entrypoint for backup and recovery. Four subcommands, one policy:

- **create** writes a timestamped custom-format dump of the configured database (§45);
- **list** shows the managed dumps under the artifact directory, newest first;
- **prune** deletes dumps older than the retention window (§48);
- **verify** restores the latest (or a named) dump into an *isolated* scratch database, checks the
  migration revision and representative record counts, then drops the scratch database (§46).

It never prints a credential: the database password is read from `DatabaseSettings`, handed to the
libpq child through its environment, and never placed on a command line or in a report. Every
target is named by its redacted `host:port/db`.

Exit codes mirror the other CLIs: 0 clean, 3 finished with a failure — a tool failed, or a `verify`
came back NOT VERIFIED (the restored schema is not at the expected revision). A non-zero `verify`
is the signal a monitored recovery drill watches for.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from alembic.config import Config
from alembic.script import ScriptDirectory

from backend.app.backup import (
    BackupArtifact,
    BackupError,
    BackupService,
    PostgresConnectionParams,
    SqlAlchemyRestoreInspector,
    SubprocessPgToolRunner,
    format_backup_report,
    format_prune_report,
    format_verification_report,
)
from backend.app.core.settings import BackupSettings, DatabaseSettings

EXIT_OK: Final[int] = 0
EXIT_INCOMPLETE: Final[int] = 3

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
ALEMBIC_INI: Final[Path] = REPO_ROOT / "alembic.ini"


def _expected_revision() -> str:
    """The Alembic head a verified restore must land on — resolved from the migration scripts.

    Read here so the service stays free of Alembic (it is handed the revision, not the config).
    """
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    head = script.get_current_head()
    if head is None:
        raise BackupError("no Alembic head revision found; the migration scripts are missing")
    return head


def _build_service() -> tuple[BackupService, PostgresConnectionParams]:
    """Assemble the service from the environment, and the connection its operations target.

    The one credential (the database password) enters here, inside `PostgresConnectionParams`, and
    goes no further than the libpq child's environment. `artifact_root` is resolved against the
    repository root so a relative default (`var/backups`) lands in the same place regardless of the
    operator's working directory.
    """
    database = DatabaseSettings.from_env()
    backup = BackupSettings.from_env()
    source = PostgresConnectionParams.from_url(database.url)
    runner = SubprocessPgToolRunner(
        pg_dump_path=backup.pg_dump_path,
        pg_restore_path=backup.pg_restore_path,
        psql_path=backup.psql_path)
    root = Path(backup.artifact_root)
    if not root.is_absolute():
        root = REPO_ROOT / root
    service = BackupService(
        runner=runner,
        inspector=SqlAlchemyRestoreInspector(),
        artifact_root=root,
        retention_days=backup.retention_days,
        expected_revision=_expected_revision())
    return service, source


def _latest_or_named(service: BackupService, name: str | None) -> BackupArtifact:
    """The dump a `verify` targets: the one named, else the newest managed dump."""
    backups = service.list_backups()
    if name is not None:
        for artifact in backups:
            if artifact.path.name == name:
                return artifact
        raise BackupError(f"no managed backup named {name!r} under the artifact directory")
    if not backups:
        raise BackupError("no backups to verify; run `create` first")
    return backups[0]


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.cli.run_backup",
        description="Take, list, prune and verify PostgreSQL backups. Credentials never appear "
                    "on a command line or in output; every target is the redacted host:port/db.")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="write a timestamped dump of the configured database")
    create.add_argument(
        "--label", default=None,
        help="an alphanumeric/underscore suffix for the dump filename (e.g. pre-migration)")

    sub.add_parser("list", help="list managed dumps under the artifact directory, newest first")
    sub.add_parser("prune", help="delete managed dumps older than the retention window")

    verify = sub.add_parser(
        "verify", help="restore a dump into an isolated scratch database and check it (§46)")
    verify.add_argument(
        "--name", default=None,
        help="the dump filename to verify; defaults to the newest managed dump")
    return parser.parse_args(argv)


def _do_create(service: BackupService, source: PostgresConnectionParams,
               label: str | None) -> int:
    artifact = service.create_backup(source, now=datetime.now(UTC), label=label)
    print(format_backup_report(artifact))
    return EXIT_OK


def _do_list(service: BackupService) -> int:
    backups = service.list_backups()
    if not backups:
        print("no managed backups found")
        return EXIT_OK
    print(f"{len(backups)} managed backup(s), newest first:")
    for artifact in backups:
        print(f"  {artifact.path.name}  ({artifact.size_bytes} bytes)")
    return EXIT_OK


def _do_prune(service: BackupService) -> int:
    removed = service.prune(now=datetime.now(UTC))
    print(format_prune_report(removed))
    return EXIT_OK


def _do_verify(service: BackupService, source: PostgresConnectionParams,
               name: str | None) -> int:
    artifact = _latest_or_named(service, name)
    verification = service.verify(artifact, source=source)
    print(format_verification_report(verification))
    return EXIT_OK if verification.verified else EXIT_INCOMPLETE


def _run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        service, source = _build_service()
        if args.command == "create":
            return _do_create(service, source, args.label)
        if args.command == "list":
            return _do_list(service)
        if args.command == "prune":
            return _do_prune(service)
        if args.command == "verify":
            return _do_verify(service, source, args.name)
    except Exception as exc:  # a CLI reports a failure, it does not traceback
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE
    return EXIT_INCOMPLETE  # pragma: no cover - argparse rejects an unknown command first


if __name__ == "__main__":  # pragma: no cover - exercised through _run()
    raise SystemExit(_run())
