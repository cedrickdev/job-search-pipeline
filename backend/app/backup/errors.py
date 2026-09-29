"""The typed failures the backup layer raises — none of which ever carries a credential.

Kept in one leaf module so the service, the tool runner and the CLI share exactly one vocabulary,
and so the rule that matters — an error message names the *target* by its redacted `host:port/db`
and the tool's stderr, never the DSN or the password — is stated once and enforced everywhere.
"""
from __future__ import annotations


class BackupError(Exception):
    """Base for every backup/recovery failure. Its message is always credential-free."""


class BackupToolError(BackupError):
    """A libpq tool (`pg_dump`/`pg_restore`/`psql`) failed — the tool, target and stderr, no secret.

    The password never reaches a tool's command line (it travels in the child's environment), so the
    stderr this carries cannot echo it back; the target is the redacted `host:port/db`, never the
    DSN.
    """

    def __init__(self, tool: str, target: str, *, returncode: int, stderr: str) -> None:
        self.tool = tool
        self.target = target
        self.returncode = returncode
        self.stderr = stderr.strip()
        detail = f" — {self.stderr}" if self.stderr else ""
        super().__init__(
            f"{tool} failed against {target} (exit {returncode}){detail}")


class RestoreTargetError(BackupError):
    """A restore was aimed at a database it must not overwrite — the §46 destructive-restore guard.

    Raised before any tool runs, so the protected database is never touched. The message names both
    databases by their redacted form only.
    """
