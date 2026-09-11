# tests/test_v2_import_v1.py
"""Importing V1's SQLite tracker into the V2 schema, and refusing to damage it.

The V1 database is the operator's real job search. It stays the source of truth
until V2 replaces it, so the first thing asserted here is the negative one: the
file is byte-identical afterwards, and the connection the importer holds cannot
write to it even if a later edit to the module tried.

The rest is the skip/report/fail policy, which is what makes a migration over a
database scraped from a dozen job boards survivable. Most of it runs against a
list of dictionaries and a fake repository — including rows no SQLite file could
produce, and the failures no test could provoke on demand from a real one — while
the properties that only PostgreSQL can establish (savepoint isolation, and one
row per V1 job after two runs) run against the live schema.

`docs/PERSISTENCE.md §Import` is written against the codes counted here, and the
CLI's exit status is derived from them: 0 clean, 3 completed with rows that did
not land, 1 nothing imported at all. A skipped row must never pass for a clean
migration.
"""
import hashlib
import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.app.cli.import_v1 import (
    EXIT_INCOMPLETE,
    EXIT_OK,
    EXIT_UNUSABLE,
    _format_report,
    _positive,
    main,
)
from backend.app.compat.v1_import import (
    ImportIssue,
    ImportIssueCode,
    ImportOutcome,
    ImportReport,
    OnError,
    V1ImportAborted,
    V1ImportError,
    import_v1_jobs,
    import_v1_sqlite,
    open_v1_database,
    read_v1_jobs,
)
from backend.app.compat.v1_jobs import (
    V1MappingErrorCode,
    opportunity_from_v1_job,
    opportunity_id_for_v1_job,
)
from backend.app.domain.identifiers import OpportunityId
from backend.app.domain.opportunity import Opportunity
from backend.app.repositories.contracts import OpportunityRepository
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyOpportunityRepository,
)
from pipeline.db import connect, init_db
from tests.v2_builders import an_opportunity

def a_v1_row(job_id: int, **overrides) -> dict[str, object]:
    """One V1 `jobs` row, as `dict(sqlite3.Row)` yields it.

    Every column populated, including the ones V1 stores as scraped text
    (`salary` "80-100%" is a workload, not a wage) — those must reach `raw` and
    leave the typed fields unset.
    """
    row: dict[str, object] = {
        "id": job_id, "source": "wtj", "company": f"Employer {job_id}",
        "title": "Ingenieur logiciel", "url": f"https://example.test/{job_id}",
        "location": "Lausanne, Suisse", "remote_policy": "hybrid",
        "contract_type": "Full-time", "salary": "80-100%",
        "description": "Build and operate the platform.", "language": "fr",
        "posted_date": "2026-02-20", "discovered_date": "2026-03-01",
        "dedup_hash": f"v1-hash-{job_id}", "track": "job",
    }
    row.update(overrides)
    return row


def a_v1_database(directory: Path, *rows: dict[str, object]) -> Path:
    """A real V1 SQLite file, created by V1's own schema module.

    `pipeline.db.init_db` rather than a `CREATE TABLE` written here: the importer
    has to read the table V1 actually has, and a test-owned copy of the schema
    would drift from it without ever failing.
    """
    path = directory / "tracker.db"
    connection = connect(path)
    try:
        init_db(connection)
        for row in rows:
            names = ", ".join(row)
            placeholders = ", ".join(f":{name}" for name in row)
            # The column names come from this module's own row builders, never
            # from a caller's string.
            statement = f"INSERT INTO jobs ({names}) VALUES ({placeholders})"  # noqa: S608
            connection.execute(statement, row)
        connection.commit()
    finally:
        connection.close()
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def report_issue(v1_id: int, outcome: ImportOutcome) -> ImportIssue:
    """One entry in a report, for the tests about the report itself."""
    return ImportIssue(v1_id=v1_id, outcome=outcome,
                       code=ImportIssueCode.PERSISTENCE_FAILED.value,
                       message="why the row did not land")


