"""Import V1's SQLite `jobs` table into the V2 schema, repeatably.

The V1 tracker stays the source of truth until V2 replaces it, so this module
reads it and never writes to it: the connection is opened `mode=ro`, and the only
statement issued is a `SELECT`. A migration tool that can modify its own input is
one bug away from destroying the data it was meant to rescue.

Three properties are what make it safe to run more than once.

*Deterministic identity.* Every row's V2 id is `uuid5(V1 namespace, "v1:jobs:<id>")`
(`opportunity_id_for_v1_job`), so the second run addresses the same rows as the
first. Nothing depends on insertion order and nothing is duplicated by a retry.

*Per-row isolation.* Each row is written inside a savepoint. One posting that
violates a constraint is reported and rolled back on its own; in PostgreSQL any
error otherwise poisons the whole transaction, which would turn a single bad row
into a failed import of thousands of good ones.

*A report instead of a log.* Every row that does not land counts against a code
(`V1_DISCOVERED_DATE_INVALID`, `PERSISTENCE_FAILED`, …) with the offending value
in a message next to it. `docs/PERSISTENCE.md §Import` is written against these
codes, and the CLI's exit status is derived from them, so a skipped row cannot
pass for a clean run.

The module holds no session and builds no query: it writes through the
`OpportunityRepository` contract and isolates rows through an injected savepoint
factory. It does name two of SQLAlchemy's exception classes, because deciding that
a failure belongs to one row rather than to the run requires knowing what a
constraint violation is — see `ROW_LOCAL_ERRORS`.
"""
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, contextmanager
from datetime import UTC, tzinfo
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.exc import DataError, IntegrityError

from backend.app.compat.v1_jobs import V1MappingError, opportunity_from_v1_job
from backend.app.repositories.contracts import OpportunityRepository

# The V1 table this reads. Named once: V1 owns it, and the Phase 2 order forbids
# renaming or dropping it.
V1_JOBS_TABLE: Final[str] = "jobs"

# A savepoint per row. `AsyncSession.begin_nested` satisfies this exactly.
SavepointFactory = Callable[[], AbstractAsyncContextManager[Any]]

# The failures that belong to the row being written, and to nothing else: a
# violated constraint (`IntegrityError`) or a value PostgreSQL refuses
# (`DataError`). They are reported and skipped.
#
# Everything else propagates, which is the important half of this decision. A
# dropped connection surfaces as an `OperationalError` on every subsequent row,
# and a run that swallowed it would report "3000 rows failed" for what is one
# unreachable database, then exit as though it had merely lost some rows.
ROW_LOCAL_ERRORS: Final[tuple[type[Exception], ...]] = (DataError, IntegrityError)

class ImportOutcome(StrEnum):
    """What happened to one V1 row."""

    IMPORTED = "imported"
    UPDATED = "updated"
    SKIPPED = "skipped"
    FAILED = "failed"


class ImportIssueCode(StrEnum):
    """Why a row did not land, for the reasons `v1_jobs` cannot express.

    Mapping failures already carry a `V1MappingErrorCode`; these two cover the
    stages after it. The split between them is the one a reader has to act on:
    `DOMAIN_VALIDATION_FAILED` is a V1 value the domain refuses, so the fix is in
    the mapper, while `PERSISTENCE_FAILED` is a constraint the schema enforces —
    two rows claiming one `dedup_hash`, say — so the fix is in the data.
    """

    DOMAIN_VALIDATION_FAILED = "DOMAIN_VALIDATION_FAILED"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"


class OnError(StrEnum):
    """What an unusable row does to the run.

    `SKIP` reports it and continues, which is the only way to migrate a database
    scraped from a dozen job boards; `FAIL` stops on the first one, which is what
    a release rehearsal wants. Neither hides anything: both record the row in the
    report, and the CLI's exit status distinguishes "clean" from "completed with
    skipped rows".
    """

    SKIP = "skip"
    FAIL = "fail"


class V1ImportError(RuntimeError):
    """The V1 database cannot be read at all — missing file, or no `jobs` table.

    Distinct from a bad row: there is nothing to report per row and no policy to
    apply, so this propagates and the CLI exits non-zero.
    """


