"""The V2 tables.

The schema is a flattening of the Phase 1 domain, not a second model of the
problem. Value objects (`Location`, `SalaryRange`, `WorkloadRange`) become column
groups on the entity that owns them, because a `location_city` on `opportunities`
is one row read where a joined `locations` table is two, and nothing else ever
refers to an opportunity's location by identity. Entities the specification names
in their own right (`OpportunitySourceRecord`, `CompanyLocation`,
`DimensionScore`) stay separate tables.

Three rules the tables are built to hold:

**The database refuses what the domain refuses.** Every `model_validator` in
`backend/app/domain` that a column group can express is also a named CHECK — a
salary range with no bound, a workload whose minimum exceeds its maximum, a score
outside the unit interval. A domain invariant enforced only in Python survives
exactly until the first `INSERT` that does not go through Python, which the V1
importer and any future backfill script are.

**Ownership is prepared, not invented.** `companies` and `opportunities` are
shared facts and carry no `user_id`; `match_evaluations` is user-scoped and
carries one, and its foreign keys cascade from `users` so that Phase 4 can add
authentication without a schema migration on the tables that matter.

**Enums are TEXT plus a CHECK, not PostgreSQL `ENUM` types.** Adding a member to
a native enum is a DDL statement that cannot run inside a transaction with a
table rewrite, and removing one is nearly impossible; a CHECK is one
`DROP CONSTRAINT`/`ADD CONSTRAINT` pair in a forward-only migration
(docs/ENGINEERING_STANDARDS.md §Database rules).
"""
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import TextClause

from backend.app.domain.candidate import WorkAuthorizationStatus
from backend.app.domain.common import (
    GeocodingConfidence,
    GeoPoint,
    LanguageLevel,
    LocationPrecision,
    LocationProvenance,
    SalaryPeriod,
    Weekday,
)
from backend.app.domain.company import (
    AtsPlatform,
    CareerSiteKind,
    CompanyIdentityStatus,
    CompanySeedKind,
    DetectionStatus,
    SpontaneousApplicationSupport,
)
from backend.app.domain.geo import GeocodingOutcome
from backend.app.domain.matching import MatchDimension
from backend.app.domain.opportunity import ContractType, OpportunityType, WorkplaceMode
from backend.app.domain.search import SearchAreaKind
from backend.app.domain.user import UserStatus
from backend.app.infrastructure.database.base import Base, TimestampedMixin

# Advertised pay. `NUMERIC`, never a float: 4500.10 has to come back as 4500.10.
# Two decimals is what postings quote, and 12 integer digits covers a yearly
# salary in any currency this application is likely to see.
_MONEY: Final[Numeric[Decimal]] = Numeric(14, 2)

# JSONB rather than JSON: it is indexable and it does not re-parse text on every
# read. The empty literals are server-side defaults so a row inserted by a
# migration or by psql is as valid as one inserted by SQLAlchemy.
_EMPTY_JSON_OBJECT: Final[TextClause] = text("'{}'::jsonb")
_EMPTY_JSON_ARRAY: Final[TextClause] = text("'[]'::jsonb")

# A saved search's allow-lists are `TEXT[]`, not child tables and not JSONB. They
# are read and written whole, they are never joined, and PostgreSQL can still
# index and containment-query them (`<@`, `&&`) — which JSONB would also allow but
# with no element type at all. An array of TEXT keeps the CHECKs below expressible.
_EMPTY_TEXT_ARRAY: Final[TextClause] = text("'{}'::text[]")

_LOCATION_COMPONENTS: Final[tuple[str, ...]] = (
    "location_country", "location_region", "location_city", "location_postal_code",
    "location_point", "location_raw",
)

# Every enum column is `VARCHAR(32)`, not `VARCHAR(<longest member>)`. Left to
# itself SQLAlchemy sizes the column to the longest current value, which makes
# adding a longer member two DDL changes (the CHECK *and* the column type)
# instead of one. 32 characters is beyond any member name in the domain.
_ENUM_LENGTH: Final[int] = 32


def enum_column(enum_class: type[StrEnum], name: str) -> Enum:
    """A `StrEnum` as a TEXT column with a named CHECK on its members.

    `values_callable` stores `member.value`; SQLAlchemy's default is
    `member.name`. For today's enums the two coincide, which is exactly why it
    has to be explicit — the first member whose name and value differ would
    otherwise silently change what is written.

    `validate_strings` makes a raw string that is not a member fail in Python as
    well, so a bad value surfaces where it was written rather than as a CHECK
    violation from the driver.
    """
    return Enum(enum_class, name=name, native_enum=False, create_constraint=True,
                validate_strings=True, length=_ENUM_LENGTH,
                values_callable=lambda members: [str(m.value) for m in members])



def _unit_interval(column: str) -> CheckConstraint:
    """`Score` — `Annotated[float, Field(ge=0.0, le=1.0)]` — as a CHECK.

    `BETWEEN` evaluates to NULL for a NULL column, and a CHECK only fails on
    FALSE, so this is correct for nullable scores without a special case.
    """
    return CheckConstraint(f"{column} BETWEEN 0.0 AND 1.0",
                           name=f"{column}_in_unit_interval")


def _code_format(column: str, pattern: str) -> CheckConstraint:
    """An ISO code column constrained to the shape the domain validates."""
    return CheckConstraint(f"{column} ~ '{pattern}'", name=f"{column}_format")


def _location_provenance_coherent() -> CheckConstraint:
    """`Location._the_provenance_describes_coordinates_that_exist`, as a CHECK.

    Three clauses, one per rule the domain validator states. A precision without
    coordinates would claim an accuracy for nothing; a `GEOCODED` row with no
    point is a resolution that did not resolve; and geocoding metadata on a row
    the geocoder never touched would misreport who is responsible for the value —
    which matters here specifically, because §7 lets that metadata *veto* a later
    write.

    Restated on each of the three tables that embed `LocationColumnsMixin`
    (a mixin cannot carry `__table_args__`), which is why it is a function.
    """
    return CheckConstraint(
        "(location_precision = 'UNKNOWN' OR location_point IS NOT NULL)"
        " AND (location_provenance <> 'GEOCODED'"
        "      OR (location_point IS NOT NULL AND location_geocoder IS NOT NULL))"
        " AND (location_provenance = 'GEOCODED'"
        "      OR (location_confidence IS NULL AND location_geocoder IS NULL"
        "          AND location_geocoded_at IS NULL))",
        name="location_provenance_coherent")


def _text_array_elements_present(column: str) -> CheckConstraint:
    """No NULL and no empty string inside a `TEXT[]`.

    Every array here holds `NonEmptyStr` or an enum value in the domain, and an
    array is the one column type where PostgreSQL's NOT NULL says nothing about
    the elements: `ARRAY[NULL]::text[]` is a perfectly non-null array of one null.
    `array_position` is the containment test that works for NULL — `= ANY` would
    evaluate to NULL and pass.

    A whitespace-only element is *not* caught, and that is stated rather than
    faked: `btrim` inside a per-element test needs `unnest`, and a CHECK may not
    contain a subquery. The domain's `NonEmptyStr` is what rejects "  ".
    """
    return CheckConstraint(
        f"array_position({column}, NULL) IS NULL"
        f" AND array_position({column}, '') IS NULL",
        name=f"{column}_elements_present")


