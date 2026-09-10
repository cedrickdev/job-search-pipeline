# tests/test_v2_persistence_migrations.py
"""The Phase 2 acceptance criterion, as a test: `alembic upgrade head` is enough.

A fresh PostgreSQL database must become the whole V2 schema — PostGIS included —
by running the migrations and nothing else. Every test here starts by dropping
`public`, so none of them can pass because of something a previous run left
behind.

The one that matters most is the drift test. `compare_metadata` asks PostgreSQL
what it actually has and compares it against the mapped classes; an empty diff is
the only proof that the hand-written revision and the models still describe the
same database. Without it, a column added to a model and forgotten in a migration
is discovered in production.

Two categories of object are filtered out of that comparison, both because they
are not this schema's to own: PostGIS's own tables and views, and the CHECK
constraints that belong to a non-native `Enum` type (see
`type_bound_check_constraint_names` — Alembic reflects them but does not see them
on the metadata side, so every one would be reported as removed).
"""
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from backend.app.domain.company import normalize_company_name
from backend.app.infrastructure.database import models  # imported: registers tables
from backend.app.infrastructure.database.base import (
    Base,
    type_bound_check_constraint_names,
)
from backend.app.infrastructure.database.engine import create_sync_database_engine
from backend.app.infrastructure.database.types import (
    is_postgis_owned,
    register_geography_reflection,
)
from tests.v2_database import (
    EXPECTED_AFTER_DOWNGRADE,
    alembic_config,
    downgrade_to_base,
    reset_schema,
    upgrade_to_head,
)

# Every table the revisions create, in dependency order: parents before the rows
# that reference them. Spelled out rather than derived from the metadata so that a
# table silently dropped from a model is a failure here.
V2_TABLES = frozenset({
    "companies", "company_aliases", "company_career_sites",
    "company_discovery_records", "company_locations", "users", "user_sessions",
    "candidate_profiles", "candidate_languages", "candidate_work_authorizations",
    "candidate_availability_slots", "search_profiles", "search_areas",
    "opportunities", "opportunity_source_records", "match_evaluations",
    "match_dimension_scores",
})

# The revision `alembic upgrade head` is expected to stop at. A revision added
# without updating this line is a revision nobody decided to ship.
HEAD_REVISION = "0004"

# Every `geography(Point,4326)` column, by the table that holds it. One radius
# query has to run against any of them, so they are declared identically and
# indexed identically — see the two tests below.
GEOGRAPHY_COLUMNS = frozenset({
    ("candidate_profiles", "location_point"),
    ("company_locations", "location_point"),
    ("opportunities", "location_point"),
    ("search_areas", "center"),
})

_TYPE_BOUND_CHECKS = type_bound_check_constraint_names(Base.metadata)


def _include_object(object_, name, type_, reflected, compare_to):
    """The filter `backend/migrations/env.py` applies, restated as a rule.

    Deliberately a copy and not an import: `env.py` runs migrations as a side
    effect of being imported, so it cannot be imported here — and what this test
    needs to assert is the rule, not that one function was called.
    """
    if is_postgis_owned(object_, name):
        return False
    return not (type_ == "check_constraint" and name in _TYPE_BOUND_CHECKS)


def _schema_drift(connection):
    """What autogenerate would still have to change. Empty means agreement."""
    register_geography_reflection()
    context = MigrationContext.configure(
        connection,
        opts={"target_metadata": Base.metadata, "include_object": _include_object,
              "compare_type": True})
    return compare_metadata(context, Base.metadata)


@pytest.fixture
def schema_engine(postgres_settings):
    """A synchronous engine on a database this test may drop and rebuild.

    The teardown migrates it back to head so that the session-scoped
    `migrated_database` fixture stays true for whatever runs next: a test that
    leaves the schema half-downgraded would fail its neighbours instead of itself.
    """
    engine = create_sync_database_engine(postgres_settings)
    try:
        yield engine
        reset_schema(engine)
    finally:
        engine.dispose()


def test_an_empty_database_becomes_the_whole_schema(schema_engine):
    """`alembic upgrade head` on nothing at all — the Phase 2 acceptance test."""
    reset_schema(schema_engine)
    with schema_engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        version = connection.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one()
    assert V2_TABLES <= tables
    assert version == HEAD_REVISION


def test_the_migrated_schema_and_the_models_describe_the_same_database(schema_engine):
    """No drift. The test that makes every other persistence test meaningful."""
    reset_schema(schema_engine)
    with schema_engine.connect() as connection:
        assert _schema_drift(connection) == []