class FakeOpportunityRepository:
    """The two methods the importer uses, over a dictionary.

    Everything else raises: the importer is supposed to need only "is it there?"
    and "write it", so a future version that reaches for a query fails loudly in
    these tests instead of quietly acquiring a dependency on a real database.

    `fail_on` makes one posting unwritable, which is how the per-row failure path
    is exercised without a schema.
    """

    def __init__(self, *, fail_on: OpportunityId | None = None,
                 error: Exception | None = None) -> None:
        self.stored: dict[OpportunityId, Opportunity] = {}
        self.writes = 0
        self._fail_on = fail_on
        self._error = error or IntegrityError(
            "INSERT INTO opportunities ...", {},
            Exception('duplicate key value violates unique constraint '
                      '"uq_opportunities_dedup_fingerprint"'))

    async def get(self, opportunity_id: OpportunityId) -> Opportunity | None:
        return self.stored.get(opportunity_id)

    async def upsert(self, opportunity: Opportunity) -> Opportunity:
        self.writes += 1
        if opportunity.id == self._fail_on:
            raise self._error
        self.stored[opportunity.id] = opportunity
        return opportunity

    async def get_by_source(self, source_key: str,
                            external_id: str) -> Opportunity | None:
        raise NotImplementedError

    async def get_by_fingerprint(self, fingerprint: str) -> Opportunity | None:
        raise NotImplementedError

    async def list_recent(self, *, limit: int = 100) -> tuple[Opportunity, ...]:
        raise NotImplementedError

    async def list_near(self, center: object, radius_meters: float, *,
                        limit: int = 100) -> tuple[object, ...]:
        raise NotImplementedError

    async def search_geo(self, query: object) -> tuple[object, ...]:
        raise NotImplementedError

    async def list_unlinked(self, *, limit: int = 100) -> tuple[Opportunity, ...]:
        raise NotImplementedError

    async def link_company(self, opportunity_id: OpportunityId,
                           company_id: object) -> bool:
        raise NotImplementedError


async def run_import(*rows: dict[str, object], **options) -> ImportReport:
    """Import `rows` through a fake repository, with no savepoint and no database.

    `nullcontext` stands in for `AsyncSession.begin_nested`: the importer needs
    "isolate this row" and nothing more, which is exactly what makes the policy
    testable without PostgreSQL.
    """
    repository = options.pop("repository", None) or FakeOpportunityRepository()
    return await import_v1_jobs(rows, repository=repository,
                                savepoint=nullcontext, **options)


def test_the_fake_repository_satisfies_the_contract_the_importer_declares():
    """The fake is only useful while it is the same shape as the real thing."""
    assert isinstance(FakeOpportunityRepository(), OpportunityRepository)


@pytest.mark.asyncio
async def test_a_clean_run_reports_every_row_imported():
    """The baseline: three good rows, nothing skipped, nothing failed."""
    report = await run_import(a_v1_row(1), a_v1_row(2), a_v1_row(3))
    assert (report.rows_read, report.imported, report.updated) == (3, 3, 0)
    assert report.issues == ()
    assert report.is_clean is True


@pytest.mark.asyncio
async def test_an_empty_v1_database_is_a_clean_run():
    """Nothing to import is not a failure: nothing was lost."""
    report = await run_import()
    assert (report.rows_read, report.imported) == (0, 0)
    assert report.is_clean is True


@pytest.mark.asyncio
async def test_the_second_run_reports_every_row_as_updated():
    """Idempotency, in the form the report makes observable.

    The same file imported twice must not produce a second copy of anything. The
    ids are derived from the V1 row ids, so the second pass finds every row
    already there — `imported` 0, `updated` 3, and one stored posting per V1 job.
    """
    repository = FakeOpportunityRepository()
    rows = (a_v1_row(1), a_v1_row(2), a_v1_row(3))
    first = await run_import(*rows, repository=repository)
    second = await run_import(*rows, repository=repository)

    assert (first.imported, first.updated) == (3, 0)
    assert (second.imported, second.updated) == (0, 3)
    assert len(repository.stored) == 3
    assert second.is_clean is True


