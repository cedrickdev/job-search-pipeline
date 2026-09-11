"""Column types the V2 schema is built from, and the PostGIS expressions on them.

Two types, each closing a class of bug that documentation alone does not:

`UtcDateTime` refuses a naive datetime at the driver boundary. V1 stores
`datetime.now().isoformat()` — local time, no offset — which is why its date
arithmetic is ambiguous; the Phase 2 policy is that every stored instant is
timezone-aware and normalized to UTC, and this is where that policy is
mechanically true rather than merely stated.

`GeographyPoint` stores `geography(Point,4326)` and hands back the domain's
`GeoPoint`. It is deliberately small: Phase 2 needs a point, a radius predicate
and a distance, and a hand-written type keeps `backend/` free of an untyped
dependency while `mypy --strict` is a gate. GeoAlchemy2 is the documented
upgrade path when Phase 7 needs polygons, transforms and autogenerate support.
"""
import re
from datetime import UTC, datetime
from typing import Any, Final, cast

from sqlalchemy import (
    Boolean,
    ColumnExpressionArgument,
    DateTime,
    Dialect,
    Float,
    func,
    literal,
)
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.sql import ColumnElement
from sqlalchemy.types import TypeDecorator, UserDefinedType

from backend.app.domain.common import GeoBounds, GeoPoint

# WGS84. The only SRID this application stores: every source of coordinates
# (geocoders, job boards, map clients) speaks it, and a mixed-SRID table makes
# every distance query a guess about which one a row is in.
WGS84_SRID: Final[int] = 4326

_EWKT_POINT_RE: Final[re.Pattern[str]] = re.compile(
    r"^SRID=(?P<srid>\d+);POINT\((?P<longitude>\S+) (?P<latitude>\S+)\)$")

# `CREATE EXTENSION postgis` creates these in `public` and owns them.
POSTGIS_OWNED_TABLES: Final[frozenset[str]] = frozenset({
    "spatial_ref_sys",      # the only real table; the rest are views
    "geography_columns",
    "geometry_columns",
    "raster_columns",
    "raster_overviews",
})


def is_postgis_owned(database_object: Any, name: str | None) -> bool:
    """Whether a schema object belongs to the PostGIS extension.

    Autogenerate reflects the extension's own objects, finds no counterpart in
    `Base.metadata` and proposes to drop them — which would break PostGIS. Both
    Alembic's `include_object` hook and the schema-drift test filter on this, so
    the list lives here rather than being written twice.

    An index or a constraint is attributed to the table it belongs to; a table
    answers for itself.
    """
    owner = getattr(database_object, "table", None)
    return (name if owner is None else owner.name) in POSTGIS_OWNED_TABLES


