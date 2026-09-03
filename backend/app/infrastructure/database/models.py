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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import TextClause

from backend.app.domain.common import GeoPoint, SalaryPeriod
from backend.app.domain.matching import MatchDimension
from backend.app.domain.opportunity import ContractType, OpportunityType, WorkplaceMode
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
    """

    location_country: Mapped[str | None] = mapped_column(String(2))
    location_region: Mapped[str | None]
    location_city: Mapped[str | None]
    location_postal_code: Mapped[str | None]
    location_point: Mapped[GeoPoint | None]
    location_raw: Mapped[str | None]


class UserRow(TimestampedMixin, Base):
    """The owner of user-scoped rows, and nothing more.

    Deliberately minimal: Phase 2 needs a foreign-key target so that
    `match_evaluations` can be user-scoped from the first migration, and Phase 4
    owns authentication. There is no password, no email and no session here, so
    nothing in this table can leak a credential before the phase that is
    supposed to design one.
    """

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    display_name: Mapped[str | None]


class CandidateProfileRow(TimestampedMixin, Base):
    """A candidate profile, as a foreign-key target for evaluations.

    Separate from `users` because one person legitimately searches under more
    than one profile (a student job and a graduate role weigh education
    differently), and an evaluation is against a profile, not against an account.
    The profile's contents arrive with onboarding in Phase 4 and the evidence
    store in Phase 10; this is the identity those rows will hang from.
    """

    __tablename__ = "candidate_profiles"
    __table_args__ = (
        Index("ix_candidate_profiles_user_id", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"))
    label: Mapped[str | None]


class CompanyRow(TimestampedMixin, Base):
    """A canonical employer.

    `name` is indexed but not unique. Two legally distinct companies share a name
    often enough that a unique constraint would reject real data, and
    canonicalizing "Migros", "Migros SA" and "MIGROS Vaud" into one identity is
    Phase 6's discovery work. Until then identity comes from the primary key: the
    V1 importer derives it deterministically (uuid5) from the employer string, so
    re-importing the same string updates the same row instead of adding a
    duplicate.

    `accepts_spontaneous_applications` is nullable because the domain models it as
    three-valued — NULL is "nobody has looked", which an application strategy must
    not read as "no".
    """

    __tablename__ = "companies"
    __table_args__ = (
        Index("ix_companies_name", "name"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str]
    website: Mapped[str | None]
    careers_url: Mapped[str | None]
    accepts_spontaneous_applications: Mapped[bool | None]

    locations: Mapped[list["CompanyLocationRow"]] = relationship(
        back_populates="company", cascade="all, delete-orphan",
        passive_deletes=True, lazy="raise",
        # Deterministic order, so a company read twice produces an equal domain
        # object and a round-trip test can compare tuples directly.
        order_by="CompanyLocationRow.id")


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
        Index("ix_company_locations_company_id", "company_id"),
        # GiST is what makes `ST_DWithin` an index lookup instead of a scan of
        # every site in the country.
        Index("ix_company_locations_location_point", "location_point",
              postgresql_using="gist"),
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

_WORKLOAD_PERCENT_RANGE: Final[str] = (
    "workload_min_percent BETWEEN 1 AND 100"
    " AND workload_max_percent BETWEEN 1 AND 100"
)

_WORKLOAD_HOURS_RANGE: Final[str] = (
    "workload_min_weekly_hours > 0 AND workload_min_weekly_hours <= 168"
    " AND workload_max_weekly_hours > 0 AND workload_max_weekly_hours <= 168"
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
        Index("ix_opportunities_company_id", "company_id"),
        # The feed is "what is new", so this is the ordering every list query
        # uses. PostgreSQL does not index a foreign key or a sort column for you.
        Index("ix_opportunities_discovered_at", "discovered_at"),
        Index("ix_opportunities_location_point", "location_point",
              postgresql_using="gist"),
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