class ImportIssue(BaseModel):
    """One row that did not land, and enough to find it in V1.

    `v1_id` is optional because the id itself is what can be missing (a row whose
    `id` is NULL in a hand-edited export). `code` is a plain `str` so a
    `V1MappingErrorCode` and an `ImportIssueCode` can sit in the same column of
    the same report without a union that every reader would have to widen.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    v1_id: int | None
    outcome: ImportOutcome
    code: str
    message: str


class ImportReport(BaseModel):
    """The result of one import: counts, and every row that did not land.

    Counts are derived from `issues` and the two success counters rather than
    tracked independently, so the report cannot claim 900 imported rows and list
    901 issues.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_read: int = 0
    imported: int = 0
    updated: int = 0
    issues: tuple[ImportIssue, ...] = ()

    @property
    def skipped(self) -> int:
        """Rows V1 could not offer in a usable form."""
        return sum(1 for issue in self.issues
                   if issue.outcome is ImportOutcome.SKIPPED)

    @property
    def failed(self) -> int:
        """Rows that mapped cleanly and that the database refused."""
        return sum(1 for issue in self.issues if issue.outcome is ImportOutcome.FAILED)

    @property
    def is_clean(self) -> bool:
        """True when every row read was written. The CLI's exit status.

        A run over an empty V1 database is clean: nothing was lost.
        """
        return not self.issues


class V1ImportAborted(V1ImportError):
    """`--on-error fail` met a row it could not import, and stopped.

    Carries the partial report so the caller can still say which row ended the
    run and how many had already been read. Raising rather than returning is what
    makes `fail` mean *nothing partial*: the exception travels through
    `session_scope`, which rolls the transaction back on any `BaseException`.
    """

    def __init__(self, report: ImportReport) -> None:
        issue = report.issues[-1]
        super().__init__(f"V1 jobs row {issue.v1_id} could not be imported "
                         f"[{issue.code}]: {issue.message}")
        self.report = report


@contextmanager
def open_v1_database(path: Path) -> Iterator[sqlite3.Connection]:
    """Open V1's SQLite file read-only.

    `mode=ro` is enforced by SQLite, not by this code being careful: an `INSERT`
    on this connection raises instead of writing, so no future edit to this module
    can turn the migration into a mutation of its own source. It also leaves no
    `-wal`/`-shm` files next to a database V1 may have open at the same time.

    `as_uri()` percent-encodes the path, so a directory containing `?` or `#`
    cannot smuggle a second URI parameter into the connection string.
    """
    if not path.is_file():
        raise V1ImportError(f"V1 SQLite database not found: {path}")
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise V1ImportError(f"cannot open V1 SQLite database {path}: {exc}") from exc
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def read_v1_jobs(connection: sqlite3.Connection, *,
                 limit: int | None = None) -> Iterator[Mapping[str, Any]]:
    """Stream V1 `jobs` rows as plain dictionaries, oldest id first.

    A generator, not a list: V1's table is small today, but an importer that has
    to hold the whole table in memory before writing the first row is one that
    stops working exactly when it matters. `ORDER BY id` makes a `--limit` run
    read the same rows every time, so a rehearsal on 50 rows is repeatable.
    """
    if connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (V1_JOBS_TABLE,)).fetchone() is None:
        raise V1ImportError(f"V1 SQLite database has no {V1_JOBS_TABLE!r} table")

    # The table name is a module constant, never a caller's string, so the f-string
    # cannot carry an injected fragment; `limit` is bound as a parameter.
    statement = f"SELECT * FROM {V1_JOBS_TABLE} ORDER BY id"  # noqa: S608
    cursor = (connection.execute(statement) if limit is None
              else connection.execute(f"{statement} LIMIT ?", (limit,)))
    for row in cursor:
        yield dict(row)


# Long enough to identify the offending value, short enough that a report over a
# thousand bad rows stays readable: a SQLAlchemy `IntegrityError` embeds the whole
# statement and every bound parameter.
_MAX_MESSAGE_LENGTH: Final[int] = 400


def _message(exc: Exception) -> str:
    """An exception's message as one bounded line, fit for a report column."""
    text = " ".join(str(exc).split())
    return text if len(text) <= _MAX_MESSAGE_LENGTH \
        else f"{text[:_MAX_MESSAGE_LENGTH]}..."


def _detail(exc: Exception) -> str:
    """The same, prefixed with the exception's class.

    Used for the persistence failures, where the class is what a reader acts on:
    `IntegrityError` means two rows disagree about something unique, `DataError`
    means one value is out of range for its column. The classified failures do
    not need it — their code already says what kind of failure it was.
    """
    return f"{type(exc).__name__}: {_message(exc)}"


