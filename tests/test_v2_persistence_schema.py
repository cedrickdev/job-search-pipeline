# tests/test_v2_persistence_schema.py
"""The database rules, asserted against the metadata instead of a code review.

`docs/ENGINEERING_STANDARDS.md §Database rules` and the Phase 2 order state four
policies that are easy to hold on four tables and impossible to hold on ninety
without a test: timezone-aware timestamps, native UUID keys, generated constraint
names, and enums as TEXT + CHECK. Each is checked here over *every* mapped column,
so a fifteenth table inherits the rule automatically and a table that opts out
fails.

Ownership is the other thing asserted here, in three groups. A shared fact carries
no `user_id`; a user-owned row names its owner and cascades from `users`; a
parent-owned row reaches its owner through exactly one parent and cascades from it.
Every table belongs to one group, which is what makes "delete my account" a single
`DELETE` (docs/ENGINEERING_STANDARDS.md §Security) and what Phase 4's
authorization filter relies on.

No database is needed: `Base.metadata` is the declaration, and these are questions
about the declaration. The companion file `test_v2_persistence_migrations.py` asks
PostgreSQL whether the migrations actually produced it.
"""
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    Double,
    Enum,
    ForeignKeyConstraint,
    Numeric,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID

from backend.app.infrastructure.database import models  # imported: registers tables
from backend.app.infrastructure.database.base import Base
from backend.app.infrastructure.database.types import GeographyPoint, UtcDateTime

TABLES = Base.metadata.tables

# Facts about the world: no owner, and no `user_id` on any of them. A posting seen
# by two users is one row (docs/V2_SPECIFICATION.md), so a `user_id` appearing here
# later would be a design change and not a detail — hence the test.
SHARED_TABLES = ("companies", "company_locations", "opportunities",
                 "opportunity_source_records")

# Rows one user owns, named by the `user_id` Phase 4's authorization filter reads.
USER_OWNED_TABLES = ("candidate_profiles", "match_evaluations", "search_profiles",
                     "user_sessions")

# Rows owned through a parent instead of directly: a language belongs to a profile,
# an area to a search profile, a dimension score to an evaluation. They carry no
# `user_id` on purpose — a second copy of the owner is a second thing that can be
# wrong — so each one is listed with the parent it cascades from.
PARENT_OWNED_TABLES = {
    "candidate_availability_slots": "candidate_profiles",
    "candidate_languages": "candidate_profiles",
    "candidate_work_authorizations": "candidate_profiles",
    "match_dimension_scores": "match_evaluations",
    "search_areas": "search_profiles",
}

# PostgreSQL truncates anything longer, and a truncated name is one a later
# migration cannot drop by name.
MAX_IDENTIFIER_LENGTH = 63


def _columns():
    """Every mapped column, as `(table_name, column)` pairs."""
    return [(name, column) for name, table in TABLES.items()
            for column in table.columns]


def _python_type(column):
    """The column's Python type, or `None` when the type does not claim one.

    `UtcDateTime` and `GeographyPoint` both raise `NotImplementedError` here:
    SQLAlchemy only defines `python_type` for the types that can promise one, and
    a `TypeDecorator` or a `UserDefinedType` is not obliged to.
    """
    try:
        return column.type.python_type
    except NotImplementedError:
        return None


def test_the_metadata_holds_exactly_the_fourteen_v2_tables():
    """A tripwire on the shape of the schema itself.

    `models.py` is the only place a V2 table may be declared, so the three
    ownership groups plus `users` are the inventory. A new table has to be added to
    one of them — which is the moment to ask whether it needs `user_id`, a cascade
    and a migration.
    """
    assert set(TABLES) == set(SHARED_TABLES) | set(USER_OWNED_TABLES) | set(
        PARENT_OWNED_TABLES) | {"users"}
    assert len(TABLES) == 14


@pytest.mark.parametrize("table_name", sorted(TABLES))
def test_every_table_has_a_primary_key_and_the_bookkeeping_columns(table_name):
    """One identity per row, and two persistence timestamps on every table.

    `created_at`/`updated_at` come from `TimestampedMixin` and are set by the
    database clock; a table that forgot the mixin would lose the only record of
    when a row was written, which is the first thing anyone wants during an
    import post-mortem.
    """
    table = TABLES[table_name]
    assert list(table.primary_key.columns.keys()) == ["id"]
    for name in ("created_at", "updated_at"):
        assert table.columns[name].server_default is not None


def test_every_timestamp_column_is_timestamptz_and_refuses_naive_values():
    """The Phase 2 timezone policy, over every column at once.

    V1 stored `datetime.now().isoformat()` — local, naive, ambiguous — and the
    order for Phase 2 is explicitly not to reproduce it. `UtcDateTime` is
    `TIMESTAMPTZ` *and* a guard that rejects a naive value at the driver
    boundary, so this asserts the type rather than the underlying `DateTime`:
    a plain `DateTime(timezone=True)` would satisfy the column type and still
    accept an ambiguous instant.
    """
    timestamps = [(table, column) for table, column in _columns()
                  if isinstance(column.type, UtcDateTime | DateTime)]
    assert timestamps, "no timestamp columns found — the query is wrong"
    for table, column in timestamps:
        assert isinstance(column.type, UtcDateTime), f"{table}.{column.name}"


