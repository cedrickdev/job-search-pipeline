"""`python -m backend.app.cli.import_v1` — copy V1's tracker into the V2 schema.

A thin shell over `backend.app.compat.v1_import`: this module owns the arguments,
the printed report and the exit status, and nothing else. The import logic is
there so it can be tested without a process, and the wiring is here so the
importer never has to know how the database URL was resolved.

Exit status, because this is meant to be run by `docker compose run` and by CI:

- `0` — every row read was written;
- `1` — nothing was imported: the V1 file is missing or has no `jobs` table, the
  DSN is invalid, or PostgreSQL is unreachable;
- `3` — the run completed (or stopped, under `--on-error fail`) with rows that did
  not land. A skipped row can therefore never pass for a clean migration, which
  is the point of the exercise.

Nothing printed here can contain a password: the DSN goes through
`redacted_url`, and any database error message is passed through
`redact_database_url` in case it echoed a DSN of its own.
"""
import argparse
import asyncio
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from backend.app.compat.v1_import import (
    ImportOutcome,
    ImportReport,
    OnError,
    V1ImportAborted,
    V1ImportError,
    import_v1_sqlite,
)
from backend.app.core.settings import DatabaseSettings, redact_database_url
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
    session_scope,
)
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyOpportunityRepository,
)

EXIT_OK: Final[int] = 0
EXIT_UNUSABLE: Final[int] = 1
# 2 is argparse's own usage error, so the "ran but lost rows" status skips it.
EXIT_INCOMPLETE: Final[int] = 3

# Enough rows to see the pattern, few enough that a bad migration does not scroll
# the counts off the screen. The counts by code above the list are never capped.
DEFAULT_MAX_ISSUES: Final[int] = 20

def _positive(value: str) -> int:
    """An `int` argument that must be at least 1.

    `--limit 0` would read nothing and report a clean run, which is the one
    outcome a migration tool must never produce by accident.
    """
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {number}")
    return number


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.cli.import_v1",
        description="Import the V1 SQLite `jobs` table into the V2 PostgreSQL "
                    "schema. The V1 database is opened read-only and is never "
                    "modified.",
        epilog="The V2 database comes from JOBSEARCH_DATABASE_URL, DATABASE_URL, "
               "or the docker-compose development default, in that order.")
    # Required rather than defaulted to `data/tracker.db`: this command writes to
    # a database from a file named on the command line, and guessing which file
    # is not a service it should offer.
    parser.add_argument("--sqlite", type=Path, required=True, metavar="PATH",
                        help="path to the V1 SQLite database (read-only)")
    parser.add_argument("--on-error", type=OnError, choices=tuple(OnError),
                        default=OnError.SKIP,
                        help="what an unusable row does to the run: report it and "
                             "continue (skip, the default), or stop and roll the "
                             "whole import back (fail)")
    parser.add_argument("--dry-run", action="store_true",
                        help="map and write every row against the real schema, then "
                             "roll back — a rehearsal that reports what the real "
                             "run would do")
    parser.add_argument("--limit", type=_positive, metavar="N",
                        help="read only the first N rows, by ascending V1 id")
    parser.add_argument("--max-issues", type=_positive, metavar="N",
                        default=DEFAULT_MAX_ISSUES,
                        help=f"how many offending rows to list (default "
                             f"{DEFAULT_MAX_ISSUES}); the per-code counts are "
                             f"always complete")
    return parser.parse_args(argv)


def _by_code(report: ImportReport, outcome: ImportOutcome) -> str:
    """The breakdown printed next to a count: `(V1_DISCOVERED_DATE_INVALID: 28)`.

    Grouping on the code and not on the message is the reason `V1MappingErrorCode`
    exists: a message embeds the row id and the offending value, so counting
    messages would produce one group per row.
    """
    counts = Counter(issue.code for issue in report.issues
                     if issue.outcome is outcome)
    if not counts:
        return ""
    return "  (" + ", ".join(f"{code}: {count}"
                             for code, count in sorted(counts.items())) + ")"


