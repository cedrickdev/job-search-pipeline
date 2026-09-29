"""Backup & recovery — taking, pruning and *verifying* PostgreSQL dumps (Phase 16 §45-48, §74).

The application-layer package that turns the libpq tooling (`pg_dump`/`pg_restore`/`psql`) and one
read-only inspector into three operator-scope operations — create, prune, verify — under one rule:
**nothing here ever emits a credential**. The database password travels only in a child process's
environment, never on a command line; every path, report and error names its target by the redacted
`host:port/db`. See `docs/BACKUP_RECOVERY.md` for the operator-facing description.

The subprocess/libpq specifics live in `tools.py`, the restored-database reads in `inspector.py`,
and the orchestration and safety guards — including the §46 refusal to restore over the database a
dump came from — in `service.py`, which is unit-testable with fakes for the other two.
"""
from backend.app.backup.connection import PostgresConnectionParams
from backend.app.backup.errors import BackupError, BackupToolError, RestoreTargetError
from backend.app.backup.inspector import RestoreInspector, SqlAlchemyRestoreInspector
from backend.app.backup.service import (
    REPRESENTATIVE_TABLES,
    BackupArtifact,
    BackupService,
    RestoreVerification,
    format_backup_report,
    format_prune_report,
    format_verification_report,
)
from backend.app.backup.tools import PgToolRunner, SubprocessPgToolRunner

__all__ = [
    "REPRESENTATIVE_TABLES",
    "BackupArtifact",
    "BackupError",
    "BackupService",
    "BackupToolError",
    "PgToolRunner",
    "PostgresConnectionParams",
    "RestoreInspector",
    "RestoreTargetError",
    "RestoreVerification",
    "SqlAlchemyRestoreInspector",
    "SubprocessPgToolRunner",
    "format_backup_report",
    "format_prune_report",
    "format_verification_report",
]