def _row_id(row: Mapping[str, Any]) -> int | None:
    """The V1 row id for the report, or `None` when that is what is wrong.

    Deliberately forgiving: this value exists so a human can run
    `SELECT * FROM jobs WHERE id = ?` on the row that failed, and an id that
    cannot be read must not raise on its way into an error report.
    """
    try:
        return int(str(row["id"]).strip())
    except (KeyError, TypeError, ValueError):
        return None


async def import_v1_jobs(
        rows: Iterable[Mapping[str, Any]], *,
        repository: OpportunityRepository,
        savepoint: SavepointFactory,
        on_error: OnError = OnError.SKIP,
        default_timezone: tzinfo = UTC) -> ImportReport:
    """Write every V1 row through `repository`, one savepoint at a time.

    Takes rows rather than a path so the skip/fail policy is testable against a
    list of dictionaries — including rows no SQLite file would produce — while
    `import_v1_sqlite` remains the one call the CLI makes.

    `savepoint` is injected for the same reason: what this needs from persistence
    is "isolate this row", and `AsyncSession.begin_nested` satisfies that without
    this module ever holding a session or building a query. Nothing here commits;
    the caller's `session_scope` owns the transaction, which is what makes
    `--dry-run` a parameter of the boundary and not a branch in this loop.

    `imported` counts rows that were absent and `updated` counts rows that were
    already there — the second run of the same import reports every row as
    updated, and that is the observable form of idempotency.
    """
    rows_read = 0
    imported = 0
    updated = 0
    issues: list[ImportIssue] = []

    def snapshot() -> ImportReport:
        return ImportReport(rows_read=rows_read, imported=imported, updated=updated,
                            issues=tuple(issues))

    for row in rows:
        rows_read += 1
        v1_id = _row_id(row)

        try:
            opportunity = opportunity_from_v1_job(row,
                                                  default_timezone=default_timezone)
        except V1MappingError as exc:
            # Ahead of `ValidationError`, which would otherwise shadow it: both are
            # `ValueError`s, and only this one arrives already classified.
            issues.append(ImportIssue(v1_id=v1_id, outcome=ImportOutcome.SKIPPED,
                                      code=exc.code.value, message=_message(exc)))
            if on_error is OnError.FAIL:
                raise V1ImportAborted(snapshot()) from exc
            continue
        except ValidationError as exc:
            # A V1 value the mapper accepted and the domain refuses. Reported apart
            # from a mapping failure because the fix is in a different file.
            issues.append(ImportIssue(
                v1_id=v1_id, outcome=ImportOutcome.SKIPPED,
                code=ImportIssueCode.DOMAIN_VALIDATION_FAILED.value,
                message=_message(exc)))
            if on_error is OnError.FAIL:
                raise V1ImportAborted(snapshot()) from exc
            continue

        try:
            async with savepoint():
                # Read before write, so the report can distinguish a first import
                # from a re-run. Costs one indexed primary-key lookup per row.
                existing = await repository.get(opportunity.id)
                await repository.upsert(opportunity)
        except ROW_LOCAL_ERRORS as exc:
            # Narrow on purpose: see `ROW_LOCAL_ERRORS`. `_detail` keeps the
            # exception's class name, so a report never says only "it failed" —
            # a duplicate `dedup_hash` and an over-long value read differently.
            issues.append(ImportIssue(
                v1_id=v1_id, outcome=ImportOutcome.FAILED,
                code=ImportIssueCode.PERSISTENCE_FAILED.value, message=_detail(exc)))
            if on_error is OnError.FAIL:
                raise V1ImportAborted(snapshot()) from exc
            continue

        if existing is None:
            imported += 1
        else:
            updated += 1

    return snapshot()


async def import_v1_sqlite(
        sqlite_path: Path, *,
        repository: OpportunityRepository,
        savepoint: SavepointFactory,
        on_error: OnError = OnError.SKIP,
        limit: int | None = None,
        default_timezone: tzinfo = UTC) -> ImportReport:
    """Import V1's `jobs` table from the SQLite file at `sqlite_path`.

    The connection is opened read-only and closed before this returns; the rows
    are streamed, so the file is read exactly once.
    """
    with open_v1_database(sqlite_path) as connection:
        return await import_v1_jobs(read_v1_jobs(connection, limit=limit),
                                    repository=repository, savepoint=savepoint,
                                    on_error=on_error,
                                    default_timezone=default_timezone)