@pytest.mark.asyncio
async def test_a_v1_row_always_lands_on_the_same_v2_identity():
    """`uuid5(namespace, "v1:jobs:<id>")`, which is what makes a retry safe.

    Asserted against `opportunity_id_for_v1_job` rather than against whatever the
    first run produced: a random id per run would satisfy "the same twice in one
    process" while still duplicating every row on the next invocation.
    """
    repository = FakeOpportunityRepository()
    await run_import(a_v1_row(7), repository=repository)
    assert set(repository.stored) == {opportunity_id_for_v1_job(7)}


@pytest.mark.asyncio
async def test_a_v1_date_becomes_an_instant_in_the_zone_the_caller_named():
    """V1's naive local dates are not reproduced; they are given a zone.

    `insert_job` writes `datetime.now().date().isoformat()` — no time, no offset.
    The zone has to come from outside the data, so it is a parameter, and the
    original string stays in `raw` for a later pass to revisit. Midnight in Zurich
    on 1 March 2026 is 23:00 UTC the day before, which is the whole point of
    storing `TIMESTAMPTZ`.
    """
    repository = FakeOpportunityRepository()
    await run_import(a_v1_row(1, discovered_date="2026-03-01"),
                     repository=repository,
                     default_timezone=ZoneInfo("Europe/Zurich"))
    stored = repository.stored[opportunity_id_for_v1_job(1)]
    assert stored.discovered_at == datetime(2026, 2, 28, 23, 0, tzinfo=UTC)
    assert stored.source.raw["v1_discovered_date"] == "2026-03-01"


@pytest.mark.asyncio
async def test_an_unparseable_discovered_date_is_reported_with_the_offending_value():
    """The documented V1 risk, and the reason the report carries a message.

    `discovered_date` is `TEXT NOT NULL` in V1 with nothing checking its shape, so
    a hand-edited row or an early import can hold anything. It is required by V2 —
    it becomes `discovered_at` — so the row cannot be written, and the count alone
    would leave nobody able to fix it.
    """
    report = await run_import(a_v1_row(1), a_v1_row(2, discovered_date="pas une date"))
    assert (report.imported, report.skipped, report.failed) == (1, 1, 0)
    issue = report.issues[0]
    assert (issue.v1_id, issue.outcome) == (2, ImportOutcome.SKIPPED)
    assert issue.code == V1MappingErrorCode.DISCOVERED_DATE_INVALID.value
    assert "'pas une date'" in issue.message
    assert report.is_clean is False


@pytest.mark.asyncio
async def test_a_row_that_cannot_say_which_row_it_is_still_reaches_the_report():
    """A NULL or non-numeric `id` is reported with `v1_id` unset, not dropped.

    Impossible through V1's schema and entirely possible through a JSON export of
    it. The row is unusable either way; silently ignoring it is what turns a
    migration into a data loss nobody notices.
    """
    report = await run_import(a_v1_row(1, id=None), a_v1_row(2, id="not-a-number"))
    assert [issue.v1_id for issue in report.issues] == [None, None]
    assert {issue.code for issue in report.issues} == \
        {V1MappingErrorCode.ID_INVALID.value}
    assert report.skipped == 2


@pytest.mark.asyncio
async def test_a_row_missing_its_provenance_is_skipped_and_named():
    """`source`, `company` and `title` are identity: without them there is no posting.

    All three are `NOT NULL` in V1, which is why this case is put through a list of
    dictionaries — a real file cannot produce it, and a JSON export or a future V1
    schema change can.
    """
    report = await run_import(a_v1_row(1, company=None), a_v1_row(2, source="   "))
    assert report.skipped == 2
    assert {issue.code for issue in report.issues} == \
        {V1MappingErrorCode.REQUIRED_COLUMN_MISSING.value}
    assert "'company'" in report.issues[0].message
    assert "'source'" in report.issues[1].message