def test_every_uuid_column_is_a_native_postgres_uuid():
    """Not CHAR(36). A native `uuid` is 16 bytes, compares as an integer pair and
    cannot hold a value that is not a UUID — and every V2 identifier is one,
    including the uuid5 ids the V1 import derives.
    """
    uuids = [(table, column) for table, column in _columns()
             if isinstance(column.type, PostgresUUID)]
    # Nine primary keys plus the foreign keys that point at them.
    assert len(uuids) >= len(TABLES)
    for table, column in uuids:
        assert column.type.as_uuid is True, f"{table}.{column.name}"
        assert column.type.python_type is UUID


def test_every_identifier_the_convention_generates_fits_in_63_characters():
    """Composite index and constraint names are the ones that run long.

    `NAMING_CONVENTION` names every column a constraint covers
    (`%(column_0_N_name)s`), which is what makes a name predictable — and what
    makes a four-column unique constraint the case to check.
    """
    names = [constraint.name for table in TABLES.values()
             for constraint in table.constraints] + \
            [index.name for table in TABLES.values() for index in table.indexes] + \
            list(TABLES)
    too_long = [name for name in names
                if name is not None and len(name) > MAX_IDENTIFIER_LENGTH]
    assert too_long == []


def test_no_constraint_or_index_is_left_unnamed():
    """An anonymous constraint cannot be dropped by a portable migration.

    SQLAlchemy will happily emit `CHECK (...)` with no name, PostgreSQL will
    invent `opportunities_check1`, and the migration that has to drop it later
    can only find out what it was called by querying the live database.
    """
    for table in TABLES.values():
        for constraint in table.constraints:
            assert constraint.name is not None, f"{table.name}: {constraint}"
        for index in table.indexes:
            assert index.name is not None, f"{table.name}: {index}"


@pytest.mark.parametrize("table_name", SHARED_TABLES)
def test_a_shared_fact_carries_no_owner(table_name):
    """`Opportunity` and `Company` are facts about the world, not user data.

    The Phase 2 order is explicit that they "do not inherently need `user_id`".
    Two users looking at the same posting must see one row, or deduplication,
    geocoding and every enrichment pass would have to run per user.
    """
    assert "user_id" not in TABLES[table_name].columns


@pytest.mark.parametrize("table_name", USER_OWNED_TABLES)
def test_user_owned_rows_name_their_owner_and_cascade_from_users(table_name):
    """The column the authorization filter reads, and its cascade.

    Every read of these tables is `WHERE user_id = :current_user`
    (`backend/app/repositories/sqlalchemy_repositories.py`), so the column has to
    exist and may not be NULL. `ondelete="CASCADE"` from `users` is what makes
    "delete my account" a single statement instead of a script that has to know
    every table — which matters for a project that will hold candidate data
    (docs/ENGINEERING_STANDARDS.md §Security).
    """
    table = TABLES[table_name]
    assert "user_id" in table.columns
    assert table.columns["user_id"].nullable is False
    to_users = [fk for fk in table.foreign_keys if fk.column.table.name == "users"]
    assert [fk.ondelete for fk in to_users] == ["CASCADE"]


@pytest.mark.parametrize("table_name", sorted(PARENT_OWNED_TABLES))
def test_parent_owned_rows_reach_their_owner_through_one_cascading_parent(table_name):
    """No second copy of the owner, and no way to orphan a child.

    A candidate's languages are the candidate's, and the only route to a user is
    the profile — so the parent FK is NOT NULL and cascades, and `user_id` is
    absent rather than duplicated. Two columns naming the same owner is two columns
    that can disagree, and the one a query forgets is the one that leaks.
    """
    table = TABLES[table_name]
    assert "user_id" not in table.columns
    parent = PARENT_OWNED_TABLES[table_name]
    to_parent = [fk for fk in table.foreign_keys if fk.column.table.name == parent]
    assert [fk.ondelete for fk in to_parent] == ["CASCADE"]
    assert table.columns[to_parent[0].parent.name].nullable is False


def test_every_foreign_key_states_what_happens_on_delete():
    """No implicit `NO ACTION`.

    The choice is meaningful in both directions here — a deleted company must not
    take its postings with it (`SET NULL` on `opportunities.company_id`) while a
    deleted evaluation must take its dimension scores (`CASCADE`) — so leaving it
    unstated would be leaving it to whoever writes the first `DELETE`.
    """
    for table in TABLES.values():
        for constraint in table.constraints:
            if isinstance(constraint, ForeignKeyConstraint):
                assert constraint.ondelete in {"CASCADE", "SET NULL"}, \
                    f"{table.name}.{constraint.name}"