def test_the_first_revision_installs_postgis_itself(schema_engine):
    """PostGIS arrives with the migrations, not with a hand-run SQL statement.

    The development container installs the extension into `template1`'s
    descendants for the *development* database, and the test database is created
    without it precisely so this can be verified: dropping `public` takes PostGIS
    with it, and only revision 0001 puts it back.
    """
    reset_schema(schema_engine)
    with schema_engine.connect() as connection:
        installed = {row[0] for row in connection.execute(
            text("SELECT extname FROM pg_extension"))}
        registered = {(row[0], row[1], row[2], row[3]) for row in connection.execute(
            text("SELECT f_table_name, f_geography_column, type, srid "
                 "FROM geography_columns"))}
    assert "postgis" in installed
    # Every point column is registered as a WGS84 point, which is what makes
    # `ST_DWithin` a metre-based radius query rather than a degree-based one.
    assert registered == {(table, column, "Point", 4326)
                          for table, column in GEOGRAPHY_COLUMNS}


def test_the_geography_columns_are_indexed_for_radius_queries(schema_engine):
    """A GiST index on each point column: `ST_DWithin` cannot use any other kind.

    Asserted against the live index definitions rather than the models, because a
    `postgresql_using="gist"` that never made it into a migration would still
    look right in `models.py`.
    """
    reset_schema(schema_engine)
    expected = {f"ix_{table}_{column}" for table, column in GEOGRAPHY_COLUMNS}
    with schema_engine.connect() as connection:
        definitions = {row[0]: row[1] for row in connection.execute(
            text("SELECT indexname, indexdef FROM pg_indexes "
                 "WHERE indexname = ANY(:names)"), {"names": sorted(expected)})}
    assert set(definitions) == expected
    for definition in definitions.values():
        assert "USING gist" in definition


def test_downgrade_undoes_everything_the_revisions_created(schema_engine):
    """`downgrade base` leaves an empty schema — no orphan table, no orphan type.

    Worth testing even though a deployment never runs it: a downgrade path that
    does not work is a migration that cannot be reviewed by trying it, and the
    partial-unique index and the enum CHECKs are exactly the objects a
    hand-written `downgrade()` forgets.
    """
    reset_schema(schema_engine)
    with schema_engine.begin() as connection:
        downgrade_to_base(connection)
    with schema_engine.connect() as connection:
        remaining = set(inspect(connection).get_table_names())
        # `spatial_ref_sys` stays: revision 0001 does not drop the extension, and
        # dropping PostGIS out from under another database on the same cluster
        # would be a worse bug than a leftover projection table.
        assert remaining <= EXPECTED_AFTER_DOWNGRADE


def test_the_schema_is_the_same_after_a_downgrade_and_a_second_upgrade(schema_engine):
    """down + up is a fixed point: a rehearsed migration leaves no residue."""
    reset_schema(schema_engine)
    with schema_engine.begin() as connection:
        downgrade_to_base(connection)
    with schema_engine.begin() as connection:
        upgrade_to_head(connection)
    with schema_engine.connect() as connection:
        assert _schema_drift(connection) == []


def test_running_the_migrations_twice_changes_nothing(schema_engine):
    """`alembic upgrade head` on a database already at head is a no-op.

    The property that makes the compose `migrate` service safe to run on every
    `docker compose up`, and the deployment step safe to retry.
    """
    reset_schema(schema_engine)
    with schema_engine.begin() as connection:
        upgrade_to_head(connection)
    with schema_engine.connect() as connection:
        assert _schema_drift(connection) == []
        assert connection.execute(
            text("SELECT count(*) FROM alembic_version")).scalar_one() == 1


def test_no_identifier_exceeds_postgresql_63_character_limit(schema_engine):
    """PostgreSQL truncates longer names silently, which breaks `drop_constraint`.

    Read from the live database, so it covers the names the naming convention
    generated as well as the ones written by hand — a truncated name matches
    nothing when a later migration tries to address it.
    """
    reset_schema(schema_engine)
    with schema_engine.connect() as connection:
        names = [row[0] for row in connection.execute(text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
            "UNION SELECT conname FROM pg_constraint con JOIN pg_namespace n "
            "ON n.oid = con.connamespace WHERE n.nspname = 'public'"))]
    too_long = [name for name in names if len(name) > 63]
    assert too_long == []


# The revision Phase 6 upgrades *from*, and the one it must be reversible to. A
# database at 0003 is a Phase 5 database: it holds companies, postings and
# evaluations, and none of the Phase 6 columns.
PRE_PHASE_6_REVISION = "0003"

# Two employers that only the real normalizer can reduce. `Logitech Europe S.A.`
# needs the joining-punctuation rule (`s.a.` → `sa`) and `Sàrl Dupont & Cie` needs
# accent folding plus `casefold`; `lower(btrim(name))` — the obvious SQL guess —
# gets both wrong, which is why the backfill runs the function instead of a
# `regexp_replace`.
#
# The first one already has an answer about spontaneous applications, because a
# Phase 5 row that does is the compatibility case: the new
# `spontaneous_support_matches_flag` CHECK has to accept a decided boolean with no
# evidence group beside it, or upgrading would reject data revision 0002 allowed.
_PHASE_6_SEED_ROWS = (
    ("11111111-1111-1111-1111-111111111111", "Logitech Europe S.A.",
     "https://WWW.Logitech.com/fr/", None, True),
    ("22222222-2222-2222-2222-222222222222", "Sàrl Dupont & Cie",
     None, "https://boards.greenhouse.io/dupont", None),
)


