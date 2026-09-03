"""The declarative base every V2 table inherits, and its conventions.

Two decisions are centralized here so no table can quietly disagree with the
others:

**Constraint names are generated, not invented.** A PostgreSQL constraint that
was auto-named (`opportunities_company_id_fkey1`) cannot be dropped by a later
migration without first looking it up in the live database, which is how
migrations become environment-dependent. The naming convention makes every index
and constraint name a pure function of its table and columns, so Alembic can
address them by name and a reviewer can predict them.

**Python annotations decide column types.** `Mapped[datetime]` is always
`TIMESTAMPTZ` through `UtcDateTime`, and `Mapped[UUID]` is always a native
`uuid` column — the two rules docs/ENGINEERING_STANDARDS.md §Database rules
states. Getting them wrong on one table out of nine is exactly the kind of drift
a convention prevents and a code review does not.
"""
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import CheckConstraint, Double, MetaData, Text, func
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.app.domain.common import GeoPoint
from backend.app.infrastructure.database.types import GeographyPoint, UtcDateTime

# The Alembic-recommended convention, with `%(column_0_N_name)s` so a composite
# index or unique constraint names every column it covers instead of only the
# first — `uq_match_evaluations_candidate_profile_id` alone would be a lie about
# what is unique.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    # Requires every CHECK to be explicitly named. That is the point: an
    # anonymous CHECK is undroppable in a portable migration.
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for the V2 schema.

    `Decimal` is deliberately absent from the annotation map: money and working
    hours need different precisions (`NUMERIC(14,2)` against `NUMERIC(5,2)`), and
    a default would hide that choice. A `Mapped[Decimal]` column must name its
    own `Numeric`.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: dict[Any, Any] = {
        UUID: PostgresUUID(as_uuid=True),
        datetime: UtcDateTime(),
        # TEXT, not VARCHAR(n): PostgreSQL stores them identically, and an
        # invented length limit on a job description is a future migration.
        str: Text(),
        # Scores are on the unit interval and coordinates are WGS84 degrees;
        # DOUBLE PRECISION keeps both exact enough to compare with what the
        # domain produced. SQLAlchemy's default `Float` is single precision.
        float: Double(),
        GeoPoint: GeographyPoint(),
    }


class TimestampedMixin:
    """Row-level bookkeeping, distinct from anything the domain models.

    `discovered_at`, `fetched_at` and `evaluated_at` are domain facts and are
    supplied by the caller. These two are persistence facts — when this row was
    written — so the database clock sets them: `now()` is the transaction
    timestamp, which is consistent across every row a single import writes and
    is immune to a wrong clock on whichever machine ran the code.
    """

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(),
                                                 onupdate=func.now())


def type_bound_check_constraint_names(metadata: MetaData) -> frozenset[str]:
    """Names of the CHECK constraints that a column *type* owns, not a table.

    `Enum(native_enum=False, create_constraint=True)` attaches a CHECK listing the
    permitted strings. Alembic deliberately ignores those on the metadata side
    (`sqla_compat.all_table_check_constraints`) but reflects them from the database
    like any other CHECK, so an unfiltered autogenerate reports each one as
    "removed" and writes a `drop_constraint` — quietly deleting the validation.

    Both Alembic's `include_object` hook and the schema-drift test filter on this
    set, which is derived from the metadata rather than hard-coded so that adding
    an enum column cannot forget to update it.

    The consequence is worth stating plainly: autogenerate cannot see a change to
    an enum's members either. Adding or removing one needs a hand-written
    migration that drops and recreates the CHECK (docs/PERSISTENCE.md §Enums).

    Requires the mapped classes to have been imported; `metadata` is empty until
    then.
    """
    return frozenset(
        constraint.name
        for table in metadata.tables.values()
        for constraint in table.constraints
        # `_type_bound` is private to SQLAlchemy, and it is what Alembic itself
        # reads for this exact distinction. There is no public equivalent.
        if isinstance(constraint, CheckConstraint)
        and getattr(constraint, "_type_bound", False)
        and isinstance(constraint.name, str))
