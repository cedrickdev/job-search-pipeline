"""phase 4 identity and saved searches

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-05 01:53:56.587327

What Phase 4 owes the schema: an account that can authenticate, a revocable
session, the candidate profile onboarding actually fills in, and the saved searches
that replace V1's single-user `config/searches.yaml`.

Six tables are created — `user_sessions`, `candidate_languages`,
`candidate_work_authorizations`, `candidate_availability_slots`,
`search_profiles`, `search_areas` — and two are completed: `users` gains its login
identifier, its credential and its lockout counters, `candidate_profiles` gains a
name, a location and an availability window.

*Three columns are added NOT NULL with no backfill, and that is safe by
construction rather than by luck.* Revision 0002 created `users` and
`candidate_profiles` so that user-scoped rows could carry a real foreign key before
anything could authenticate, and nothing between that revision and this one could
write a row into either: there was no authentication, no route that creates an
account, and the V1 importer touches neither table. On a database that somehow does
hold one, `ADD COLUMN email TEXT NOT NULL` fails and the whole revision rolls back
— which is the right outcome, because an email address and a password hash cannot
be invented for an existing account and a placeholder credential would be worse
than a failed migration.

`candidate_profiles.label` is dropped here. It was a nullable free-text stand-in
for a name the Phase 1 domain did not model yet; `display_name` is that column, and
keeping both would leave two answers to "what is this profile called".

Edited after `alembic revision --autogenerate` in the two ways revision 0002
documents. *No application imports:* `sa.DateTime(timezone=True)` is the
`TIMESTAMPTZ` that `UtcDateTime` emits and the `Geography` below is the
`geography(Point,4326)` that `GeographyPoint` emits, so this file keeps producing
the same schema after those classes are renamed or deleted. *Repetition factored
out:* the helpers produce exactly the columns autogenerate wrote, and the
schema-drift test compares the result against `Base.metadata`, which is what keeps
that a claim rather than a hope.

The helpers are restated rather than imported from revision 0002. A revision is an
immutable record, and one revision importing another would make deleting or
squashing the older file break the newer one.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class Geography(sa.types.UserDefinedType[str]):
    """`geography(Point,4326)`, spelled out so this file stands alone.

    Only `get_col_spec` matters: it is the type name PostgreSQL is given in
    `CREATE TABLE`, and PostGIS records the column in `geography_columns` from that
    declaration — which is what makes the GiST index on `search_areas.center`
    possible.
    """

    cache_ok = True

    def get_col_spec(self, **kwargs: Any) -> str:
        return "geography(Point,4326)"


# Shared type instances: a SQLAlchemy type object carries no column state, so one
# instance can describe every column that uses it.
_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_GEOGRAPHY_POINT = Geography()
# A SHA-256 in lower-case hex is always 64 characters, so the digest columns are
# fixed-width by nature and the width is stated rather than left to TEXT.
_DIGEST = sa.String(length=64)
# ISO 3166-1 alpha-2 and ISO 639-1: two characters, and the length is the
# validation rather than a guess about how long a value might get.
_CODE = sa.String(length=2)
_TEXT_ARRAY = postgresql.ARRAY(_TEXT)
_EMPTY_TEXT_ARRAY = sa.text("'{}'::text[]")

# The member lists the array CHECKs on `search_profiles` police. The same values
# revision 0002 gave the scalar enum columns on `opportunities`; a saved search
# filters on them, so both spellings have to agree and both are written out.
_OPPORTUNITY_TYPES = ("FULL_TIME", "PART_TIME", "STUDENT_JOB", "INTERNSHIP",
                      "APPRENTICESHIP", "WORK_STUDY", "GRADUATE", "TEMPORARY",
                      "FREELANCE")
_CONTRACT_TYPES = ("PERMANENT", "FIXED_TERM", "TEMPORARY_AGENCY", "SERVICE_CONTRACT")
_WORKPLACE_MODES = ("ON_SITE", "HYBRID", "REMOTE")

def _enum(name: str, *members: str) -> sa.Enum:
    """A `VARCHAR(32)` plus a CHECK on the permitted values.

    No PostgreSQL `ENUM` type, for the reason revision 0002 gives: adding a member
    to a native enum is DDL that cannot share a transaction with a table rewrite,
    while widening a CHECK is an ordinary migration.
    """
    return sa.Enum(*members, name=name, native_enum=False, create_constraint=True,
                   length=32)


def _timestamps() -> tuple[sa.Column[Any], ...]:
    """`created_at` and `updated_at`, as `TimestampedMixin` declares them.

    The database clock sets both, so two processes inserting concurrently cannot
    disagree about order because one of them had a skewed system clock.
    """
    return (
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
    )


def _location_columns() -> tuple[sa.Column[Any], ...]:
    """The flattened `Location`, as `LocationColumnsMixin` declares it.

    Identical to what revision 0002 gave `company_locations` and `opportunities`,
    which is the property that matters: one radius query has to run against any of
    the three tables, and that is only true while the geography column is declared
    the same way in each.
    """
    return (
        sa.Column("location_country", _CODE, nullable=True),
        sa.Column("location_region", _TEXT, nullable=True),
        sa.Column("location_city", _TEXT, nullable=True),
        sa.Column("location_postal_code", _TEXT, nullable=True),
        sa.Column("location_point", _GEOGRAPHY_POINT, nullable=True),
        sa.Column("location_raw", _TEXT, nullable=True),
    )


def _ordinal() -> sa.Column[Any]:
    """`ordinal`, on every child table whose parent holds an ordered tuple.

    The domain's collections are tuples and their order is information the
    candidate supplied — the first language listed is the one they lead with. A
    child table has no inherent order, so the position is stored.
    """
    return sa.Column("ordinal", sa.SmallInteger(), nullable=False)


def _allow_list(column: str) -> sa.Column[Any]:
    """One of a saved search's filters: `TEXT[]`, NOT NULL, empty by default.

    Empty means "no restriction", not "match nothing". The server default is `'{}'`
    rather than NULL so that intent has exactly one representation — a nullable
    array would give it two, and the first query written with `= ANY` on the NULL
    one would silently match no postings at all.
    """
    return sa.Column(column, _TEXT_ARRAY, server_default=_EMPTY_TEXT_ARRAY,
                     nullable=False)


def _elements_present(column: str) -> sa.CheckConstraint:
    """No NULL and no empty string inside one of those arrays.

    An array is the one column type where NOT NULL says nothing about the elements:
    `ARRAY[NULL]::text[]` is a perfectly non-null array of one null. `array_position`
    is the containment test that works for NULL, where `= ANY` would evaluate to
    NULL and pass.
    """
    return sa.CheckConstraint(
        f"array_position({column}, NULL) IS NULL"
        f" AND array_position({column}, '') IS NULL",
        name=op.f(f"ck_search_profiles_{column}_elements_present"))


def _members(column: str, values: Sequence[str]) -> sa.CheckConstraint:
    """A `TEXT[]` whose every element is one of `values`.

    `<@` is array containment, which is exactly "every element of the left array
    appears in the right one" — the array form of the CHECK a scalar enum column
    gets. An empty array is contained in anything, which is the convention above.
    """
    joined = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} <@ ARRAY[{joined}]::text[]",
                              name=op.f(f"ck_search_profiles_{column}_members"))


# `unnest` would need a subquery and a CHECK may not contain one, so the array is
# joined and the joined form is matched: a separator that cannot appear inside the
# element pattern turns "every element is a language code" into one regular
# expression. `array_to_string` is IMMUTABLE, which is what makes it legal here.
_POSTING_LANGUAGES_FORMAT = (
    "array_to_string(posting_languages, ',') ~ '^([a-z]{2}(,[a-z]{2})*)?$'"
)

# `SearchArea` is a discriminated union of three shapes flattened into one table,
# and this is what keeps the discriminator honest: each kind names exactly which
# columns must be present and which must be absent, so a row cannot be a RADIUS
# area with no centre or a COUNTRY area with a stray radius.
_AREA_SHAPE_MATCHES_KIND = (
    "(kind = 'COUNTRY'"
    " AND country IS NOT NULL AND center IS NULL AND radius_km IS NULL)"
    " OR (kind = 'RADIUS'"
    " AND country IS NULL AND center IS NOT NULL AND radius_km IS NOT NULL)"
    " OR (kind = 'REMOTE_ONLY' AND center IS NULL AND radius_km IS NULL)"
)

def _complete_users() -> None:
    """Turn the Phase 2 placeholder into an account that can authenticate.

    `email` is unique *and* CHECKed to be its own normalized form. The unique index
    alone would let `Ada@x.com` and `ada@x.com` both register, since PostgreSQL
    compares TEXT case-sensitively, and the whole point of `normalize_email` in the
    domain is that one person has one account — enforcing the normalized form here
    is what makes the index mean what it says.

    `password_hash` is TEXT with no width: Argon2id's encoded form is 97 characters
    today and a cost increase changes that, so `VARCHAR(97)` would turn the next
    parameter bump into a migration.

    `ck_users_user_status` is not created here and is not missing: `op.add_column`
    attaches the column to a table object, which is what makes a non-native `Enum`
    emit its member CHECK, so adding it again would fail as a duplicate. It is
    dropped explicitly in `downgrade`, where nothing creates it implicitly.
    """
    op.add_column("users", sa.Column("email", _TEXT, nullable=False))
    op.add_column("users", sa.Column("password_hash", _TEXT, nullable=False))
    op.add_column("users", sa.Column("status", _enum("user_status", "ACTIVE",
                                                     "DISABLED"),
                                     server_default=sa.text("'ACTIVE'"),
                                     nullable=False))
    # Deliberately not defaulted to `now()`: a verified-at that nothing verified is
    # worse than a NULL, and nothing sends mail in this phase.
    op.add_column("users", sa.Column("email_verified_at", _TIMESTAMPTZ, nullable=True))
    op.add_column("users", sa.Column("last_login_at", _TIMESTAMPTZ, nullable=True))
    op.add_column("users", sa.Column("failed_login_attempts", sa.SmallInteger(),
                                     server_default=sa.text("0"), nullable=False))
    op.add_column("users", sa.Column("locked_until", _TIMESTAMPTZ, nullable=True))
    op.add_column("users",
                  sa.Column("onboarding_completed_at", _TIMESTAMPTZ, nullable=True))
    op.create_unique_constraint(op.f("uq_users_email"), "users", ["email"])
    # Trimmed, lower-cased, and containing an `@` that is neither first nor last.
    # Not address validation — `EmailStr` does that — but enough that a row written
    # by psql cannot be a login identifier nobody can ever match.
    op.create_check_constraint(
        op.f("ck_users_email_normalized"), "users",
        "email = lower(btrim(email))"
        " AND position('@' in email) > 1"
        " AND position('@' in email) < length(email)")
    op.create_check_constraint(op.f("ck_users_failed_login_attempts_non_negative"),
                               "users", "failed_login_attempts >= 0")


def _create_user_sessions() -> None:
    """One authenticated browser, as a revocable server-side row.

    What is stored is a digest, never a token: the value the browser holds exists
    only in the response that set the cookie, so a leaked dump of this table is not
    a set of working credentials. The two format CHECKs are what keep that true —
    a 43-character `token_urlsafe` value written here by mistake violates
    `token_digest_format` instead of persisting in the clear.

    No IP address and no user-agent column. Both are personal data with retention
    rules of their own, neither informs any decision in this phase, and a session
    table is the easiest place to accumulate a request log nobody asked for.
    """
    op.create_table(
        "user_sessions",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("token_digest", _DIGEST, nullable=False),
        sa.Column("csrf_token_digest", _DIGEST, nullable=False),
        sa.Column("issued_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("expires_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("last_seen_at", _TIMESTAMPTZ, nullable=False),
        # NULL means live. A `revoked` boolean would lose *when*, which is the
        # column a security question ("was this session active at 14:05?") needs.
        sa.Column("revoked_at", _TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        sa.CheckConstraint("csrf_token_digest ~ '^[0-9a-f]{64}$'",
                           name=op.f("ck_user_sessions_csrf_token_digest_format")),
        sa.CheckConstraint("token_digest ~ '^[0-9a-f]{64}$'",
                           name=op.f("ck_user_sessions_token_digest_format")),
        # Issuing one secret twice would hand the session token to any script that
        # can read the CSRF cookie, which is readable by design.
        sa.CheckConstraint("token_digest <> csrf_token_digest",
                           name=op.f("ck_user_sessions_digests_are_independent")),
        sa.CheckConstraint("expires_at > issued_at",
                           name=op.f("ck_user_sessions_window_is_forward")),
        sa.CheckConstraint("last_seen_at >= issued_at",
                           name=op.f("ck_user_sessions_last_seen_after_issued")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_user_sessions_user_id_users"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sessions")),
        # The lookup every authenticated request performs, so the constraint is
        # also its index. Unique because two sessions sharing a token digest would
        # be two browsers holding one credential.
        sa.UniqueConstraint("token_digest",
                            name=op.f("uq_user_sessions_token_digest")),
    )
    # "Revoke my other sessions" reads by user; the expiry sweep reads by both.
    op.create_index("ix_user_sessions_user_id_expires_at", "user_sessions",
                    ["user_id", "expires_at"], unique=False)


def _complete_candidate_profiles() -> None:
    """Give the profile the contents onboarding collects.

    `display_name` replaces `label` and is NOT NULL: a profile the candidate cannot
    tell apart from their other one is not usable in a picker.

    Availability is five nullable columns plus a child table for the weekly slots.
    All five nullable on purpose — an all-NULL group with no slots reads back as
    "not stated", which is the same convention the location columns follow.
    """
    op.add_column("candidate_profiles", sa.Column("display_name", _TEXT,
                                                  nullable=False))
    op.add_column("candidate_profiles", sa.Column("headline", _TEXT, nullable=True))
    op.add_column("candidate_profiles",
                  sa.Column("availability_earliest_start", sa.Date(), nullable=True))
    op.add_column("candidate_profiles",
                  sa.Column("availability_latest_end", sa.Date(), nullable=True))
    op.add_column("candidate_profiles",
                  sa.Column("availability_min_weekly_hours", sa.Double(),
                            nullable=True))
    op.add_column("candidate_profiles",
                  sa.Column("availability_max_weekly_hours", sa.Double(),
                            nullable=True))
    op.add_column("candidate_profiles",
                  sa.Column("availability_notice_period_days", sa.SmallInteger(),
                            nullable=True))
    for column in _location_columns():
        op.add_column("candidate_profiles", column)
    # The profile's own coordinates, with the same index type the postings use, so
    # Phase 7 can answer "how far is this posting from home?" from either end.
    op.create_index("ix_candidate_profiles_location_point", "candidate_profiles",
                    ["location_point"], unique=False, postgresql_using="gist")
    op.create_check_constraint(
        op.f("ck_candidate_profiles_availability_window_ordered"),
        "candidate_profiles", "availability_earliest_start <= availability_latest_end")
    op.create_check_constraint(
        op.f("ck_candidate_profiles_availability_hours_ordered"), "candidate_profiles",
        "availability_min_weekly_hours <= availability_max_weekly_hours")
    op.create_check_constraint(
        op.f("ck_candidate_profiles_availability_hours_range"), "candidate_profiles",
        "availability_min_weekly_hours BETWEEN 0 AND 168"
        " AND availability_max_weekly_hours > 0"
        " AND availability_max_weekly_hours <= 168")
    op.create_check_constraint(
        op.f("ck_candidate_profiles_availability_notice_non_negative"),
        "candidate_profiles", "availability_notice_period_days >= 0")
    op.create_check_constraint(
        op.f("ck_candidate_profiles_location_country_format"), "candidate_profiles",
        "location_country ~ '^[A-Z]{2}$'")
    op.drop_column("candidate_profiles", "label")


def _create_candidate_children() -> None:
    """The three ordered collections a profile owns.

    Child tables rather than JSONB because each one is queried across rows —
    "candidates who read German at B2 or better" is a comparison, not a lookup —
    and because the enums are then something the database can police.

    They say `profile_id`, not `candidate_profile_id`. The qualifier is redundant
    inside a table already named `candidate_…`, and it is also unaffordable: the
    naming convention would derive a 72-character foreign-key name, and PostgreSQL
    truncates at 63 — a silently truncated name is one a migration cannot drop.
    """
    op.create_table(
        "candidate_languages",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("profile_id", _UUID, nullable=False),
        _ordinal(),
        sa.Column("language", _CODE, nullable=False),
        sa.Column("level", _enum("language_level", "A1", "A2", "B1", "B2", "C1", "C2",
                                 "NATIVE"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("language ~ '^[a-z]{2}$'",
                           name=op.f("ck_candidate_languages_language_format")),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["candidate_profiles.id"],
            name=op.f("fk_candidate_languages_profile_id_candidate_profiles"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_languages")),
        # One entry per language, which also indexes the foreign key — hence no
        # separate index on `profile_id` here or on the two tables below.
        sa.UniqueConstraint("profile_id", "language",
                            name=op.f("uq_candidate_languages_profile_id_language")),
    )

    op.create_table(
        "candidate_work_authorizations",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("profile_id", _UUID, nullable=False),
        _ordinal(),
        sa.Column("country", _CODE, nullable=False),
        sa.Column("status", _enum("work_authorization_status", "CITIZEN",
                                  "PERMANENT_RESIDENT", "WORK_PERMIT_HELD",
                                  "STUDENT_PERMIT_WITH_WORK_RIGHTS",
                                  "REQUIRES_SPONSORSHIP", "NOT_AUTHORIZED",
                                  "UNKNOWN"), nullable=False),
        sa.Column("permit_label", _TEXT, nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        # The column that makes a class of eligibility deterministic: a student
        # permit capped at 15h/week makes a 20h/week job ineligible rather than a
        # poor schedule fit. Phase 5's Country Packs supply the legal fact.
        sa.Column("permit_hours_cap", sa.Double(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("country ~ '^[A-Z]{2}$'",
                           name=op.f("ck_candidate_work_authorizations_country_format")),
        sa.CheckConstraint(
            "permit_hours_cap > 0 AND permit_hours_cap <= 168",
            name=op.f("ck_candidate_work_authorizations_permit_hours_cap_range")),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["candidate_profiles.id"],
            name=op.f("fk_candidate_work_authorizations_profile_id_candidate_profiles"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_work_authorizations")),
        sa.UniqueConstraint(
            "profile_id", "country",
            name=op.f("uq_candidate_work_authorizations_profile_id_country")),
    )

    op.create_table(
        "candidate_availability_slots",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("profile_id", _UUID, nullable=False),
        _ordinal(),
        sa.Column("weekday", _enum("weekday", "MONDAY", "TUESDAY", "WEDNESDAY",
                                   "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"),
                  nullable=False),
        # Whole local hours, no timezone: "Saturday mornings" means it in the shop's
        # local time, and storing an instant would invent precision nobody supplied.
        sa.Column("start_hour", sa.SmallInteger(), nullable=False),
        sa.Column("end_hour", sa.SmallInteger(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "start_hour BETWEEN 0 AND 23 AND end_hour BETWEEN 1 AND 24",
            name=op.f("ck_candidate_availability_slots_slot_hours_range")),
        sa.CheckConstraint(
            "start_hour < end_hour",
            name=op.f("ck_candidate_availability_slots_slot_hours_ordered")),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["candidate_profiles.id"],
            name=op.f("fk_candidate_availability_slots_profile_id_candidate_profiles"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_availability_slots")),
        # Overlap between two slots on one day is a domain invariant no row
        # constraint can see; the exact-duplicate case is expressible, and refused.
        sa.UniqueConstraint(
            "profile_id", "weekday", "start_hour",
            name=op.f("uq_candidate_availability_slots_profile_id_weekday_start_hour")),
    )


def _create_search_profiles() -> None:
    """The saved searches, and the areas they look in.

    This is what replaces V1's `config/searches.yaml`: a single-user file of
    free-text locations and a keyword blacklist. Two differences carry the phase —
    the areas are a child table with real geometry rather than strings, and the
    filters are allow-lists of typed values rather than excluded words.
    """
    op.create_table(
        "search_profiles",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("name", _TEXT, nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"),
                  nullable=False),
        _allow_list("queries"),
        _allow_list("title_keywords"),
        _allow_list("excluded_keywords"),
        _allow_list("opportunity_types"),
        _allow_list("contract_types"),
        _allow_list("workplace_modes"),
        _allow_list("posting_languages"),
        _allow_list("source_keys"),
        sa.Column("workload_min_percent", sa.SmallInteger(), nullable=True),
        sa.Column("workload_max_percent", sa.SmallInteger(), nullable=True),
        sa.Column("workload_min_weekly_hours", sa.Double(), nullable=True),
        sa.Column("workload_max_weekly_hours", sa.Double(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("workload_min_percent <= workload_max_percent",
                           name=op.f("ck_search_profiles_workload_percent_ordered")),
        sa.CheckConstraint(
            "workload_min_percent BETWEEN 1 AND 100"
            " AND workload_max_percent BETWEEN 1 AND 100",
            name=op.f("ck_search_profiles_workload_percent_range")),
        sa.CheckConstraint("workload_min_weekly_hours <= workload_max_weekly_hours",
                           name=op.f("ck_search_profiles_workload_hours_ordered")),
        sa.CheckConstraint(
            "workload_min_weekly_hours > 0 AND workload_min_weekly_hours <= 168"
            " AND workload_max_weekly_hours > 0 AND workload_max_weekly_hours <= 168",
            name=op.f("ck_search_profiles_workload_hours_range")),
        _elements_present("queries"),
        _elements_present("title_keywords"),
        _elements_present("excluded_keywords"),
        _elements_present("source_keys"),
        _members("opportunity_types", _OPPORTUNITY_TYPES),
        _members("contract_types", _CONTRACT_TYPES),
        _members("workplace_modes", _WORKPLACE_MODES),
        sa.CheckConstraint(_POSTING_LANGUAGES_FORMAT,
                           name=op.f("ck_search_profiles_posting_languages_format")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_search_profiles_user_id_users"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_profiles")),
    )
    # Discovery reads "every active search" and the account page reads "my
    # searches". One index serves both; a partial index on `is_active` could not
    # answer the second.
    op.create_index("ix_search_profiles_user_id_is_active", "search_profiles",
                    ["user_id", "is_active"], unique=False)

    op.create_table(
        "search_areas",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("search_profile_id", _UUID, nullable=False),
        _ordinal(),
        sa.Column("kind", _enum("search_area_kind", "COUNTRY", "RADIUS",
                                "REMOTE_ONLY"), nullable=False),
        sa.Column("country", _CODE, nullable=True),
        sa.Column("center", _GEOGRAPHY_POINT, nullable=True),
        sa.Column("radius_km", sa.Double(), nullable=True),
        sa.Column("label", _TEXT, nullable=True),
        *_timestamps(),
        sa.CheckConstraint(_AREA_SHAPE_MATCHES_KIND,
                           name=op.f("ck_search_areas_shape_matches_kind")),
        sa.CheckConstraint("radius_km > 0 AND radius_km <= 500",
                           name=op.f("ck_search_areas_radius_km_range")),
        sa.CheckConstraint("country ~ '^[A-Z]{2}$'",
                           name=op.f("ck_search_areas_country_format")),
        sa.ForeignKeyConstraint(
            ["search_profile_id"], ["search_profiles.id"],
            name=op.f("fk_search_areas_search_profile_id_search_profiles"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_areas")),
        # `ordinal` is the natural key rather than a convenience: two radius areas
        # can differ only by their radius, so there is nothing else to identify a
        # row by — and this is what makes re-saving a search an update of the same
        # rows instead of a delete-and-reinsert.
        sa.UniqueConstraint("search_profile_id", "ordinal",
                            name=op.f("uq_search_areas_search_profile_id_ordinal")),
    )
    op.create_index("ix_search_areas_center", "search_areas", ["center"], unique=False,
                    postgresql_using="gist")


def upgrade() -> None:
    """Create the Phase 4 schema.

    Order is dependency order: `search_areas` points at `search_profiles`, and the
    three candidate child tables point at `candidate_profiles`, whose own columns
    are added first so a failure there costs nothing.
    """
    _complete_users()
    _create_user_sessions()
    _complete_candidate_profiles()
    _create_candidate_children()
    _create_search_profiles()


def downgrade() -> None:
    """Return the schema to revision 0002, children first.

    Destructive by nature: every account, session, saved search and profile detail
    is deleted, and `candidate_profiles.label` comes back empty because the column
    that replaced it is dropped. That is what a downgrade means — it exists so the
    revision is reversible in development and in the test suite, not because running
    it against real data is ever routine.

    Each CHECK is dropped before the column it references. Dropping the column would
    take the constraint with it, so the reverse order would fail on a constraint that
    no longer exists.
    """
    op.drop_index("ix_search_areas_center", table_name="search_areas",
                  postgresql_using="gist")
    op.drop_table("search_areas")
    op.drop_index("ix_search_profiles_user_id_is_active", table_name="search_profiles")
    op.drop_table("search_profiles")
    op.drop_table("candidate_availability_slots")
    op.drop_table("candidate_work_authorizations")
    op.drop_table("candidate_languages")

    op.add_column("candidate_profiles", sa.Column("label", _TEXT, nullable=True))
    for name in ("ck_candidate_profiles_location_country_format",
                 "ck_candidate_profiles_availability_notice_non_negative",
                 "ck_candidate_profiles_availability_hours_range",
                 "ck_candidate_profiles_availability_hours_ordered",
                 "ck_candidate_profiles_availability_window_ordered"):
        op.drop_constraint(op.f(name), "candidate_profiles", type_="check")
    op.drop_index("ix_candidate_profiles_location_point",
                  table_name="candidate_profiles", postgresql_using="gist")
    for column in ("location_raw", "location_point", "location_postal_code",
                   "location_city", "location_region", "location_country",
                   "availability_notice_period_days", "availability_max_weekly_hours",
                   "availability_min_weekly_hours", "availability_latest_end",
                   "availability_earliest_start", "headline", "display_name"):
        op.drop_column("candidate_profiles", column)

    op.drop_index("ix_user_sessions_user_id_expires_at", table_name="user_sessions")
    op.drop_table("user_sessions")

    for name in ("ck_users_user_status",
                 "ck_users_failed_login_attempts_non_negative",
                 "ck_users_email_normalized"):
        op.drop_constraint(op.f(name), "users", type_="check")
    op.drop_constraint(op.f("uq_users_email"), "users", type_="unique")
    for column in ("onboarding_completed_at", "locked_until", "failed_login_attempts",
                   "last_login_at", "email_verified_at", "status", "password_hash",
                   "email"):
        op.drop_column("users", column)