def _enum_array_members(column: str, enum_class: type[StrEnum]) -> CheckConstraint:
    """A `TEXT[]` whose every element is a member of `enum_class`.

    `<@` is array containment, which is exactly "every element of the left array
    appears in the right one" — the array form of the CHECK `enum_column` writes
    for a scalar. An empty array is contained in anything, which matches the
    domain's convention that an empty allow-list restricts nothing.
    """
    members = ", ".join(f"'{member.value}'" for member in enum_class)
    return CheckConstraint(f"{column} <@ ARRAY[{members}]::text[]",
                           name=f"{column}_members")


def _code_array_format(column: str, pattern: str) -> CheckConstraint:
    """A `TEXT[]` whose every element matches `pattern`.

    `unnest` would need a subquery, which a CHECK may not contain, so the array is
    joined and the joined form is matched: a separator that cannot appear inside
    the pattern turns "every element matches" into one regular expression.
    `array_to_string` is IMMUTABLE, which is what makes it legal here.
    """
    return CheckConstraint(
        f"array_to_string({column}, ',') ~ '^({pattern}(,{pattern})*)?$'",
        name=f"{column}_format")


def _sha256_hex(column: str) -> CheckConstraint:
    """A stored token digest, constrained to the shape `SessionTokenDigest` accepts.

    The database repeating the domain's validator is the point: a code path that
    wrote a raw 43-character `token_urlsafe` value into a digest column would be
    storing a live credential in the clear, and this is the layer that would still
    refuse it if the write did not go through the model.
    """
    return CheckConstraint(f"{column} ~ '^[0-9a-f]{{64}}$'", name=f"{column}_format")


# `WorkloadRange` bounds, shared by `opportunities` (what the posting offers) and
# `search_profiles` (what the candidate wants). One pair of expressions for both,
# because the two column groups mean the same thing and a saved search whose
# workload the database accepted but an opportunity's would refuse would be a
# filter that can never match anything.
_WORKLOAD_PERCENT_RANGE: Final[str] = (
    "workload_min_percent BETWEEN 1 AND 100"
    " AND workload_max_percent BETWEEN 1 AND 100"
)

_WORKLOAD_HOURS_RANGE: Final[str] = (
    "workload_min_weekly_hours > 0 AND workload_min_weekly_hours <= 168"
    " AND workload_max_weekly_hours > 0 AND workload_max_weekly_hours <= 168"
)




class LocationColumnsMixin:
    """`Location` flattened, with one definition for both tables that embed it.

    The `location_` prefix is redundant on `company_locations` and load-bearing
    on `opportunities`, where a bare `raw` column would be confused with the
    source record's raw payload and a bare `country` with a future country of
    incorporation. One prefixed mixin, used twice, is worth the redundancy: the
    two tables cannot drift, and the geography column is declared identically in
    both — which is what makes a single radius query work over either.

    Every component is nullable because discovery is incremental: a posting that
    says only "Lausanne" stores `location_city` and nothing else, and the
    geocoding pass of Phase 7 fills `location_point` later.

    Phase 7 added the five `location_provenance`/`location_precision`/… columns.
    They describe the *coordinates*, not the place, which is why they sit outside
    `_LOCATION_COMPONENTS`: a row with a provenance and no components still
    locates nothing. Their job is to make the enrichment pass re-runnable — it can
    read that a point came from the employer itself and leave it alone (§7, §39) —
    and to stop a city centroid being drawn as a street address (§32).

    The two defaults say what an un-geocoded row means: the source provided
    whatever is there, and nothing claims a precision. They are server-side so
    that revision 0005 can backfill the existing rows with the same statement that
    adds the column, and so a row written by psql is as honest as one written here.
    """

    location_country: Mapped[str | None] = mapped_column(String(2))
    location_region: Mapped[str | None]
    location_city: Mapped[str | None]
    location_postal_code: Mapped[str | None]
    location_point: Mapped[GeoPoint | None]
    location_raw: Mapped[str | None]

    location_provenance: Mapped[LocationProvenance] = mapped_column(
        enum_column(LocationProvenance, "location_provenance"),
        default=LocationProvenance.SOURCE_PROVIDED,
        server_default=text(f"'{LocationProvenance.SOURCE_PROVIDED.value}'"))
    location_precision: Mapped[LocationPrecision] = mapped_column(
        enum_column(LocationPrecision, "location_precision"),
        default=LocationPrecision.UNKNOWN,
        server_default=text(f"'{LocationPrecision.UNKNOWN.value}'"))
    location_confidence: Mapped[GeocodingConfidence | None] = mapped_column(
        enum_column(GeocodingConfidence, "location_confidence"))
    location_geocoder: Mapped[str | None]
    location_geocoded_at: Mapped[datetime | None]


# A SHA-256 rendered as lower-case hex is always 64 characters, so the column is
# fixed-width by nature. `String(64)` rather than TEXT because the width is a real
# constraint here and stating it lets the database reject a wrong-length value
# without consulting the CHECK.
_DIGEST_LENGTH: Final[int] = 64


