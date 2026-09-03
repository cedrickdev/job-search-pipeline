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
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

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
    downgrade_to_base,
    reset_schema,
    upgrade_to_head,
)

# The tables revision 0002 creates, in the order it creates them: parents before
# the rows that reference them. Spelled out rather than derived from the metadata
# so that a table silently dropped from a model is a failure here.
V2_TABLES = frozenset({
    "companies", "company_locations", "users", "candidate_profiles",
    "opportunities", "opportunity_source_records", "match_evaluations",
    "match_dimension_scores",
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
    assert version == "0002"


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
    # Both point columns are registered as WGS84 points, which is what makes
    # `ST_DWithin` a metre-based radius query rather than a degree-based one.
    assert registered == {("company_locations", "location_point", "Point", 4326),
                          ("opportunities", "location_point", "Point", 4326)}


def test_the_geography_columns_are_indexed_for_radius_queries(schema_engine):
    """A GiST index on each point column: `ST_DWithin` cannot use any other kind.

    Asserted against the live index definitions rather than the models, because a
    `postgresql_using="gist"` that never made it into a migration would still
    look right in `models.py`.
    """
    reset_schema(schema_engine)
    with schema_engine.connect() as connection:
        definitions = {row[0]: row[1] for row in connection.execute(
            text("SELECT indexname, indexdef FROM pg_indexes "
                 "WHERE indexname LIKE '%location_point'"))}
    assert set(definitions) == {"ix_company_locations_location_point",
                                "ix_opportunities_location_point"}
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