@pytest.mark.asyncio
async def test_a_constraint_violation_is_attributed_to_its_row_and_the_run_goes_on():
    """One posting the database refuses must not cost the other thousands.

    `PERSISTENCE_FAILED` rather than a mapping code, because the distinction is
    what a reader acts on: the row mapped cleanly and the schema said no, so the
    fix is in the data and not in `v1_jobs.py`. The class name is kept in the
    message — a duplicate key and an over-long value read differently.
    """
    repository = FakeOpportunityRepository(fail_on=opportunity_id_for_v1_job(2))
    report = await run_import(a_v1_row(1), a_v1_row(2), a_v1_row(3),
                              repository=repository)
    assert (report.rows_read, report.imported, report.failed) == (3, 2, 1)
    issue = report.issues[0]
    assert (issue.v1_id, issue.outcome) == (2, ImportOutcome.FAILED)
    assert issue.code == ImportIssueCode.PERSISTENCE_FAILED.value
    assert issue.message.startswith("IntegrityError:")
    assert "uq_opportunities_dedup_fingerprint" in issue.message


@pytest.mark.asyncio
async def test_a_failure_that_is_not_the_rows_fault_stops_the_whole_run():
    """The important half of the narrow `except`: a dropped connection propagates.

    An unreachable database fails on every remaining row. A run that swallowed
    that would report "3000 rows failed" for what is one environment problem, and
    then exit as though it had merely lost some data.
    """
    repository = FakeOpportunityRepository(
        fail_on=opportunity_id_for_v1_job(2),
        error=OperationalError("SELECT 1", {}, Exception("connection closed")))
    with pytest.raises(OperationalError):
        await run_import(a_v1_row(1), a_v1_row(2), a_v1_row(3),
                         repository=repository)


@pytest.mark.asyncio
async def test_on_error_fail_stops_at_the_first_unusable_row():
    """The release-rehearsal policy: nothing partial.

    Raising rather than returning is what makes it mean that — the exception
    travels through `session_scope`, which rolls the transaction back. The partial
    report rides along on the exception so the operator is still told which row
    ended the run.
    """
    with pytest.raises(V1ImportAborted) as raised:
        await run_import(a_v1_row(1), a_v1_row(2, discovered_date=""),
                         a_v1_row(3), on_error=OnError.FAIL)
    report = raised.value.report
    assert (report.rows_read, report.imported) == (2, 1)
    assert report.issues[-1].v1_id == 2
    # The row that ended the run is named in the exception message, since that is
    # what reaches stderr and a CI log.
    assert "V1 jobs row 2" in str(raised.value)


@pytest.mark.asyncio
async def test_on_error_fail_stops_on_a_row_the_database_refuses_too():
    """Both classes of failure end the run, not only the mapping ones."""
    repository = FakeOpportunityRepository(fail_on=opportunity_id_for_v1_job(1))
    with pytest.raises(V1ImportAborted) as raised:
        await run_import(a_v1_row(1), a_v1_row(2), repository=repository,
                         on_error=OnError.FAIL)
    assert raised.value.report.issues[-1].code == \
        ImportIssueCode.PERSISTENCE_FAILED.value


def test_the_counts_cannot_disagree_with_the_list_of_issues():
    """`skipped` and `failed` are derived, not tracked in parallel.

    A report that said "900 imported" and listed 901 issues would be worse than no
    report, and the only way to guarantee it cannot happen is to compute one from
    the other.
    """
    report = ImportReport(rows_read=3, imported=1, updated=0, issues=(
        report_issue(1, ImportOutcome.SKIPPED),
        report_issue(2, ImportOutcome.FAILED)))
    assert (report.skipped, report.failed) == (1, 1)
    assert report.is_clean is False
    assert ImportReport(rows_read=2, imported=2).is_clean is True