class UserRow(TimestampedMixin, Base):
    """One account: the login identifier, the credential, and the login counters.

    Phase 2 created this table with a display name and nothing else, so that
    user-owned rows could carry a real foreign key before anything could
    authenticate. Phase 4 fills it in.

    Two columns are worth explaining. `email` is unique *and* CHECKed to be its own
    normalized form: the unique index alone would let `Ada@x.com` and `ada@x.com`
    both register, since PostgreSQL compares TEXT case-sensitively, and the whole
    point of `normalize_email` in the domain is that one person has one account.
    Enforcing the normalized form here is what makes the index mean what it says.

    `password_hash` is TEXT with no width. Argon2id's encoded form is 97 characters
    today, and a parameter bump — which `check_needs_rehash` exists to make routine
    — changes that length. A `VARCHAR(97)` would turn the next cost increase into a
    migration.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email"),
        # Trimmed, lower-cased, and containing an `@` that is neither first nor
        # last. Not an attempt to validate an address — `EmailStr` does that with
        # the `email-validator` package — but enough that a row written by psql or
        # by a future backfill cannot be a login identifier nobody can ever match.
        CheckConstraint(
            "email = lower(btrim(email))"
            " AND position('@' in email) > 1"
            " AND position('@' in email) < length(email)",
            name="email_normalized"),
        CheckConstraint("failed_login_attempts >= 0",
                        name="failed_login_attempts_non_negative"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    email: Mapped[str]
    password_hash: Mapped[str]
    status: Mapped[UserStatus] = mapped_column(
        enum_column(UserStatus, "user_status"),
        server_default=text(f"'{UserStatus.ACTIVE.value}'"))
    display_name: Mapped[str | None]
    # NULL for every account in Phase 4: nothing sends mail yet. The column exists
    # so the authorization layer has somewhere truthful to look later, and it is
    # deliberately not defaulted to `now()` — a verified-at that nothing verified
    # is worse than a NULL.
    email_verified_at: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]
    failed_login_attempts: Mapped[int] = mapped_column(
        SmallInteger, server_default=text("0"))
    locked_until: Mapped[datetime | None]
    onboarding_completed_at: Mapped[datetime | None]

    sessions: Mapped[list["UserSessionRow"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="UserSessionRow.issued_at")


class UserSessionRow(TimestampedMixin, Base):
    """One authenticated browser, as a revocable server-side row.

    What is stored is a digest, never a token: the value the browser holds exists
    only in the response that set the cookie (docs/AUTHENTICATION.md §Sessions). So
    a leaked dump of this table is not a set of working credentials, and the CHECKs
    below are what keep it that way — a 43-character `token_urlsafe` value written
    here by mistake violates `token_digest_format` instead of persisting.

    There is no IP address and no user-agent column. Both are personal data with
    retention rules of their own, neither is used by any decision in this phase,
    and a session table is the easiest place to accumulate a request log nobody
    asked for (docs/ENGINEERING_STANDARDS.md §Security).

    `csrf_token_digest` lives here rather than in its own table because a CSRF
    token has exactly the lifetime of the session it protects. Keeping it beside
    the session is also what turns the double-submit cookie into a comparison
    against server-side state: a token planted in the cookie jar by a sibling host
    matches the cookie it was planted in, and nothing else.
    """

    __tablename__ = "user_sessions"
    __table_args__ = (
        # The lookup every authenticated request performs, so it is the constraint
        # that also serves as its index. Unique because two sessions sharing a
        # token digest would be two browsers holding one credential.
        UniqueConstraint("token_digest"),
        _sha256_hex("token_digest"),
        _sha256_hex("csrf_token_digest"),
        # `UserSession._the_two_digests_differ`: issuing one secret twice would
        # hand the session token to any script that can read the CSRF cookie.
        CheckConstraint("token_digest <> csrf_token_digest",
                        name="digests_are_independent"),
        CheckConstraint("expires_at > issued_at", name="window_is_forward"),
        CheckConstraint("last_seen_at >= issued_at", name="last_seen_after_issued"),
        # "Revoke my other sessions" and the expiry sweep both read by user; the
        # sweep also reads by expiry, hence the second column.
        Index("ix_user_sessions_user_id_expires_at", "user_id", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    token_digest: Mapped[str] = mapped_column(String(_DIGEST_LENGTH))
    csrf_token_digest: Mapped[str] = mapped_column(String(_DIGEST_LENGTH))
    issued_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    # NULL means live. A `revoked` boolean would lose *when*, which is the column a
    # security question ("was this session active at 14:05?") actually needs.
    revoked_at: Mapped[datetime | None]

    user: Mapped["UserRow"] = relationship(back_populates="sessions", lazy="raise")



class CandidateProfileRow(LocationColumnsMixin, TimestampedMixin, Base):
    """What the platform knows about one candidate, minus the evidence store.

    Separate from `users` because one person legitimately searches under more than
    one profile (a student job and a graduate role weigh education differently),
    and an evaluation is against a profile, not against an account. Onboarding
    creates one profile per account and derives its id from the account's, so a
    double-submitted onboarding collides on the primary key instead of producing
    two profiles; a second profile would be an explicit act with a fresh id.

    `evidence` and `claims` are absent, and their absence is enforced rather than
    tolerated: the evidence store is Phase 10's, and
    `SqlAlchemyCandidateProfileRepository` refuses a profile carrying either
    instead of silently dropping records a claim depends on.

    Availability is flattened into five columns plus a child table for the weekly
    slots, and all five are nullable: an all-NULL group with no slots reads back as
    `None`, the same convention `_read_location` follows. `Availability()` with
    nothing set is therefore indistinguishable from "not stated", which is what it
    means anyway.
    """

    __tablename__ = "candidate_profiles"
    __table_args__ = (
        _code_format("location_country", "^[A-Z]{2}$"),
        _location_provenance_coherent(),
        CheckConstraint(
            "availability_earliest_start <= availability_latest_end",
            name="availability_window_ordered"),
        CheckConstraint(
            "availability_min_weekly_hours <= availability_max_weekly_hours",
            name="availability_hours_ordered"),
        CheckConstraint(
            "availability_min_weekly_hours BETWEEN 0 AND 168"
            " AND availability_max_weekly_hours > 0"
            " AND availability_max_weekly_hours <= 168",
            name="availability_hours_range"),
        CheckConstraint("availability_notice_period_days >= 0",
                        name="availability_notice_non_negative"),
        Index("ix_candidate_profiles_user_id", "user_id"),
        # The profile's own coordinates, so Phase 7 can answer "how far is this
        # posting from home?" with the same index type the postings use.
        Index("ix_candidate_profiles_location_point", "location_point",
              postgresql_using="gist"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    display_name: Mapped[str]
    headline: Mapped[str | None]

    availability_earliest_start: Mapped[date | None]
    availability_latest_end: Mapped[date | None]
    availability_min_weekly_hours: Mapped[float | None]
    availability_max_weekly_hours: Mapped[float | None]
    availability_notice_period_days: Mapped[int | None] = mapped_column(SmallInteger)

    languages: Mapped[list["CandidateLanguageRow"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="CandidateLanguageRow.ordinal")
    work_authorizations: Mapped[list["CandidateWorkAuthorizationRow"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="CandidateWorkAuthorizationRow.ordinal")
    availability_slots: Mapped[list["CandidateAvailabilitySlotRow"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="CandidateAvailabilitySlotRow.ordinal")


# The domain's collections are tuples, and tuple order is information the candidate
# supplied — the first language listed is the one they lead with. A child table has
# no inherent order, so every one of them carries an explicit `ordinal` and every
# relationship sorts by it. Without that, a profile read back would be equal to the
# one written only by luck, and the round-trip tests would be asserting nothing.
_ORDINAL_DOC: Final[str] = "position in the domain tuple, so order survives a round trip"

# The three child tables below say `profile_id`, not `candidate_profile_id` like
# `match_evaluations` does. The qualifier is redundant inside a table already named
# `candidate_…`, and it is also unaffordable: the naming convention derives
# `fk_candidate_work_authorizations_candidate_profile_id_candidate_profiles`, which
# is 72 characters, and PostgreSQL truncates at 63 — a silently truncated name with
# a hash suffix is one a migration cannot reliably drop.
_PROFILE_FK: Final[str] = "candidate_profiles.id"


class CandidateLanguageRow(TimestampedMixin, Base):
    """One language the candidate speaks, at one CEFR level.

    A child table rather than JSONB because docs/V2_SPECIFICATION.md §9 asks for
    "candidates who read German at B2 or better", which is a comparison across
    rows, and because `LanguageLevel` is an enum the database can then police.
    """

    __tablename__ = "candidate_languages"
    __table_args__ = (
        # `CandidateProfile._one_entry_per_language_and_country`, as a constraint.
        # It also indexes the foreign key, which is why there is no separate index.
        UniqueConstraint("profile_id", "language"),
        _code_format("language", "^[a-z]{2}$"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey(_PROFILE_FK, ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(SmallInteger, doc=_ORDINAL_DOC)
    language: Mapped[str] = mapped_column(String(2))
    level: Mapped[LanguageLevel] = mapped_column(
        enum_column(LanguageLevel, "language_level"))

    profile: Mapped["CandidateProfileRow"] = relationship(
        back_populates="languages", lazy="raise")


class CandidateWorkAuthorizationRow(TimestampedMixin, Base):
    """The candidate's right to work in one country.

    `permit_hours_cap` is the column that makes a class of eligibility
    deterministic: a student permit capped at 15h/week makes a 20h/week job
    *ineligible* rather than a poor schedule fit. It is a legal fact a Country Pack
    supplies in Phase 5; this table only carries it.

    `evidence_ids` from the domain object is not stored. Phase 10 owns the evidence
    table, and a column holding ids with no table to point at would be a foreign key
    that cannot be declared.
    """

    __tablename__ = "candidate_work_authorizations"
    __table_args__ = (
        UniqueConstraint("profile_id", "country"),
        _code_format("country", "^[A-Z]{2}$"),
        CheckConstraint("permit_hours_cap > 0 AND permit_hours_cap <= 168",
                        name="permit_hours_cap_range"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey(_PROFILE_FK, ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(SmallInteger, doc=_ORDINAL_DOC)
    country: Mapped[str] = mapped_column(String(2))
    status: Mapped[WorkAuthorizationStatus] = mapped_column(
        enum_column(WorkAuthorizationStatus, "work_authorization_status"))
    permit_label: Mapped[str | None]
    valid_until: Mapped[date | None]
    permit_hours_cap: Mapped[float | None]

    profile: Mapped["CandidateProfileRow"] = relationship(
        back_populates="work_authorizations", lazy="raise")


class CandidateAvailabilitySlotRow(TimestampedMixin, Base):
    """A recurring window the candidate can work, in whole local hours.

    No timezone, matching `WeeklyAvailabilitySlot`: "Saturday mornings" means it in
    the shop's local time, and storing an instant would invent precision nobody
    supplied.

    Overlap between two slots on the same day is a domain invariant that no CHECK
    can express — a row constraint cannot see another row, and an exclusion
    constraint would need a range type the domain does not use. The exact-duplicate
    case *is* expressible, and is refused.
    """

    __tablename__ = "candidate_availability_slots"
    __table_args__ = (
        UniqueConstraint("profile_id", "weekday", "start_hour"),
        CheckConstraint("start_hour BETWEEN 0 AND 23 AND end_hour BETWEEN 1 AND 24",
                        name="slot_hours_range"),
        CheckConstraint("start_hour < end_hour", name="slot_hours_ordered"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey(_PROFILE_FK, ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(SmallInteger, doc=_ORDINAL_DOC)
    weekday: Mapped[Weekday] = mapped_column(enum_column(Weekday, "weekday"))
    start_hour: Mapped[int] = mapped_column(SmallInteger)
    end_hour: Mapped[int] = mapped_column(SmallInteger)

    profile: Mapped["CandidateProfileRow"] = relationship(
        back_populates="availability_slots", lazy="raise")



class SearchProfileRow(TimestampedMixin, Base):
    """One saved search belonging to one user.

    This is what replaces V1's `config/searches.yaml`, which is a single-user file
    of free-text `locations` and a keyword blacklist. Two differences carry the
    phase: the areas are a child table with real geometry rather than strings, and
    the filters are allow-lists of typed values rather than excluded words — V1
    excludes "stage" and "apprenti", which are exactly the opportunity types
    docs/V2_SPECIFICATION.md §7 makes first-class.

    **An empty array means "no restriction", not "match nothing".** The domain
    states the convention once and every filter here inherits it, which is why the
    server defaults are `'{}'` rather than NULL: a nullable array would give the
    same intent two representations, and the first query written with `= ANY` on
    the NULL one would silently match nothing.
    """

    __tablename__ = "search_profiles"
    __table_args__ = (
        CheckConstraint("workload_min_percent <= workload_max_percent",
                        name="workload_percent_ordered"),
        CheckConstraint("workload_min_weekly_hours <= workload_max_weekly_hours",
                        name="workload_hours_ordered"),
        CheckConstraint(_WORKLOAD_PERCENT_RANGE, name="workload_percent_range"),
        CheckConstraint(_WORKLOAD_HOURS_RANGE, name="workload_hours_range"),
        _text_array_elements_present("queries"),
        _text_array_elements_present("title_keywords"),
        _text_array_elements_present("excluded_keywords"),
        _text_array_elements_present("source_keys"),
        _enum_array_members("opportunity_types", OpportunityType),
        _enum_array_members("contract_types", ContractType),
        _enum_array_members("workplace_modes", WorkplaceMode),
        _code_array_format("posting_languages", "[a-z]{2}"),
        # Discovery reads "every active search", and the account page reads "my
        # searches". One index serves both, because a partial index on `is_active`
        # could not answer the second.
        Index("ix_search_profiles_user_id_is_active", "user_id", "is_active"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str]
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))

    queries: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    title_keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    excluded_keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    opportunity_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    contract_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    workplace_modes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    posting_languages: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)
    source_keys: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=_EMPTY_TEXT_ARRAY)

    workload_min_percent: Mapped[int | None] = mapped_column(SmallInteger)
    workload_max_percent: Mapped[int | None] = mapped_column(SmallInteger)
    workload_min_weekly_hours: Mapped[float | None]
    workload_max_weekly_hours: Mapped[float | None]

    areas: Mapped[list["SearchAreaRow"]] = relationship(
        back_populates="search_profile", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise", order_by="SearchAreaRow.ordinal")


# `SearchArea` is a discriminated union of three shapes, and a single table with
# nullable columns is the flattening every ORM reaches for. What makes it safe here
# is that the union's exhaustiveness is written down as a constraint: each kind
# names exactly which columns must be present and which must be absent, so a row
# cannot be a RADIUS area with no centre or a COUNTRY area with a stray radius.
# Without this, the discriminator would be a label and the coherence the domain
# guarantees would be lost at the first `INSERT` that did not go through Pydantic.
_AREA_SHAPE_MATCHES_KIND: Final[str] = (
    f"(kind = '{SearchAreaKind.COUNTRY.value}'"
    " AND country IS NOT NULL AND center IS NULL AND radius_km IS NULL)"
    f" OR (kind = '{SearchAreaKind.RADIUS.value}'"
    " AND country IS NULL AND center IS NOT NULL AND radius_km IS NOT NULL)"
    f" OR (kind = '{SearchAreaKind.REMOTE_ONLY.value}'"
    " AND center IS NULL AND radius_km IS NULL)"
)


class SearchAreaRow(TimestampedMixin, Base):
    """Where one saved search looks: a country, a radius, or nowhere in particular.

    `ordinal` is the natural key rather than a convenience: two radius areas can
    differ only by their radius, so there is nothing else to identify a row by, and
    `UNIQUE (search_profile_id, ordinal)` is what makes re-saving a search an update
    of the same rows instead of a delete-and-reinsert.
    """

    __tablename__ = "search_areas"
    __table_args__ = (
        UniqueConstraint("search_profile_id", "ordinal"),
        CheckConstraint(_AREA_SHAPE_MATCHES_KIND, name="shape_matches_kind"),
        CheckConstraint("radius_km > 0 AND radius_km <= 500", name="radius_km_range"),
        _code_format("country", "^[A-Z]{2}$"),
        # The same GiST index the opportunities carry, so Phase 7's `ST_DWithin`
        # can start from either end of the question.
        Index("ix_search_areas_center", "center", postgresql_using="gist"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    search_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("search_profiles.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(SmallInteger, doc=_ORDINAL_DOC)
    kind: Mapped[SearchAreaKind] = mapped_column(
        enum_column(SearchAreaKind, "search_area_kind"))
    country: Mapped[str | None] = mapped_column(String(2))
    center: Mapped[GeoPoint | None]
    radius_km: Mapped[float | None]
    label: Mapped[str | None]

    search_profile: Mapped["SearchProfileRow"] = relationship(
        back_populates="areas", lazy="raise")






# A provenance key — a source or provider key — as `ProvenanceKey` validates it:
# `^[a-z][a-z0-9_]*$`, at most 40 characters. Width stated rather than left to TEXT
# because the domain states it, and a 200-character value in this column would be a
# bug somewhere upstream rather than a long name.
_PROVENANCE_KEY_LENGTH: Final[int] = 40

# `DetectedATS` is all-or-nothing when present, exactly as the value object is: a
# status, a detector and evidence only mean something next to a platform, and §10's
# rule that a `CONFIRMED` detection must name the organization identifier is the
# second clause. Without this the flattening would allow a status asserting
# confidence about a platform nobody detected.
_ATS_COMPLETE_OR_ABSENT: Final[str] = (
    "(ats_platform IS NULL AND ats_organization_id IS NULL"
    " AND ats_status IS NULL AND ats_detected_by IS NULL"
    " AND ats_evidence = '[]'::jsonb)"
    " OR (ats_platform IS NOT NULL AND ats_status IS NOT NULL"
    " AND ats_detected_by IS NOT NULL AND ats_evidence <> '[]'::jsonb"
    f" AND (ats_status <> '{DetectionStatus.CONFIRMED.value}'"
    " OR ats_organization_id IS NOT NULL))"
)

# `Company._the_channel_and_the_flag_agree` and
# `SpontaneousApplicationChannel._a_verdict_shows_its_work`, as one expression: the
# boolean the API exposes and the evidence-backed verdict §12 asks for have to give
# the same answer, a decided verdict has to carry evidence and an observer, and
# NOT_SUPPORTED with a URL contradicts itself.
_SPONTANEOUS_SUPPORT_MATCHES_FLAG: Final[str] = (
    "(spontaneous_support IS NULL"
    " AND spontaneous_url IS NULL AND spontaneous_observed_by IS NULL"
    " AND spontaneous_evidence = '[]'::jsonb)"
    f" OR (spontaneous_support = '{SpontaneousApplicationSupport.UNKNOWN.value}'"
    " AND accepts_spontaneous_applications IS NULL)"
    f" OR (spontaneous_support = '{SpontaneousApplicationSupport.SUPPORTED.value}'"
    " AND accepts_spontaneous_applications IS TRUE"
    " AND spontaneous_observed_by IS NOT NULL"
    " AND spontaneous_evidence <> '[]'::jsonb)"
    f" OR (spontaneous_support = '{SpontaneousApplicationSupport.NOT_SUPPORTED.value}'"
    " AND accepts_spontaneous_applications IS FALSE"
    " AND spontaneous_url IS NULL AND spontaneous_observed_by IS NOT NULL"
    " AND spontaneous_evidence <> '[]'::jsonb)"
)


class CompanyRow(TimestampedMixin, Base):
    """A canonical employer.

    `name` is indexed but not unique, and `normalized_name` is indexed and not
    unique either. Two legally distinct companies share a name often enough that a
    unique constraint would reject real data — and, more to the point, deciding that
    "Migros", "Migros SA" and "MIGROS Vaud" are one employer is evidence-based work
    the database cannot do: §2 forbids merging on name similarity, so a unique index
    on the normalized form would be the database silently making exactly the decision
    `backend.app.companies.resolution` refuses to make. Identity comes from the
    primary key, and `normalized_name` is what narrows the shortlist that `resolve`
    then judges.

    Phase 6 added thirteen columns and every one of them is read off the domain
    object rather than computed here. `normalized_name` and `normalized_domain` are
    projections of `Company.normalized_name`/`normalized_domain` — properties, not
    fields, so the column cannot disagree with the name beside it — and the CHECK
    that each equals its own lower-cased form is what keeps a hand-written `INSERT`
    from putting a display name in the comparison column.

    `accepts_spontaneous_applications` is nullable because the domain models it as
    three-valued — NULL is "nobody has looked", which an application strategy must
    not read as "no" — and `spontaneous_*` beside it is the evidence §12 requires
    for a decided answer. `ck_companies_spontaneous_support_matches_flag` is
    `Company._the_channel_and_the_flag_agree` as a constraint: the two
    representations exist because Phase 1's boolean is what the API exposes, and a
    row where they contradict each other would make the answer depend on which
    column the reader picked.

    The ATS columns are `DetectedATS` flattened, with the same all-or-nothing rule
    the value object has: no platform means no status, no evidence and no
    organization, and a `CONFIRMED` detection must name the organization or nothing
    can fetch the board.
    """

    __tablename__ = "companies"
    __table_args__ = (
        # `Company._a_name_has_a_comparison_form`, and the reason the column is NOT
        # NULL: a company whose name normalizes to nothing can never be compared
        # against anything, so it is not a usable identity.
        CheckConstraint("normalized_name = lower(normalized_name)"
                        " AND normalized_name <> ''",
                        name="normalized_name_is_comparison_form"),
        CheckConstraint("normalized_domain = lower(normalized_domain)"
                        " AND normalized_domain <> ''",
                        name="normalized_domain_is_comparison_form"),
        _code_format("country", "^[A-Z]{2}$"),
        CheckConstraint(_ATS_COMPLETE_OR_ABSENT, name="ats_complete_or_absent"),
        CheckConstraint(_SPONTANEOUS_SUPPORT_MATCHES_FLAG,
                        name="spontaneous_support_matches_flag"),
        Index("ix_companies_name", "name"),
        # The two shortlist lookups `resolution.name_lookup_keys` and
        # `strong_lookup_keys` drive. Without them every resolution is a sequential
        # scan of the whole employer table, which is fine on an empty development
        # database and is the outage §23's repeated passes would cause on a real one.
        Index("ix_companies_normalized_name", "normalized_name"),
        Index("ix_companies_normalized_domain", "normalized_domain"),
        # The ATS organization as an identity: unique per platform, because
        # `boards.greenhouse.io/acme` is one employer's board and two companies
        # claiming it is the duplicate `resolve` reports as AMBIGUOUS. Partial, so
        # the thousands of companies with no detected ATS do not collide on NULL.
        Index("uq_companies_ats_platform_organization_id",
              "ats_platform", "ats_organization_id", unique=True,
              postgresql_where=text("ats_organization_id IS NOT NULL")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str]
    # A projection of `Company.normalized_name`. NOT NULL: every company has one by
    # construction, and a nullable column would invite a query that misses rows.
    normalized_name: Mapped[str]
    website: Mapped[str | None]
    careers_url: Mapped[str | None]
    # A projection of `Company.normalized_domain`, which falls back to the careers
    # host when there is no website. Nullable because plenty of employers arrive
    # with neither.
    normalized_domain: Mapped[str | None]
    country: Mapped[str | None] = mapped_column(String(2))
    identity_status: Mapped[CompanyIdentityStatus] = mapped_column(
        enum_column(CompanyIdentityStatus, "company_identity_status"),
        server_default=text(f"'{CompanyIdentityStatus.SEEDED.value}'"))
    ats_platform: Mapped[AtsPlatform | None] = mapped_column(
        enum_column(AtsPlatform, "ats_platform"))
    ats_organization_id: Mapped[str | None]
    ats_status: Mapped[DetectionStatus | None] = mapped_column(
        enum_column(DetectionStatus, "ats_detection_status"))
    ats_detected_by: Mapped[str | None] = mapped_column(
        String(_PROVENANCE_KEY_LENGTH))
    # `tuple[Evidence, ...]` as JSONB, the same shape `reasons_to_json` writes for a
    # match evaluation. A child table would be the third provenance table in this
    # phase for a collection that is never queried by its elements — §15 asks for the
    # smallest normalized schema, and evidence is read whole with its detection.
    ats_evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=_EMPTY_JSON_ARRAY)
    accepts_spontaneous_applications: Mapped[bool | None]
    spontaneous_support: Mapped[SpontaneousApplicationSupport | None] = mapped_column(
        enum_column(SpontaneousApplicationSupport, "spontaneous_application_support"))
    spontaneous_url: Mapped[str | None]
    spontaneous_observed_by: Mapped[str | None] = mapped_column(
        String(_PROVENANCE_KEY_LENGTH))
    spontaneous_evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=_EMPTY_JSON_ARRAY)

    locations: Mapped[list["CompanyLocationRow"]] = relationship(
        back_populates="company", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        # Deterministic order, so a company read twice produces an equal domain
        # object and a round-trip test can compare tuples directly.
        order_by="CompanyLocationRow.id")


class CompanyAliasRow(TimestampedMixin, Base):
    """Another label the same employer is published under (§4).

    Its own table because an alias has provenance, and `UNIQUE (company_id,
    normalized_alias)` is what makes §23's repeated pass an update rather than an
    insert: the second sighting of `LOGITECH` moves `last_seen_at` and leaves
    `first_seen_at` alone. The primary key is derived (`company_alias_id`, uuid5 over
    the same pair), so the upsert can be written without a prior SELECT and the
    unique constraint is the backstop rather than the mechanism.

    `alias` keeps the spelling the source used and `normalized_alias` is the
    comparison form. Both, because §4's rule is that another source's label must not
    overwrite the canonical name — showing an operator `Logitech Europe S.A.` is only
    possible if the original spelling survived.
    """

    __tablename__ = "company_aliases"
    __table_args__ = (
        UniqueConstraint("company_id", "normalized_alias"),
        CheckConstraint("normalized_alias = lower(normalized_alias)"
                        " AND normalized_alias <> ''",
                        name="normalized_alias_is_comparison_form"),
        CheckConstraint("last_seen_at >= first_seen_at", name="seen_window_ordered"),
        Index("ix_company_aliases_normalized_alias", "normalized_alias"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"))
    alias: Mapped[str]
    normalized_alias: Mapped[str]
    source_key: Mapped[str] = mapped_column(String(_PROVENANCE_KEY_LENGTH))
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]


class CompanyCareerSiteRow(TimestampedMixin, Base):
    """One careers endpoint of one company (§11).

    Several per company is the normal case — a corporate page, an ATS board, a
    spontaneous-application form — which is why §11 asks for records instead of one
    `careers_url` column. `Company.careers_url` survives as the *preferred* endpoint,
    which §11 explicitly permits.

    `UNIQUE (company_id, url)` with a derived primary key, for the same idempotence
    reason as the aliases: rediscovering the same board updates one row.

    `last_checked_at` is nullable and Phase 6 never sets it: nothing in this phase
    fetches a URL, so a timestamp here would claim a check that never happened.
    """

    __tablename__ = "company_career_sites"
    __table_args__ = (
        UniqueConstraint("company_id", "url"),
        # `CareerSite._an_ats_board_names_its_platform`: a board nothing can identify
        # the platform of is a board no source plugin can read.
        CheckConstraint(
            f"kind <> '{CareerSiteKind.ATS_BOARD.value}' OR platform IS NOT NULL",
            name="ats_board_names_its_platform"),
        CheckConstraint("last_checked_at IS NULL OR last_checked_at >= discovered_at",
                        name="checked_after_discovered"),
        Index("ix_company_career_sites_company_id", "company_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"))
    url: Mapped[str]
    kind: Mapped[CareerSiteKind] = mapped_column(
        enum_column(CareerSiteKind, "career_site_kind"))
    platform: Mapped[AtsPlatform | None] = mapped_column(
        enum_column(AtsPlatform, "career_site_platform"))
    source_key: Mapped[str] = mapped_column(String(_PROVENANCE_KEY_LENGTH))
    verification_status: Mapped[DetectionStatus] = mapped_column(
        enum_column(DetectionStatus, "career_site_verification_status"),
        server_default=text(f"'{DetectionStatus.LIKELY.value}'"))
    discovered_at: Mapped[datetime]
    last_checked_at: Mapped[datetime | None]


class CompanyDiscoveryRecordRow(TimestampedMixin, Base):
    """How one provider came to tell us about one company (§5).

    `UNIQUE (provider_key, external_id)` is §23's external identity uniqueness: the
    same provider reporting the same employer twice is one sighting, updated. The
    primary key is `company_discovery_record_id`, uuid5 over exactly that pair, so
    the constraint and the key say the same thing and neither can drift.

    `company_id` is nullable and the foreign key is `ON DELETE SET NULL`. Both matter:
    §14 leaves an `AMBIGUOUS` seed unlinked rather than guessing, and a sighting we
    keep is what stops the next pass rediscovering and re-refusing it; and merging two
    duplicate employers must not delete the provenance that revealed the duplication.

    `raw` is JSONB and the domain refuses credential-shaped keys before it ever gets
    here (`CompanyDiscoveryRecord._raw_carries_no_secrets`, §5, §26). No CHECK
    mirrors that: the forbidden set is a substring list that will grow, and a CHECK
    over JSONB keys would be a second copy of it that silently disagrees.
    """

    __tablename__ = "company_discovery_records"
    __table_args__ = (
        UniqueConstraint("provider_key", "external_id"),
        Index("ix_company_discovery_records_company_id", "company_id"),
        # "What did this provider find, most recent first" — the operator's question
        # on a provenance screen.
        Index("ix_company_discovery_records_provider_key_discovered_at",
              "provider_key", "discovered_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(_PROVENANCE_KEY_LENGTH))
    external_id: Mapped[str]
    seed_kind: Mapped[CompanySeedKind] = mapped_column(
        enum_column(CompanySeedKind, "company_seed_kind"))
    company_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"))
    company_name: Mapped[str]
    source_url: Mapped[str | None]
    discovered_at: Mapped[datetime]
    confidence: Mapped[DetectionStatus] = mapped_column(
        enum_column(DetectionStatus, "discovery_record_confidence"),
        server_default=text(f"'{DetectionStatus.LIKELY.value}'"))
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=_EMPTY_JSON_OBJECT)


class CompanyLocationRow(LocationColumnsMixin, TimestampedMixin, Base):
    """One physical site of a company — a marker on the Phase 8 map, once the
    Phase 7 geocoding pass gives it coordinates.

    Unlike an opportunity's location, this one is required to say something:
    `Location` is non-optional on the domain entity, so `location_not_empty`
    mirrors `Location._must_locate_something` as a CHECK. A row with six NULLs
    would be a site nobody can find.
    """

    __tablename__ = "company_locations"
    __table_args__ = (
        CheckConstraint(
            " OR ".join(f"{column} IS NOT NULL" for column in _LOCATION_COMPONENTS),
            name="location_not_empty"),
        _code_format("location_country", "^[A-Z]{2}$"),
        _location_provenance_coherent(),
        Index("ix_company_locations_company_id", "company_id"),
        # GiST is what makes `ST_DWithin` an index lookup instead of a scan of
        # every site in the country.
        Index("ix_company_locations_location_point", "location_point",
              postgresql_using="gist"),
        # §13's whole-country branch, which is not a spatial predicate and which
        # GiST therefore does not serve: `location_country = 'CH'`, against the
        # canonical code rather than a substring of a display string.
        Index("ix_company_locations_location_country", "location_country"),
        # `Company._locations_belong_here` allows at most one headquarters. A
        # partial unique index says the same thing to the database, and costs
        # nothing on the rows that are not headquarters.
        Index("uq_company_locations_company_id_headquarters", "company_id",
              unique=True, postgresql_where=text("is_headquarters")),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"))
    is_headquarters: Mapped[bool] = mapped_column(server_default=text("false"))

    company: Mapped["CompanyRow"] = relationship(back_populates="locations",
                                                 lazy="raise")


# `SalaryRange` is optional on an opportunity but is all-or-nothing when present:
# a currency without a bound cannot be displayed, and a bound without a currency
# cannot be compared. The domain expresses this by making the value object
# indivisible; a flattened column group needs it spelled out.
_SALARY_COMPLETE_OR_ABSENT: Final[str] = (
    "(salary_currency IS NULL AND salary_period IS NULL"
    " AND salary_minimum IS NULL AND salary_maximum IS NULL)"
    " OR (salary_currency IS NOT NULL AND salary_period IS NOT NULL"
    " AND (salary_minimum IS NOT NULL OR salary_maximum IS NOT NULL))"
)


class OpportunityRow(LocationColumnsMixin, TimestampedMixin, Base):
    """A normalized opportunity — the central V2 table.

    No `user_id`: a posting is a shared fact, and what a particular candidate
    thinks of it lives in `match_evaluations`. This is the decision
    docs/ARCHITECTURE.md §5 makes, and it is why two candidates searching the
    same market do not store the same posting twice.

    `company_name` sits beside a nullable `company_id` because that is the order
    the facts arrive in: discovery has a string, and Phase 6 resolves it to a
    `companies` row without overwriting what the posting claimed. The foreign key
    is `ON DELETE SET NULL` for the same reason — merging two duplicate company
    records must not delete their postings.
    """

    __tablename__ = "opportunities"
    __table_args__ = (
        CheckConstraint("workload_min_percent <= workload_max_percent",
                        name="workload_percent_ordered"),
        CheckConstraint("workload_min_weekly_hours <= workload_max_weekly_hours",
                        name="workload_hours_ordered"),
        CheckConstraint(_WORKLOAD_PERCENT_RANGE, name="workload_percent_range"),
        CheckConstraint(_WORKLOAD_HOURS_RANGE, name="workload_hours_range"),
        CheckConstraint(_SALARY_COMPLETE_OR_ABSENT, name="salary_complete_or_absent"),
        CheckConstraint("salary_minimum <= salary_maximum",
                        name="salary_bounds_ordered"),
        CheckConstraint("salary_minimum >= 0 AND salary_maximum >= 0",
                        name="salary_non_negative"),
        _code_format("location_country", "^[A-Z]{2}$"),
        _code_format("posting_language", "^[a-z]{2}$"),
        _code_format("salary_currency", "^[A-Z]{3}$"),
        _location_provenance_coherent(),
        Index("ix_opportunities_company_id", "company_id"),
        # The feed is "what is new", so this is the ordering every list query
        # uses. PostgreSQL does not index a foreign key or a sort column for you.
        Index("ix_opportunities_discovered_at", "discovered_at"),
        Index("ix_opportunities_location_point", "location_point",
              postgresql_using="gist"),
        # §13's whole-country branch — see `ix_company_locations_location_country`.
        Index("ix_opportunities_location_country", "location_country"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    company_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"))
    company_name: Mapped[str]
    title: Mapped[str]
    description: Mapped[str | None]

    opportunity_type: Mapped[OpportunityType | None] = mapped_column(
        enum_column(OpportunityType, "opportunity_type"))
    contract_type: Mapped[ContractType | None] = mapped_column(
        enum_column(ContractType, "contract_type"))
    workplace_mode: Mapped[WorkplaceMode | None] = mapped_column(
        enum_column(WorkplaceMode, "workplace_mode"))

    # Both workload scales are stored as the source gave them. Converting a
    # percentage into hours needs the local full-time week, which is Country Pack
    # knowledge (Phase 5); a column that guessed it would be wrong in one country
    # out of two and unfixable afterwards.
    workload_min_percent: Mapped[int | None] = mapped_column(SmallInteger)
    workload_max_percent: Mapped[int | None] = mapped_column(SmallInteger)
    workload_min_weekly_hours: Mapped[float | None]
    workload_max_weekly_hours: Mapped[float | None]

    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_period: Mapped[SalaryPeriod | None] = mapped_column(
        enum_column(SalaryPeriod, "salary_period"))
    salary_minimum: Mapped[Decimal | None] = mapped_column(_MONEY)
    salary_maximum: Mapped[Decimal | None] = mapped_column(_MONEY)

    posting_language: Mapped[str | None] = mapped_column(String(2))
    # A list of `LanguageRequirement` objects. JSONB rather than a child table:
    # the requirements are always read with the opportunity, never queried on
    # their own in Phase 2, and a `?` containment index can be added later without
    # a schema change if Phase 9 needs "postings that require German".
    language_requirements: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)

    posted_at: Mapped[date | None]
    discovered_at: Mapped[datetime]
    application_url: Mapped[str | None]
    # V1's `dedup_hash`, carried through unchanged. Unique so a second import of
    # the same posting collides instead of duplicating — and nullable, because
    # PostgreSQL treats NULLs as distinct, so postings whose source never provided
    # a fingerprint coexist freely.
    dedup_fingerprint: Mapped[str | None] = mapped_column(unique=True)

    source: Mapped["OpportunitySourceRecordRow"] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise", uselist=False)


class OpportunitySourceRecordRow(TimestampedMixin, Base):
    """Where an opportunity came from, and what the source literally said.

    Its own table, not a column group, for two reasons the specification gives:
    provenance is an entity in docs/ARCHITECTURE.md §3, and `raw` is an unbounded
    JSONB payload that would be read on every opportunity list query if it lived
    on the same row.

    `UNIQUE (source_key, external_id)` is the import idempotency key
    (docs/ENGINEERING_STANDARDS.md §Database rules): running the V1 import twice
    conflicts on the second run instead of inserting a second copy. NULL
    `external_id` values do not conflict with each other in PostgreSQL, which is
    the behaviour wanted here — a source that publishes no stable id cannot claim
    two postings are the same.

    `UNIQUE (opportunity_id)` holds the one-to-one the domain models today. It is
    the constraint to drop, not a table to create, if a posting later needs to be
    traceable to several boards.
    """

    __tablename__ = "opportunity_source_records"
    __table_args__ = (
        # Unnamed on purpose: the `uq` naming convention derives
        # `uq_opportunity_source_records_source_key_external_id` from the columns,
        # so the name in a migration is predictable from the definition here.
        UniqueConstraint("opportunity_id"),
        UniqueConstraint("source_key", "external_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    opportunity_id: Mapped[UUID] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"))
    source_key: Mapped[str]
    external_id: Mapped[str | None]
    source_url: Mapped[str | None]
    fetched_at: Mapped[datetime]
    # Public posting metadata only. Credentials, cookies and session tokens must
    # never reach this column (docs/ENGINEERING_STANDARDS.md §Security); the
    # domain model carries the same warning, and the importer only ever copies
    # V1's own job columns into it.
    raw: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT)

    opportunity: Mapped["OpportunityRow"] = relationship(
        back_populates="source", lazy="raise")


class MatchEvaluationRow(TimestampedMixin, Base):
    """How one candidate fits one opportunity — the user-scoped half of the pair.

    `user_id` is stored even though it is reachable through
    `candidate_profiles.user_id`. The duplication is deliberate: every
    authorization filter in Phase 4 and every list query is `WHERE user_id = ?`,
    and a policy that needs a join to know who owns a row is a policy that will
    eventually be written without the join. The foreign keys cascade, so a deleted
    account leaves nothing behind.

    `overall` is stored rather than recomputed from the dimension rows, matching
    `MatchEvaluation`: a matcher may apply penalties the mean does not express,
    and a database view that recomputed it would quietly disagree with the engine.

    `UNIQUE (candidate_profile_id, opportunity_id)` makes re-evaluation an upsert.
    """

    __tablename__ = "match_evaluations"
    __table_args__ = (
        UniqueConstraint("candidate_profile_id", "opportunity_id"),
        _unit_interval("overall"),
        _unit_interval("evidence_confidence"),
        # The shape of every authorization-scoped read: this user's evaluations,
        # newest first.
        Index("ix_match_evaluations_user_id_evaluated_at", "user_id", "evaluated_at"),
        Index("ix_match_evaluations_opportunity_id", "opportunity_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    candidate_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_profiles.id", ondelete="CASCADE"))
    opportunity_id: Mapped[UUID] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"))
    overall: Mapped[float]
    evidence_confidence: Mapped[float | None]
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    evaluator_key: Mapped[str | None]
    evaluated_at: Mapped[datetime]

    dimensions: Mapped[list["MatchDimensionScoreRow"]] = relationship(
        back_populates="evaluation", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        order_by="MatchDimensionScoreRow.dimension")


class MatchDimensionScoreRow(TimestampedMixin, Base):
    """One axis of one evaluation, with the reasons behind it.

    A table rather than a JSONB array on the evaluation, unlike
    `reasons`: docs/V2_SPECIFICATION.md §9 asks which axis is weak across a whole
    market, and "average LOCATION_FIT for this user's evaluations" is an
    aggregate, which is what rows are for. The `UNIQUE (match_evaluation_id,
    dimension)` pair enforces `MatchEvaluation._one_score_per_dimension`.

    A `weight` of exactly 0 is legal and means "computed, deliberately ignored" —
    different from the absence of a row, which means "not computed". Only the
    domain's `weighted_dimension_mean()` interprets that difference.
    """

    __tablename__ = "match_dimension_scores"
    __table_args__ = (
        UniqueConstraint("match_evaluation_id", "dimension"),
        _unit_interval("score"),
        _unit_interval("weight"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    match_evaluation_id: Mapped[UUID] = mapped_column(
        ForeignKey("match_evaluations.id", ondelete="CASCADE"))
    dimension: Mapped[MatchDimension] = mapped_column(
        enum_column(MatchDimension, "match_dimension"))
    score: Mapped[float]
    weight: Mapped[float] = mapped_column(server_default=text("1.0"))
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)

    evaluation: Mapped["MatchEvaluationRow"] = relationship(
        back_populates="dimensions", lazy="raise")


# `GeocodingResult._the_payload_matches_the_outcome`, as a CHECK. The reason it is
# worth restating in the database is the reason the validator exists: an adapter is
# written against one example response, and `AMBIGUOUS` with a single candidate is
# the bug that would quietly retire §6's prohibition on turning the first hit into
# truth. `'{}'::jsonb` is "no place" and `'[]'::jsonb` is "no alternatives", the
# same absent-means-empty convention `ats_evidence` uses.
_GEOCODING_PAYLOAD_MATCHES_OUTCOME: Final[str] = (
    f"(outcome = '{GeocodingOutcome.MATCHED.value}' AND place <> '{{}}'::jsonb"
    " AND alternatives = '[]'::jsonb)"
    f" OR (outcome = '{GeocodingOutcome.AMBIGUOUS.value}' AND place = '{{}}'::jsonb"
    " AND jsonb_array_length(alternatives) >= 2)"
    f" OR (outcome IN ('{GeocodingOutcome.NOT_FOUND.value}',"
    f" '{GeocodingOutcome.FAILED.value}')"
    " AND place = '{}'::jsonb AND alternatives = '[]'::jsonb)"
)


class GeocodingCacheRow(TimestampedMixin, Base):
    """One provider's answer to one normalized question (Phase 7 §23).

    A table rather than an in-process dictionary because the enrichment pass is a
    CLI invocation (§22) — the process exits, and a cache that died with it would
    let a nightly run ask a rate-limited public geocoder the same thousand
    questions every night. Idempotence by construction is the point: the primary
    key is `geocoding_cache_entry_id`, uuid5 over exactly the three columns the
    unique constraint covers, so re-asking updates the row rather than adding one.

    **The key is provider-aware and country-aware.** Two geocoders answer the same
    string differently, so caching one under the other's name would attribute
    provenance to the wrong service; and "Neuchâtel" resolves differently
    depending on the hint that accompanied it. `country_hint` is NOT NULL with `''`
    meaning "no hint", rather than nullable, for a reason worth stating: in
    PostgreSQL two NULLs are distinct in a unique constraint, so a nullable column
    here would let the unhinted question — the common one — be stored twice with
    nothing to stop it.

    **A failure is not an answer.** `expires_at` is NULL for `MATCHED`,
    `AMBIGUOUS` and `NOT_FOUND`: those are what the provider knows, and re-asking
    tomorrow gives the same reply. It is set for `FAILED`, so a timeout or a 502
    is remembered only long enough to stop a retry storm — caching one provider
    outage forever would turn it into a permanently unresolvable address.

    **No user data reaches this table.** It is shared and unscoped, so §33 applies
    with full force: the enrichment service geocodes `opportunities` and
    `company_locations` and nothing else. A candidate's home address is private,
    and normalizing it into a shared cache key would be precisely the leak §33
    names — which is why "geocode a candidate profile" is not an operation this
    phase offers.

    `place` and `alternatives` hold `GeocodedPlace` documents rather than flattened
    columns. Nothing queries the cache geographically — it is read by key and only
    by key — and a `GeocodedPlace` has nowhere to put a credential by
    construction, so the JSONB carries no provider blob (§6).
    """

    __tablename__ = "geocoding_cache"
    __table_args__ = (
        UniqueConstraint("provider", "country_hint", "normalized_query"),
        _code_format("country_hint", "^([A-Z]{2})?$"),
        _code_format("provider", "^[a-z][a-z0-9_]*$"),
        CheckConstraint(_GEOCODING_PAYLOAD_MATCHES_OUTCOME,
                        name="payload_matches_outcome"),
        # Only a failure may expire, and it must: the two halves of §23's rule,
        # stated so a provider outage cannot be recorded as permanent.
        CheckConstraint(
            f"(outcome = '{GeocodingOutcome.FAILED.value}')"
            " = (expires_at IS NOT NULL)",
            name="only_a_failure_expires"),
        CheckConstraint("normalized_query <> ''", name="normalized_query_present"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(_PROVENANCE_KEY_LENGTH))
    country_hint: Mapped[str] = mapped_column(String(2), server_default=text("''"))
    normalized_query: Mapped[str]
    outcome: Mapped[GeocodingOutcome] = mapped_column(
        enum_column(GeocodingOutcome, "geocoding_outcome"))
    place: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=_EMPTY_JSON_OBJECT)
    alternatives: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSON_ARRAY)
    # Composed by the adapter from a fixed vocabulary, never a forwarded provider
    # message and never a formatted exception: a URL in an exception carries its
    # query string, and a query string carries the API key
    # (`backend/app/discovery/failures.py`).
    detail: Mapped[str | None]
    # When the provider was actually asked. `created_at` is when the row was
    # written, which stops being the same thing the first time an entry is
    # refreshed in place.
    checked_at: Mapped[datetime]
    expires_at: Mapped[datetime | None]








