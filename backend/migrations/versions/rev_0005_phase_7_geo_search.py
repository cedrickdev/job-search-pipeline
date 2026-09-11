"""phase 7 geo search

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-10 09:41:18.204553

What Phase 7 owes the schema is not a geography column — revision 0002 created
all four of those, GiST-indexed, precisely so this phase would not have to alter
a table under load. What is missing is everything *around* the coordinates: where
they came from, how precisely they locate anything, who produced them and when.

Five columns are added to each of the three tables that embed
`LocationColumnsMixin` (`opportunities`, `company_locations`,
`candidate_profiles`), one table is created (`geocoding_cache`), and two indexes
support the whole-country branch of a geo search. No column is dropped, no column
changes type, and nothing existing is rewritten, so a database at revision 0004
keeps every posting, employer and evaluation it had.

*The two enum columns are NOT NULL with a server default, and that is what makes
the revision non-destructive.* Every row already in the table means "the source
provided whatever is there, and nothing claims a precision" — which is exactly
`SOURCE_PROVIDED`/`UNKNOWN`. PostgreSQL 11 and later fill an added NOT NULL
column from its default without rewriting the heap, so the backfill is the
`ADD COLUMN` itself and there is no data migration to get wrong. Compare revision
0004, where `normalized_name` needed a real backfill because no default could
produce it: here one can, and stating it is more honest than computing it.

`location_provenance_coherent` is the domain validator
`Location._the_provenance_describes_coordinates_that_exist` as a CHECK, restated
on each of the three tables. Its three clauses close the three ways the group
could lie — a precision with no coordinates, a `GEOCODED` row with no point or no
geocoder, and geocoding metadata on a row no geocoder touched. The last matters
most, because Phase 7 §7 lets that metadata *veto* a later write: a stale
confidence would silently make a location unimprovable.

`geocoding_cache` is the one new table. §23 asks for a provider-aware cache with
a normalized key and expiry for failures, and the shape here is that read
strictly: the key is `(provider, country_hint, normalized_query)`, the primary
key is uuid5 over the same three values so a re-ask updates rather than inserts,
and `expires_at` is NULL for every outcome except `FAILED`. A timeout is the
absence of an answer, and caching one permanently would turn a provider outage
into a permanently unresolvable address.

Edited after `alembic revision --autogenerate` in the two ways revision 0002
documents: no application imports for column *types* (`sa.DateTime(timezone=True)`
is the `TIMESTAMPTZ` `UtcDateTime` emits), and the repetition factored into the
helpers below. The schema-drift test compares the result against `Base.metadata`,
which is what keeps that a claim.

As in revisions 0003 and 0004, the CHECK constraints behind the new enum columns
are *not* created explicitly: `op.add_column` attaches the column to a table
object, which is what makes a non-native `Enum` emit its member CHECK. They are
dropped explicitly in `downgrade`, where nothing creates them implicitly.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Shared type instances: a SQLAlchemy type object carries no column state, so one
# instance can describe every column that uses it.
_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_JSONB = postgresql.JSONB()
_CODE = sa.String(length=2)
# A provenance key — `^[a-z][a-z0-9_]*$`, at most 40 characters — as the domain
# types it. A geocoder's key is one of these: `nominatim`, not `Nominatim v1.4`.
_PROVENANCE_KEY = sa.String(length=40)
_EMPTY_JSON_ARRAY = sa.text("'[]'::jsonb")
_EMPTY_JSON_OBJECT = sa.text("'{}'::jsonb")

# The members of the four enums this revision introduces, written out. Each is a
# `StrEnum` in `backend.app.domain.common` or `backend.app.domain.geo`; the CHECK
# behind the column is what keeps the two lists agreeing, and the schema-drift
# test is what keeps this file and `models.py` agreeing.
_LOCATION_PROVENANCES = ("SOURCE_PROVIDED", "GEOCODED", "MANUAL")
_LOCATION_PRECISIONS = ("EXACT_ADDRESS", "POSTAL_CODE", "CITY", "REGION", "COUNTRY",
                        "UNKNOWN")
_GEOCODING_CONFIDENCES = ("HIGH", "MEDIUM", "LOW")
_GEOCODING_OUTCOMES = ("MATCHED", "AMBIGUOUS", "NOT_FOUND", "FAILED")

# The three tables that embed `LocationColumnsMixin`, and therefore gain the
# provenance group. Listed rather than discovered: a fourth one would be a
# decision, and a loop over the live catalogue would apply this silently.
_LOCATED_TABLES = ("candidate_profiles", "company_locations", "opportunities")

# `Location._the_provenance_describes_coordinates_that_exist`, as one expression.
# Three clauses, one per rule: a precision describes coordinates that must exist;
# a GEOCODED location must carry both the point and the name of what produced it;
# and only a GEOCODED location may state geocoding metadata at all.
_LOCATION_PROVENANCE_COHERENT = (
    "(location_precision = 'UNKNOWN' OR location_point IS NOT NULL)"
    " AND (location_provenance <> 'GEOCODED'"
    "      OR (location_point IS NOT NULL AND location_geocoder IS NOT NULL))"
    " AND (location_provenance = 'GEOCODED'"
    "      OR (location_confidence IS NULL AND location_geocoder IS NULL"
    "          AND location_geocoded_at IS NULL))"
)

# `GeocodingResult._the_payload_matches_the_outcome`, as one expression. An
# adapter is written against one example response, and `AMBIGUOUS` with a single
# candidate is exactly the bug that would quietly retire §6's prohibition on
# turning the first hit into truth.
_GEOCODING_PAYLOAD_MATCHES_OUTCOME = (
    "(outcome = 'MATCHED' AND place <> '{}'::jsonb"
    " AND alternatives = '[]'::jsonb)"
    " OR (outcome = 'AMBIGUOUS' AND place = '{}'::jsonb"
    " AND jsonb_array_length(alternatives) >= 2)"
    " OR (outcome IN ('NOT_FOUND', 'FAILED')"
    " AND place = '{}'::jsonb AND alternatives = '[]'::jsonb)"
)


def _enum(name: str, *members: str) -> sa.Enum:
    """A `VARCHAR(32)` plus a CHECK on the permitted values.

    No PostgreSQL `ENUM` type, for the reason revision 0002 gives: adding a member
    to a native enum is DDL that cannot share a transaction with a table rewrite,
    while widening a CHECK is an ordinary migration.
    """
    return sa.Enum(*members, name=name, native_enum=False, create_constraint=True,
                   length=32)


def _timestamps() -> tuple[sa.Column[Any], ...]:
    """`created_at` and `updated_at`, as `TimestampedMixin` declares them."""
    return (
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
    )


def _add_provenance_columns(table: str) -> None:
    """Give one located table the five columns that describe its coordinates.

    The order is deliberate: the columns first, then the coherence CHECK. A CHECK
    created before the columns it reads would fail to parse, and creating it after
    the defaults have been applied means it is validated against real rows — which
    is the assertion worth having, since a pre-existing row that violated it would
    otherwise surface much later as an unexplainable write failure.

    Every existing row satisfies the CHECK by construction: `SOURCE_PROVIDED` with
    `UNKNOWN` precision and three NULLs is the "nobody geocoded this" case, and it
    is the first clause of each of the three conjuncts.
    """
    op.add_column(table,
                  sa.Column("location_provenance",
                            _enum("location_provenance", *_LOCATION_PROVENANCES),
                            server_default=sa.text("'SOURCE_PROVIDED'"),
                            nullable=False))
    op.add_column(table,
                  sa.Column("location_precision",
                            _enum("location_precision", *_LOCATION_PRECISIONS),
                            server_default=sa.text("'UNKNOWN'"), nullable=False))
    op.add_column(table,
                  sa.Column("location_confidence",
                            _enum("location_confidence", *_GEOCODING_CONFIDENCES),
                            nullable=True))
    op.add_column(table, sa.Column("location_geocoder", _TEXT, nullable=True))
    op.add_column(table, sa.Column("location_geocoded_at", _TIMESTAMPTZ,
                                   nullable=True))
    op.create_check_constraint(
        op.f(f"ck_{table}_location_provenance_coherent"), table,
        _LOCATION_PROVENANCE_COHERENT)


def _index_the_country_branch() -> None:
    """Two btree indexes for the one geo predicate GiST cannot serve (§13, §25).

    A radius search is `ST_DWithin` on a geography column, and the four GiST
    indexes revision 0002 created already answer it. A *whole-country* search is
    not spatial at all — §13 requires it to be `location_country = 'CH'` against a
    canonical code rather than a substring match on a display string — and that
    predicate has no index at revision 0004.

    The honest statement of what these buy, since §25 forbids adding an index
    blindly: on a single-country dataset PostgreSQL will correctly ignore them,
    because a predicate matching most of the table is cheaper as a scan. They earn
    their keep in the two cases this application actually grows into — a search
    for the minority country once a second Country Pack exists, and the "which
    countries do we hold postings in" aggregate a Phase 8 map needs — and the cost
    of a btree on a two-character column is small enough that adding it now is
    cheaper than an `ALTER TABLE` on a populated table later.

    Nothing is added for `workplace_mode`, on the same reasoning taken the other
    way: a `REMOTE_ONLY` search filters one value of a three-member enum, no
    plausible dataset makes that selective, and there is no future shape in which
    it becomes so.
    """
    op.create_index("ix_opportunities_location_country", "opportunities",
                    ["location_country"], unique=False)
    op.create_index("ix_company_locations_location_country", "company_locations",
                    ["location_country"], unique=False)


def _create_geocoding_cache() -> None:
    """One provider's answer to one normalized question (§23).

    A table rather than an in-process dictionary because the enrichment pass is a
    CLI invocation (§22): the process exits, and a cache that died with it would
    let a nightly run ask a rate-limited public geocoder the same thousand
    questions every night.

    `country_hint` is NOT NULL with `''` meaning "no hint", rather than nullable,
    and the reason is the unique constraint: in PostgreSQL two NULLs are distinct,
    so a nullable column here would let the unhinted question — the common one —
    be stored twice with nothing to stop it.

    `only_a_failure_expires` is an equivalence rather than an implication, so it
    states both halves of §23's rule at once: a `FAILED` row must expire, and a
    row that answered must not. Without the second half a `MATCHED` result could
    be given a lifetime and quietly become a repeated provider call.

    No `user_id`, and no route by which one could arrive: the enrichment service
    geocodes `opportunities` and `company_locations`, never a candidate profile.
    A candidate's home address normalized into a shared cache key would be exactly
    the leak §33 names.
    """
    op.create_table(
        "geocoding_cache",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("provider", _PROVENANCE_KEY, nullable=False),
        sa.Column("country_hint", _CODE, server_default=sa.text("''"),
                  nullable=False),
        sa.Column("normalized_query", _TEXT, nullable=False),
        sa.Column("outcome", _enum("geocoding_outcome", *_GEOCODING_OUTCOMES),
                  nullable=False),
        # `GeocodedPlace` documents, not flattened columns: nothing queries this
        # table geographically — it is read by key and only by key — and a
        # `GeocodedPlace` has nowhere to put a credential by construction, so the
        # JSONB carries no provider blob (§6).
        sa.Column("place", _JSONB, server_default=_EMPTY_JSON_OBJECT,
                  nullable=False),
        sa.Column("alternatives", _JSONB, server_default=_EMPTY_JSON_ARRAY,
                  nullable=False),
        # Composed by the adapter from a fixed vocabulary, never a forwarded
        # provider message and never a formatted exception: a URL in an exception
        # carries its query string, and a query string carries the API key.
        sa.Column("detail", _TEXT, nullable=True),
        sa.Column("checked_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("expires_at", _TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        sa.CheckConstraint("country_hint ~ '^([A-Z]{2})?$'",
                           name=op.f("ck_geocoding_cache_country_hint_format")),
        sa.CheckConstraint("normalized_query <> ''",
                           name=op.f("ck_geocoding_cache_normalized_query_present")),
        sa.CheckConstraint("(outcome = 'FAILED') = (expires_at IS NOT NULL)",
                           name=op.f("ck_geocoding_cache_only_a_failure_expires")),
        sa.CheckConstraint(_GEOCODING_PAYLOAD_MATCHES_OUTCOME,
                           name=op.f("ck_geocoding_cache_payload_matches_outcome")),
        sa.CheckConstraint("provider ~ '^[a-z][a-z0-9_]*$'",
                           name=op.f("ck_geocoding_cache_provider_format")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_geocoding_cache")),
        # The lookup, and the idempotency key. The primary key is uuid5 over these
        # same three values, so the upsert needs no prior SELECT and this is the
        # backstop rather than the mechanism.
        sa.UniqueConstraint(
            "provider", "country_hint", "normalized_query",
            name=op.f("uq_geocoding_cache_provider_country_hint_normalized_query")),
    )


def upgrade() -> None:
    """Create the Phase 7 schema.

    Additive throughout. The located tables are extended in a fixed order so two
    runs against two databases produce the same catalogue, and the cache is
    created last because nothing else refers to it.
    """
    for table in _LOCATED_TABLES:
        _add_provenance_columns(table)
    _index_the_country_branch()
    _create_geocoding_cache()


def downgrade() -> None:
    """Return the schema to revision 0004.

    Destructive by nature: every geocoded coordinate keeps its point but loses the
    record of where it came from, and the cache is deleted outright. No location
    component is touched — a posting geocoded in Phase 7 survives as the
    coordinates revision 0002's columns already held, which is the point of an
    additive revision. The provenance is what is lost, so a re-upgrade must re-run
    the enrichment pass to say who produced them.

    Each CHECK is dropped before the columns it references. The three enum CHECKs
    per table were never created explicitly — `op.add_column` attached them — and
    nothing here drops them implicitly.
    """
    op.drop_table("geocoding_cache")
    op.drop_index("ix_company_locations_location_country",
                  table_name="company_locations")
    op.drop_index("ix_opportunities_location_country", table_name="opportunities")
    for table in reversed(_LOCATED_TABLES):
        for suffix in ("location_provenance_coherent", "location_confidence",
                       "location_precision", "location_provenance"):
            op.drop_constraint(op.f(f"ck_{table}_{suffix}"), table, type_="check")
        for column in ("location_geocoded_at", "location_geocoder",
                       "location_confidence", "location_precision",
                       "location_provenance"):
            op.drop_column(table, column)