def test_a_v1_posting_keeps_everything_v1_knew_about_it():
    """Nothing invented, nothing lost — the two rules, on one row.

    "80-100%" is a workload written in V1's `salary` column, so no `SalaryRange` is
    produced; the string survives under `v1_salary` for a Phase 5 parser. The same
    holds for the free-text location, which stays `Location.raw` until Phase 7
    geocodes it.
    """
    stored = opportunity_from_v1_job(a_v1_row(4))
    assert stored.salary is None
    assert stored.source.raw["v1_salary"] == "80-100%"
    assert stored.location is not None
    assert stored.location.raw == "Lausanne, Suisse"
    assert stored.location.point is None
    assert stored.company_name == "Employer 4"
    assert stored.dedup_fingerprint == "v1-hash-4"
    # V1's row id is local to V1's database and is not a source identifier.
    assert stored.source.external_id is None
    assert stored.source.raw["v1_id"] == "4"


@pytest.mark.asyncio
async def test_the_v1_file_is_byte_identical_after_an_import(tmp_path):
    """The property that matters most in this file.

    The V1 tracker is the operator's real job search and the import's only source.
    A hash over the whole file rather than a row count: an importer that opened it
    read-write would leave a changed header and a `-wal` sidecar even if it never
    issued an UPDATE.
    """
    path = a_v1_database(tmp_path, a_v1_row(1), a_v1_row(2))
    before = digest(path)
    repository = FakeOpportunityRepository()

    report = await import_v1_sqlite(path, repository=repository,
                                    savepoint=nullcontext)
    assert report.imported == 2
    assert digest(path) == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["tracker.db"]


def test_the_import_connection_cannot_write_even_if_asked_to(tmp_path):
    """`mode=ro` is enforced by SQLite, not by this code being careful.

    The guarantee has to survive a future edit to `v1_import.py`, so it is asserted
    on the connection itself: an INSERT through it raises. Nothing else in Phase 2
    can protect a file the operator cannot recreate.
    """
    path = a_v1_database(tmp_path, a_v1_row(1))
    with open_v1_database(path) as connection, \
            pytest.raises(sqlite3.OperationalError, match="readonly"):
        connection.execute("UPDATE jobs SET title = 'changed' WHERE id = 1")


def test_a_missing_v1_file_is_refused_by_name(tmp_path):
    """Not created, not treated as empty. An empty import would report a clean run."""
    with pytest.raises(V1ImportError, match="not found"):
        with open_v1_database(tmp_path / "absent.db"):
            pass


def test_a_database_without_a_jobs_table_is_refused(tmp_path):
    """A SQLite file that is not a V1 tracker.

    The likely mistake is pointing `--sqlite` at the wrong file. Reported as
    unusable — the CLI's exit 1 — rather than as zero rows read.
    """
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    with open_v1_database(path) as connection, \
            pytest.raises(V1ImportError, match="no 'jobs' table"):
        list(read_v1_jobs(connection))


def test_rows_are_read_oldest_first_so_a_limited_run_is_repeatable(tmp_path):
    """`ORDER BY id` with `LIMIT`, which is what makes a rehearsal mean anything.

    Without the ordering, `--limit 50` would read whatever SQLite happened to
    return, and a dry run over 50 rows would not be evidence about the same 50 rows
    the next run writes.
    """
    path = a_v1_database(tmp_path, a_v1_row(3), a_v1_row(1), a_v1_row(2))
    with open_v1_database(path) as connection:
        assert [row["id"] for row in read_v1_jobs(connection)] == [1, 2, 3]
    with open_v1_database(path) as connection:
        assert [row["id"] for row in read_v1_jobs(connection, limit=2)] == [1, 2]