def _format_report(report: ImportReport, *, sqlite_path: Path, redacted_url: str,
                   dry_run: bool, max_issues: int, aborted: bool = False) -> str:
    """The report a human reads, and the record of what this run did.

    Prints the source, the target and the mode as well as the counts: a report
    that does not say which file went into which database is not evidence of
    anything a week later.

    `mode` states whether the work was kept, and it has to: an aborted run has
    non-zero `imported`/`updated` counts for rows it wrote and then rolled back,
    and a report that called that "applied" would be a false record of what is in
    the database.
    """
    if aborted:
        kept = "rolled back; --on-error fail stopped at the first unusable row"
    elif dry_run:
        kept = "dry run, rolled back"
    else:
        kept = "applied"
    lines = [
        "V1 -> V2 import report",
        f"  source        {sqlite_path}",
        f"  database      {redacted_url}",
        f"  mode          {kept}",
        f"  rows read     {report.rows_read}",
        f"  imported      {report.imported}",
        f"  updated       {report.updated}",
        f"  skipped       {report.skipped}{_by_code(report, ImportOutcome.SKIPPED)}",
        f"  failed        {report.failed}{_by_code(report, ImportOutcome.FAILED)}",
    ]
    if report.issues:
        shown = report.issues[:max_issues]
        lines.append(f"  rows that did not land ({len(shown)} of "
                     f"{len(report.issues)}):")
        lines.extend(f"    v1 id {issue.v1_id}  {issue.outcome}  {issue.code}  "
                     f"{issue.message}" for issue in shown)
        hidden = len(report.issues) - len(shown)
        if hidden:
            lines.append(f"    ... and {hidden} more; raise --max-issues to list them")
    return "\n".join(lines)


async def _import(args: argparse.Namespace,
                  settings: DatabaseSettings) -> ImportReport:
    """Open the V2 database, run the import in one transaction, close it.

    `NullPool` because this process performs one import and exits; a pool would
    only leave a connection to reap. `commit=not args.dry_run` is where the
    rehearsal is decided — the importer itself has no idea whether its work will
    be kept, so a dry run exercises exactly the same code path as a real one.
    """
    engine = create_async_database_engine(settings, poolclass=NullPool)
    try:
        session_factory = create_session_factory(engine)
        async with session_scope(session_factory,
                                 commit=not args.dry_run) as session:
            return await import_v1_sqlite(
                args.sqlite,
                repository=SqlAlchemyOpportunityRepository(session),
                # The savepoint the importer wraps every row in. Passing the bound
                # method keeps `sqlite3` and `AsyncSession` in separate modules.
                savepoint=session.begin_nested,
                on_error=args.on_error,
                limit=args.limit)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the import and return the process exit status.

    `argv` is a parameter so the test suite can call this as a function; every
    failure path returns a status rather than raising, so a CI job reads one
    number and a developer reads the report above it.
    """
    args = _parse_args(argv)
    try:
        settings = DatabaseSettings.from_env()
    except ValueError as exc:
        print(f"error: {redact_database_url(str(exc))}", file=sys.stderr)
        return EXIT_UNUSABLE

    def render(report: ImportReport, *, aborted: bool = False) -> None:
        print(_format_report(report, sqlite_path=args.sqlite,
                             redacted_url=settings.redacted_url,
                             dry_run=args.dry_run, max_issues=args.max_issues,
                             aborted=aborted))

    try:
        report = asyncio.run(_import(args, settings))
    except V1ImportAborted as exc:
        # Before `V1ImportError`, which it subclasses. The report is printed even
        # though the transaction was rolled back: what stopped the run is the
        # single most useful thing this command can say.
        render(exc.report, aborted=True)
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE
    except V1ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    except SQLAlchemyError as exc:
        # A database that cannot be reached or migrated is an environment problem,
        # not a data problem, so it reads as "nothing was imported" and not as a
        # partial success. The message is redacted in case it embedded the DSN.
        print(f"error: the V2 database at {settings.redacted_url} is not usable: "
              f"{type(exc).__name__}: {redact_database_url(str(exc))}",
              file=sys.stderr)
        return EXIT_UNUSABLE

    render(report)
    return EXIT_OK if report.is_clean else EXIT_INCOMPLETE


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