def test_every_enum_column_is_text_with_a_check_constraint():
    """No native PostgreSQL `ENUM` type anywhere in the schema.

    Adding a member to a native enum cannot be done in the same transaction as a
    statement that uses it, and removing one requires recreating the type and
    every column that references it. TEXT + CHECK is one drop/add pair
    (docs/PERSISTENCE.md §Enums), and `length` is fixed at 32 so a longer member
    is not also a column-type change.
    """
    enums = [(table, column) for table, column in _columns()
             if isinstance(column.type, Enum)]
    assert len(enums) >= 5
    for table, column in enums:
        assert column.type.native_enum is False, f"{table}.{column.name}"
        assert column.type.create_constraint is True, f"{table}.{column.name}"
        assert column.type.length == 32, f"{table}.{column.name}"
        assert column.type.name is not None, f"{table}.{column.name}"


def test_money_is_numeric_and_scores_are_double_precision():
    """Two column types where the default would be wrong.

    `4500.10` stored as a float comes back as `4500.099999999999`, so advertised
    pay is `NUMERIC(14, 2)`. Scores are compared against the value the domain
    produced, so they are `DOUBLE PRECISION` — SQLAlchemy's default `Float` is
    single precision and would round a `Score` on the way in.
    """
    for table, column in _columns():
        if _python_type(column) is Decimal:
            assert isinstance(column.type, Numeric), f"{table}.{column.name}"
            assert (column.type.precision, column.type.scale) == (14, 2)
    scores = [(table, column) for table, column in _columns()
              if column.name.endswith("_score") or column.name in {"score", "weight"}]
    assert scores
    for table, column in scores:
        assert isinstance(column.type, Double), f"{table}.{column.name}"


def test_free_text_columns_are_text_not_varchar():
    """`TEXT`, except where a length is a domain rule.

    PostgreSQL stores them identically, so a `VARCHAR(255)` on a job description
    buys nothing and costs a migration the day a posting is longer. The exceptions
    are the ISO code columns, where the length *is* the validation, and the two
    session digests, where 64 is what a hex SHA-256 measures and a different length
    means the value is not one.
    """
    sized = {(table, column.name) for table, column in _columns()
             if _python_type(column) is str
             and not isinstance(column.type, Text | Enum)}
    assert sized == {("opportunities", "posting_language"),
                     ("opportunities", "salary_currency"),
                     ("candidate_languages", "language"),
                     ("candidate_profiles", "location_country"),
                     ("candidate_work_authorizations", "country"),
                     ("company_locations", "location_country"),
                     ("opportunities", "location_country"),
                     ("search_areas", "country"),
                     ("user_sessions", "token_digest"),
                     ("user_sessions", "csrf_token_digest")}


def test_every_geography_column_is_a_wgs84_point():
    """One SRID and one geometry type across all four tables that store a location.

    A radius query is written once and runs against any of them, which is only true
    while they are declared identically — and `geography` (not `geometry`) is what
    makes `ST_DWithin(…, 100_000)` mean 100 km rather than 100 000 degrees.

    `search_areas.center` is the one that is not called `location_point`: it is not
    where something *is* but where a search is centred, and the radius beside it is
    the user's rather than the posting's.
    """
    points = [(table, column) for table, column in _columns()
              if isinstance(column.type, GeographyPoint)]
    assert {(table, column.name) for table, column in points} == {
        ("candidate_profiles", "location_point"),
        ("company_locations", "location_point"),
        ("opportunities", "location_point"),
        ("search_areas", "center")}
    for table, column in points:
        assert column.type.get_col_spec() == "geography(Point,4326)", table


def test_json_payloads_are_jsonb_with_an_empty_server_default():
    """JSONB, and never NULL.

    `raw` and the reason lists are read with `->>` and may be indexed later, which
    rules out `JSON` (text re-parsed on every read). The server-side `'{}'::jsonb`
    means a row written by a migration or by psql is as valid as one written by
    SQLAlchemy — a NULL payload would make every reader check for it.
    """
    payloads = [(table, column) for table, column in _columns()
                if isinstance(column.type, JSONB)]
    assert len(payloads) >= 4
    for table, column in payloads:
        assert column.nullable is False, f"{table}.{column.name}"
        assert column.server_default is not None, f"{table}.{column.name}"


def test_every_array_filter_is_an_empty_by_default_text_array_with_a_check():
    """`TEXT[] NOT NULL DEFAULT '{}'`, and never a bare array.

    An empty array is the saved search's way of saying "no restriction", so NULL
    would be a second spelling of the same intent — and the first query written with
    `= ANY` against the NULL one matches nothing without failing.

    The CHECK is the other half. An array column accepts anything the element type
    accepts, including a NULL element and a value no enum member matches, so each
    filter names itself in a constraint of its own table; a new allow-list added
    without one fails here rather than in the first search that reads it.
    """
    arrays = [(table, column) for table, column in _columns()
              if isinstance(column.type, ARRAY)]
    assert len(arrays) >= 8
    for table, column in arrays:
        where = f"{table}.{column.name}"
        assert isinstance(column.type.item_type, Text), where
        assert column.nullable is False, where
        assert column.server_default is not None, where
        checks = [constraint for constraint in TABLES[table].constraints
                  if isinstance(constraint, CheckConstraint)
                  and column.name in str(constraint.sqltext)]
        assert checks, f"{where}: no CHECK mentions this column"