def _companies_at(connection, revision):
    """Roll back to `revision`, insert the two Phase 5 employers, return nothing.

    Inserting at the older revision is the point: it is the only way to test a
    backfill, since a database created at head has no rows for one to run over.
    """
    command.downgrade(alembic_config(connection), revision)
    for company_id, name, website, careers_url, spontaneous in _PHASE_6_SEED_ROWS:
        connection.execute(
            text("INSERT INTO companies (id, name, website, careers_url,"
                 " accepts_spontaneous_applications) VALUES (:id, :name, :website,"
                 " :careers_url, :spontaneous)"),
            {"id": company_id, "name": name, "website": website,
             "careers_url": careers_url, "spontaneous": spontaneous})


def test_revision_0004_backfills_the_comparison_forms_it_adds(schema_engine):
    """A Phase 5 database becomes a Phase 6 one without losing a company (§15).

    `normalized_name` cannot be given a server default — no constant is the
    comparison form of every name — so the revision has to compute it per row, and
    what it computes has to be what `Company.normalized_name` would have produced
    for the same employer. Asserted against the domain function rather than a
    hard-coded string, so the test fails on the drift it exists to catch.
    """
    reset_schema(schema_engine)
    with schema_engine.begin() as connection:
        _companies_at(connection, PRE_PHASE_6_REVISION)
    with schema_engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
    with schema_engine.connect() as connection:
        rows = {row[0]: row for row in connection.execute(text(
            "SELECT name, normalized_name, normalized_domain, identity_status,"
            " accepts_spontaneous_applications FROM companies"))}
    assert set(rows) == {row[1] for row in _PHASE_6_SEED_ROWS}
    for name, row in rows.items():
        assert row[1] == normalize_company_name(name)
        # Every new column takes its default: an existing employer has no identity
        # status anybody asserted yet.
        assert row[3] == "SEEDED"
    assert rows["Logitech Europe S.A."][2] == "logitech.com"
    # The tri-state answer revision 0002 stored survives, and the row that never
    # had one is still NULL rather than a guessed `False`.
    assert rows["Logitech Europe S.A."][4] is True
    assert rows["Sàrl Dupont & Cie"][4] is None
    # The careers-URL fallback: a company discovered from an ATS board has no
    # corporate site, and its board host is the only domain evidence there is.
    assert rows["Sàrl Dupont & Cie"][2] == "boards.greenhouse.io"


def test_revision_0004_refuses_a_name_it_cannot_normalize(schema_engine):
    """A row whose name normalizes to nothing fails the migration, loudly.

    The alternative is a comparison column holding `''`, which the CHECK refuses,
    or — worse — a value invented for an employer nobody named. Failing names the
    offending rows so an operator can decide; §1's "never fabricate" applies to a
    migration too.
    """
    reset_schema(schema_engine)
    with schema_engine.begin() as connection:
        _companies_at(connection, PRE_PHASE_6_REVISION)
        connection.execute(text(
            "INSERT INTO companies (id, name) VALUES "
            "('33333333-3333-3333-3333-333333333333', '—')"))
    with pytest.raises(RuntimeError) as failure:
        with schema_engine.begin() as connection:
            command.upgrade(alembic_config(connection), "head")
    assert "normalized_name" in str(failure.value)
    assert "—" in str(failure.value)
    # The failed upgrade left the database where it was: no half-added column.
    with schema_engine.connect() as connection:
        version = connection.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one()
        columns = {column["name"] for column in inspect(connection).get_columns(
            "companies")}
    assert version == PRE_PHASE_6_REVISION
    assert "normalized_name" not in columns


def test_downgrading_0004_keeps_every_company_it_extended(schema_engine):
    """Reversibility, with the data that makes it mean something.

    The three new tables are dropped and the thirteen columns go with them, but a
    company row survives as the name and URLs revision 0002 stored. An additive
    revision that destroyed the rows it extended would not be a downgrade.
    """
    reset_schema(schema_engine)
    with schema_engine.begin() as connection:
        _companies_at(connection, PRE_PHASE_6_REVISION)
    with schema_engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
    with schema_engine.begin() as connection:
        command.downgrade(alembic_config(connection), PRE_PHASE_6_REVISION)
    with schema_engine.connect() as connection:
        rows = {row[0]: (row[1], row[2]) for row in connection.execute(text(
            "SELECT name, website, careers_url FROM companies"))}
        remaining = set(inspect(connection).get_table_names())
        columns = {column["name"] for column in inspect(connection).get_columns(
            "companies")}
    assert rows == {"Logitech Europe S.A.": ("https://WWW.Logitech.com/fr/", None),
                    "Sàrl Dupont & Cie": (None,
                                          "https://boards.greenhouse.io/dupont")}
    assert not remaining & {"company_aliases", "company_career_sites",
                            "company_discovery_records"}
    assert not columns & {"normalized_name", "normalized_domain", "identity_status",
                          "ats_platform", "spontaneous_support"}