@pytest.mark.asyncio
async def test_importing_the_same_file_twice_leaves_one_row_per_v1_job(db_session,
                                                                      tmp_path):
    """Idempotency against PostgreSQL, where the unique constraints actually are.

    The dictionary-backed test proves the counts; this proves the writes. A second
    pass over the same file must update the rows it created — not insert a second
    posting, and not a second source record either, which is the child row a random
    surrogate key would duplicate.
    """
    path = a_v1_database(tmp_path, a_v1_row(1), a_v1_row(2))
    repository = SqlAlchemyOpportunityRepository(db_session)

    first = await import_v1_sqlite(path, repository=repository,
                                   savepoint=db_session.begin_nested)
    second = await import_v1_sqlite(path, repository=repository,
                                    savepoint=db_session.begin_nested)
    assert (first.imported, first.updated) == (2, 0)
    assert (second.imported, second.updated) == (0, 2)
    assert len(await repository.list_recent()) == 2

    stored = await repository.get(opportunity_id_for_v1_job(1))
    assert stored is not None
    assert stored.company_name == "Employer 1"
    assert stored.source.source_key == "wtj"
    assert stored.discovered_at == datetime(2026, 3, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_one_row_the_schema_refuses_costs_only_that_row(db_session, tmp_path):
    """Savepoint-per-row, proved where it is needed: inside one PostgreSQL transaction.

    A fingerprint already taken by a posting V2 discovered on its own is the
    realistic collision — V1's own `dedup_hash` is UNIQUE, so the duplicate cannot
    come from the file. In PostgreSQL an error otherwise poisons the whole
    transaction, so without the savepoint this one row would cost every row after
    it as well.
    """
    repository = SqlAlchemyOpportunityRepository(db_session)
    await repository.upsert(an_opportunity(dedup_fingerprint="v1-hash-2"))
    path = a_v1_database(tmp_path, a_v1_row(1), a_v1_row(2), a_v1_row(3))

    report = await import_v1_sqlite(path, repository=repository,
                                    savepoint=db_session.begin_nested)
    assert (report.rows_read, report.imported, report.failed) == (3, 2, 1)
    assert report.issues[0].v1_id == 2
    assert report.issues[0].code == ImportIssueCode.PERSISTENCE_FAILED.value
    assert "uq_opportunities_dedup_fingerprint" in report.issues[0].message
    # The rows on either side of the refused one are there, and so is the posting
    # that owned the fingerprint.
    assert len(await repository.list_recent()) == 3
    assert await repository.get(opportunity_id_for_v1_job(2)) is None


@pytest.mark.parametrize("value", ["0", "-1"])
def test_the_limit_must_be_at_least_one_row(value):
    """`--limit 0` would read nothing and report a clean run.

    That is the single outcome a migration tool must never produce by accident, so
    argparse rejects it with a usage error instead.
    """
    with pytest.raises(Exception, match="positive integer"):
        _positive(value)


def test_the_report_says_which_file_went_into_which_database():
    """A report that does not name its source and target is not evidence.

    The URL is the redacted form — the DSN is the one configuration value that
    routinely carries a password — and the mode line says whether the work was
    kept, which is what makes a dry run distinguishable from a real one a week
    later.
    """
    rendered = _format_report(
        ImportReport(rows_read=2, imported=2), sqlite_path=Path("data/tracker.db"),
        redacted_url="postgresql+psycopg://jobsearch:***@127.0.0.1:55432/jobsearch",
        dry_run=True, max_issues=20)
    assert "data/tracker.db" in rendered
    assert ":***@127.0.0.1:55432/jobsearch" in rendered
    assert "dry run, rolled back" in rendered
    assert "rows read     2" in rendered


def test_an_aborted_run_is_never_described_as_applied():
    """`--on-error fail` rolled the transaction back, counts and all.

    The report still carries the `imported` count for rows that were written before
    the abort. Calling that "applied" would be a false record of what is in the
    database, which is worse than no report at all.
    """
    rendered = _format_report(
        ImportReport(rows_read=2, imported=1,
                     issues=(report_issue(2, ImportOutcome.FAILED),)),
        sqlite_path=Path("data/tracker.db"), redacted_url="postgresql+psycopg://x/y",
        dry_run=False, max_issues=20, aborted=True)
    assert "rolled back" in rendered
    assert "applied" not in rendered


def test_the_offending_rows_are_grouped_by_code_and_the_list_is_capped():
    """Counts by code are complete; the listing is not, and says so.

    Grouping on the message would produce one group per row, since a message
    embeds the id and the offending value — which is the reason the codes exist.
    """
    rendered = _format_report(
        ImportReport(rows_read=3, issues=tuple(
            report_issue(index, ImportOutcome.FAILED) for index in (1, 2, 3))),
        sqlite_path=Path("data/tracker.db"), redacted_url="postgresql+psycopg://x/y",
        dry_run=False, max_issues=2)
    assert f"({ImportIssueCode.PERSISTENCE_FAILED.value}: 3)" in rendered
    assert "rows that did not land (2 of 3)" in rendered
    assert "... and 1 more" in rendered


def a_v2_database(monkeypatch, settings) -> None:
    """Point the CLI's own settings resolution at the test database."""
    monkeypatch.setenv("JOBSEARCH_DATABASE_URL", settings.url)


def test_a_dry_run_exercises_the_real_schema_and_keeps_nothing(
        migrated_database, monkeypatch, tmp_path, capsys):
    """The rehearsal, end to end: argv in, exit status and report out.

    `--dry-run` is a parameter of the transaction boundary and not a branch in the
    importer, so this run maps and writes every row against the real PostgreSQL
    schema — constraints included — and then rolls back. The second run reporting
    the same rows as *imported* rather than updated is the proof nothing was kept.
    """
    a_v2_database(monkeypatch, migrated_database)
    path = a_v1_database(tmp_path, a_v1_row(1), a_v1_row(2))
    argv = ["--sqlite", str(path), "--dry-run"]

    assert main(argv) == EXIT_OK
    assert "dry run, rolled back" in capsys.readouterr().out
    assert main(argv) == EXIT_OK
    assert "imported      2" in capsys.readouterr().out


def test_a_run_that_lost_a_row_exits_three(migrated_database, monkeypatch, tmp_path,
                                           capsys):
    """A skipped row can never pass for a clean migration.

    Exit 3 rather than 1, and rather than 0: the run worked, and it did not import
    everything. 2 is left to argparse's own usage error.
    """
    a_v2_database(monkeypatch, migrated_database)
    path = a_v1_database(tmp_path, a_v1_row(1),
                         a_v1_row(2, discovered_date="pas une date"))

    assert main(["--sqlite", str(path), "--dry-run"]) == EXIT_INCOMPLETE
    printed = capsys.readouterr().out
    assert f"skipped       1  ({V1MappingErrorCode.DISCOVERED_DATE_INVALID.value}: 1)" \
        in printed
    assert "v1 id 2" in printed


def test_a_missing_v1_file_exits_one_and_says_so(migrated_database, monkeypatch,
                                                 tmp_path, capsys):
    """Nothing was imported, and the reason is an environment problem.

    Distinct from exit 3: there is no report to print and no policy to apply, so
    the operator is told the file is missing rather than handed counts of zero.
    """
    a_v2_database(monkeypatch, migrated_database)
    assert main(["--sqlite", str(tmp_path / "absent.db")]) == EXIT_UNUSABLE
    assert "not found" in capsys.readouterr().err


def test_an_unusable_database_url_is_refused_without_printing_its_password(
        monkeypatch, tmp_path, capsys):
    """The one path that runs before any connection is opened.

    A DSN is where a credential lives, and a refusal is exactly the moment one gets
    copied into a CI log. The check is `normalize_database_url`'s, and this asserts
    the CLI keeps its promise not to print the value it rejected.
    """
    monkeypatch.setenv("JOBSEARCH_DATABASE_URL",
                       "mysql://jobsearch:not-a-real-password@db.internal/jobsearch")
    assert main(["--sqlite", str(tmp_path / "tracker.db")]) == EXIT_UNUSABLE
    printed = capsys.readouterr().err
    assert "refusing database URL" in printed
    assert "not-a-real-password" not in printed
    assert "jobsearch:***@db.internal" in printed