class UtcDateTime(TypeDecorator[datetime]):
    """`TIMESTAMP WITH TIME ZONE` that refuses naive values and returns UTC.

    PostgreSQL already stores an absolute instant in `timestamptz`, but it will
    happily accept a naive literal and interpret it in the session's timezone —
    so the same insert means different instants on a developer's laptop and on a
    server. Rejecting naive values makes that impossible.

    On the way out, psycopg returns an aware datetime in the session timezone;
    `astimezone(UTC)` normalizes it so that a value read back compares equal to
    the domain object that was written, whatever `TimeZone` the connection has.
    """

    impl = DateTime
    cache_ok = True

    def __init__(self) -> None:
        super().__init__(timezone=True)

    def process_bind_param(self, value: datetime | None,
                           dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                "refusing to store a naive datetime: V2 timestamps are "
                "timezone-aware and stored in UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None,
                             dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # Only reachable if a column were declared `timestamp` without a
            # zone. Assuming UTC is the least-surprising reading and keeps the
            # invariant ("what comes out of this type is aware") unconditional.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def geo_point_to_ewkt(point: GeoPoint) -> str:
    """`GeoPoint` as extended WKT, the text form PostGIS parses.

    Longitude first: WKT is (x y), and a swapped pair is the classic PostGIS bug
    — it stays inside the valid range for most European coordinates, so it does
    not fail, it just puts the marker in the wrong country.

    `repr` rather than `str`/format: it is Python's shortest representation that
    round-trips a float exactly, so nothing is lost on the way in.
    """
    return f"SRID={WGS84_SRID};POINT({point.longitude!r} {point.latitude!r})"


def ewkt_to_geo_point(value: str) -> GeoPoint:
    """Parse what `ST_AsEWKT` returned for a point column."""
    match = _EWKT_POINT_RE.match(value.strip())
    if match is None:
        raise ValueError(f"not a WGS84 EWKT point: {value!r}")
    srid = int(match["srid"])
    if srid != WGS84_SRID:
        raise ValueError(f"expected SRID {WGS84_SRID}, got {srid}")
    return GeoPoint(latitude=float(match["latitude"]),
                    longitude=float(match["longitude"]))


class GeographyPoint(UserDefinedType[GeoPoint]):
    """A `geography(Point,4326)` column carrying the domain's `GeoPoint`.

    `geography` rather than `geometry`: distances come back in metres over the
    spheroid, so a 100 km radius (docs/IMPLEMENTATION_PLAN.md Phase 7) is
    literally `100_000` with no projection to choose and no per-country error to
    reason about. `geometry` would be faster over a small flat area and wrong
    over Switzerland-to-Sicily.

    Values travel as EWKT text in both directions. PostGIS 3.1 and later emit
    the shortest round-tripping representation from `ST_AsEWKT`, so a coordinate
    read back is bit-identical to the one stored — a test asserts that on a
    17-significant-digit point rather than trusting it.
    """

    cache_ok = True

    def __init__(self, geometry_type: str = "Point",
                 srid: int | str = WGS84_SRID) -> None:
        """Defaults are the only combination the models use.

        The parameters exist for reflection: PostgreSQL reports the column type as
        `geography(Point,4326)`, and SQLAlchemy hands whatever is inside those
        parentheses to this constructor. Keeping them means a reflected column
        rebuilds an equal type — and that a column someone changed to
        `geography(Polygon,4326)` or to SRID 2056 rebuilds a *different* one, so
        autogenerate reports it instead of comparing two identical `NullType`s.
        """
        self.geometry_type = str(geometry_type)
        self.srid = int(srid)

    def get_col_spec(self, **kwargs: Any) -> str:
        return f"geography({self.geometry_type},{self.srid})"

    def bind_processor(self, dialect: Dialect) -> Any:
        def process(value: GeoPoint | None) -> str | None:
            return None if value is None else geo_point_to_ewkt(value)
        return process

    def bind_expression(self, bindvalue: Any) -> ColumnElement[Any]:
        # The parameter is EWKT text; PostGIS needs it parsed before the
        # comparison or the insert sees it.
        return func.ST_GeogFromText(bindvalue, type_=self)

    def column_expression(self, colexpr: Any) -> ColumnElement[Any]:
        return func.ST_AsEWKT(colexpr, type_=self)

    def result_processor(self, dialect: Dialect, coltype: object) -> Any:
        def process(value: str | None) -> GeoPoint | None:
            return None if value is None else ewkt_to_geo_point(value)
        return process


def register_geography_reflection() -> None:
    """Teach SQLAlchemy's PostgreSQL dialect what a reflected `geography` column is.

    Without this, reflection yields `NullType` and a `SAWarning` for the two point
    columns, which means `compare_type` cannot compare them: a `geography` column
    replaced by a `geometry` one — a change that silently turns metre distances
    into degree distances — would pass both autogenerate and the drift test.

    Called explicitly, by `backend/migrations/env.py` and by the drift test, rather
    than on import: a module that mutates a third party's global registry as a
    side effect of being imported is the kind of coupling nobody finds later.
    Idempotent, so calling it twice in one process is not a problem.

    GeoAlchemy2 registers its own types through the same dict. This is the one
    line of it Phase 2 needs (see the module docstring on that dependency).
    """
    ischema_names.setdefault("geography", GeographyPoint)


def geography_literal(point: GeoPoint) -> ColumnElement[Any]:
    """A point as a PostGIS geography value usable inside a query.

    Separate from the column type because a literal in a WHERE clause has no
    column to inherit a type from: `ST_DWithin(col, <here>, metres)` needs the
    right-hand side parsed as geography, not compared as text.
    """
    return func.ST_GeogFromText(literal(geo_point_to_ewkt(point)),
                                type_=GeographyPoint())


def within_radius(column: ColumnExpressionArgument[Any], center: GeoPoint,
                  radius_meters: float) -> ColumnElement[bool]:
    """`ST_DWithin`, which is the form that uses the GiST index.

    Written as a helper so no repository spells a PostGIS function name: the
    predicate has to stay index-usable, and `ST_Distance(...) <= r` — the obvious
    alternative — silently degrades to a full scan.

    `column` is a `ColumnExpressionArgument` rather than a `ColumnElement` because
    that is what a mapped attribute (`OpportunityRow.location_point`) actually is;
    narrowing it would force every caller to cast.
    """
    return cast("ColumnElement[bool]",
                func.ST_DWithin(column, geography_literal(center), radius_meters,
                                type_=Boolean))


def distance_meters(column: ColumnExpressionArgument[Any],
                    center: GeoPoint) -> ColumnElement[float]:
    """Great-circle distance in metres, for ordering and for display."""
    return func.ST_Distance(column, geography_literal(center), type_=Float)


def within_bounds(column: ColumnExpressionArgument[Any],
                  bounds: GeoBounds) -> ColumnElement[bool]:
    """Whether a point falls inside a WGS84 viewport rectangle."""
    rectangle = func.cast(
        func.ST_MakeEnvelope(
            bounds.west, bounds.south, bounds.east, bounds.north, WGS84_SRID),
        GeographyPoint("Polygon", WGS84_SRID),
    )
    return cast("ColumnElement[bool]",
                func.ST_Intersects(column, rectangle, type_=Boolean))

