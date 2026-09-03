"""v2 core schema

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-03 21:52:15.544446

The eight tables of the V2 core: users and candidate profiles, companies and
their sites, opportunities and the source record each one came from, match
evaluations and their per-dimension scores.

Produced by `alembic revision --autogenerate` against rev 0001 and then edited in
two ways worth knowing about.

*No application imports.* Autogenerate wrote `UtcDateTime` and `GeographyPoint`,
the mapped types from `backend.app.infrastructure.database.types`. A migration is
a record of DDL that has already run on real databases, so it has to keep
producing the same schema after those classes are renamed, moved or deleted — the
column types are therefore spelled out in SQLAlchemy and PostGIS terms.
`sa.DateTime(timezone=True)` is the `TIMESTAMPTZ` that `UtcDateTime` emits;
`Geography` below is the `geography(Point,4326)` that `GeographyPoint` emits.
Neither carries the Python-side conversions, and neither needs to: a migration
creates columns, it does not read or write rows.

*Reformatted, and repetition factored out.* Autogenerate emits one statement per
line at any width. The helpers below produce exactly the columns it wrote — this
is presentation, not a change of DDL, and the schema-drift test compares the
result against `Base.metadata` to keep that claim honest.

One thing that is absent rather than removed: the CHECK constraints backing the
enum columns. `sa.Enum(..., native_enum=False, create_constraint=True)` adds them
when the column is attached to a table, and `op.create_table` picks up
`Base.metadata.naming_convention` from `target_metadata` in env.py, so they land
with the same generated names as in the mapped metadata.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

class Geography(sa.types.UserDefinedType[str]):
    """`geography(Point,4326)`, spelled out so this file stands alone.

    Only `get_col_spec` matters here: it is the type name PostgreSQL is given in
    `CREATE TABLE`. PostGIS records the column in `geography_columns` from that
    declaration, which is what makes the GiST indexes below possible.
    """

    cache_ok = True

    def get_col_spec(self, **kwargs: Any) -> str:
        return "geography(Point,4326)"


# Shared type instances: a SQLAlchemy type object carries no column state, so one
# instance can describe every column that uses it.
_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_JSONB = postgresql.JSONB(astext_type=sa.Text())
_MONEY = sa.Numeric(precision=14, scale=2)
_GEOGRAPHY_POINT = Geography()


def _enum(name: str, *members: str) -> sa.Enum:
    """A `VARCHAR(32)` plus a CHECK on the permitted values.

    No PostgreSQL `ENUM` type: adding a member to a native enum is a DDL change
    that cannot run inside a transaction with other statements on older servers,
    while widening a CHECK is an ordinary migration. `length=32` leaves room for
    longer member names without a second change to the column.
    """
    return sa.Enum(*members, name=name, native_enum=False, create_constraint=True,
                   length=32)


def _timestamps() -> tuple[sa.Column[Any], ...]:
    """`created_at` and `updated_at`, as `TimestampedMixin` declares them.

    `now()` as a server default rather than a Python value: the row's clock is the
    database's, so two processes inserting concurrently cannot disagree about
    order because one of them had a skewed system clock.
    """
    return (
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
    )

def _location_columns() -> tuple[sa.Column[Any], ...]:
    """The flattened `Location`, as `LocationColumnsMixin` declares it.

    Six columns rather than one composite: a city, a postal code and a point are
    each queried on their own, and every component is individually nullable
    because a posting that says only "Remote, CH" is still worth storing. The
    tables that require *some* location add a CHECK; a column-level NOT NULL
    would be the wrong tool.
    """
    return (
        sa.Column("location_country", sa.String(length=2), nullable=True),
        sa.Column("location_region", _TEXT, nullable=True),
        sa.Column("location_city", _TEXT, nullable=True),
        sa.Column("location_postal_code", _TEXT, nullable=True),
        sa.Column("location_point", _GEOGRAPHY_POINT, nullable=True),
        sa.Column("location_raw", _TEXT, nullable=True),
    )


def upgrade() -> None:
    """Create the V2 core schema."""
    op.create_table(
        "companies",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("name", _TEXT, nullable=False),
        sa.Column("website", _TEXT, nullable=True),
        sa.Column("careers_url", _TEXT, nullable=True),
        # Three-valued on purpose: TRUE, FALSE and "nobody has found out yet".
        sa.Column("accepts_spontaneous_applications", sa.Boolean(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
    )
    op.create_index("ix_companies_name", "companies", ["name"], unique=False)

    # No credentials, no email, no authentication: Phase 4 owns identity. The table
    # exists now so that user-scoped rows can carry a real foreign key instead of a
    # loose column that would have to be backfilled later.
    op.create_table(
        "users",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("display_name", _TEXT, nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )

    op.create_table(
        "candidate_profiles",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("label", _TEXT, nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_candidate_profiles_user_id_users"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidate_profiles")),
    )
    op.create_index("ix_candidate_profiles_user_id", "candidate_profiles", ["user_id"],
                    unique=False)

    op.create_table(
        "company_locations",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("company_id", _UUID, nullable=False),
        sa.Column("is_headquarters", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False),
        *_location_columns(),
        *_timestamps(),
        sa.CheckConstraint(
            "location_country ~ '^[A-Z]{2}$'",
            name=op.f("ck_company_locations_location_country_format")),
        # A site row with every component NULL is a row that says nothing; the
        # domain's `Location` cannot be constructed that way either.
        sa.CheckConstraint(
            "location_country IS NOT NULL OR location_region IS NOT NULL"
            " OR location_city IS NOT NULL OR location_postal_code IS NOT NULL"
            " OR location_point IS NOT NULL OR location_raw IS NOT NULL",
            name=op.f("ck_company_locations_location_not_empty")),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_company_locations_company_id_companies"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_locations")),
    )
    op.create_index("ix_company_locations_company_id", "company_locations",
                    ["company_id"], unique=False)
    # GiST, which is the index type `ST_DWithin` can use; a B-tree on a geography
    # column would be built happily and then never chosen.
    op.create_index("ix_company_locations_location_point", "company_locations",
                    ["location_point"], unique=False, postgresql_using="gist")
    # Partial: at most one headquarters per company, any number of other sites.
    op.create_index("uq_company_locations_company_id_headquarters", "company_locations",
                    ["company_id"], unique=True,
                    postgresql_where=sa.text("is_headquarters"))

    op.create_table(
        "opportunities",
        sa.Column("id", _UUID, nullable=False),
        # `SET NULL`, not `CASCADE`: deleting a company must not delete the
        # postings that were discovered through it. `company_name` is kept
        # alongside the reference for exactly that reason — it is what the source
        # said, and it survives the company row.
        sa.Column("company_id", _UUID, nullable=True),
        sa.Column("company_name", _TEXT, nullable=False),
        sa.Column("title", _TEXT, nullable=False),
        sa.Column("description", _TEXT, nullable=True),
        sa.Column("opportunity_type",
                  _enum("opportunity_type", "FULL_TIME", "PART_TIME", "STUDENT_JOB",
                        "INTERNSHIP", "APPRENTICESHIP", "WORK_STUDY", "GRADUATE",
                        "TEMPORARY", "FREELANCE"),
                  nullable=True),
        sa.Column("contract_type",
                  _enum("contract_type", "PERMANENT", "FIXED_TERM",
                        "TEMPORARY_AGENCY", "SERVICE_CONTRACT"),
                  nullable=True),
        sa.Column("workplace_mode",
                  _enum("workplace_mode", "ON_SITE", "HYBRID", "REMOTE"),
                  nullable=True),
        sa.Column("workload_min_percent", sa.SmallInteger(), nullable=True),
        sa.Column("workload_max_percent", sa.SmallInteger(), nullable=True),
        sa.Column("workload_min_weekly_hours", sa.Double(), nullable=True),
        sa.Column("workload_max_weekly_hours", sa.Double(), nullable=True),
        sa.Column("salary_currency", sa.String(length=3), nullable=True),
        sa.Column("salary_period",
                  _enum("salary_period", "HOURLY", "DAILY", "WEEKLY", "MONTHLY",
                        "YEARLY"),
                  nullable=True),
        # NUMERIC, never a float: a salary is money, and 4200.10 has to come back
        # as 4200.10.
        sa.Column("salary_minimum", _MONEY, nullable=True),
        sa.Column("salary_maximum", _MONEY, nullable=True),
        sa.Column("posting_language", sa.String(length=2), nullable=True),
        sa.Column("language_requirements", _JSONB,
                  server_default=sa.text("'[]'::jsonb"), nullable=False),
        # A date, not a timestamp: boards publish "posted on the 3rd", and storing
        # midnight in some timezone would invent precision that was never there.
        sa.Column("posted_at", sa.Date(), nullable=True),
        sa.Column("discovered_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("application_url", _TEXT, nullable=True),
        sa.Column("dedup_fingerprint", _TEXT, nullable=True),
        *_location_columns(),
        *_timestamps(),
        sa.CheckConstraint("location_country ~ '^[A-Z]{2}$'",
                           name=op.f("ck_opportunities_location_country_format")),
        sa.CheckConstraint("posting_language ~ '^[a-z]{2}$'",
                           name=op.f("ck_opportunities_posting_language_format")),
        sa.CheckConstraint("salary_currency ~ '^[A-Z]{3}$'",
                           name=op.f("ck_opportunities_salary_currency_format")),
        # A currency with no amount, or an amount with no currency, is not a
        # salary. Either the whole group is absent or it is usable.
        sa.CheckConstraint(
            "(salary_currency IS NULL AND salary_period IS NULL"
            " AND salary_minimum IS NULL AND salary_maximum IS NULL)"
            " OR (salary_currency IS NOT NULL AND salary_period IS NOT NULL"
            " AND (salary_minimum IS NOT NULL OR salary_maximum IS NOT NULL))",
            name=op.f("ck_opportunities_salary_complete_or_absent")),
        sa.CheckConstraint("salary_minimum <= salary_maximum",
                           name=op.f("ck_opportunities_salary_bounds_ordered")),
        sa.CheckConstraint("salary_minimum >= 0 AND salary_maximum >= 0",
                           name=op.f("ck_opportunities_salary_non_negative")),
        sa.CheckConstraint("workload_min_percent <= workload_max_percent",
                           name=op.f("ck_opportunities_workload_percent_ordered")),
        sa.CheckConstraint(
            "workload_min_percent BETWEEN 1 AND 100"
            " AND workload_max_percent BETWEEN 1 AND 100",
            name=op.f("ck_opportunities_workload_percent_range")),
        sa.CheckConstraint("workload_min_weekly_hours <= workload_max_weekly_hours",
                           name=op.f("ck_opportunities_workload_hours_ordered")),
        sa.CheckConstraint(
            "workload_min_weekly_hours > 0 AND workload_min_weekly_hours <= 168"
            " AND workload_max_weekly_hours > 0 AND workload_max_weekly_hours <= 168",
            name=op.f("ck_opportunities_workload_hours_range")),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                name=op.f("fk_opportunities_company_id_companies"),
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opportunities")),
        # Nullable and unique: PostgreSQL allows any number of NULLs, so a posting
        # whose fingerprint could not be computed is storable, while two postings
        # claiming the same fingerprint are not.
        sa.UniqueConstraint("dedup_fingerprint",
                            name=op.f("uq_opportunities_dedup_fingerprint")),
    )
    op.create_index("ix_opportunities_company_id", "opportunities", ["company_id"],
                    unique=False)
    op.create_index("ix_opportunities_discovered_at", "opportunities",
                    ["discovered_at"], unique=False)
    op.create_index("ix_opportunities_location_point", "opportunities",
                    ["location_point"], unique=False, postgresql_using="gist")

    # `user_id` alongside `candidate_profile_id` is denormalized on purpose: every
    # repository query filters on it, so authorization is one indexed predicate
    # rather than a join a future caller could forget.
    op.create_table(
        "match_evaluations",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("candidate_profile_id", _UUID, nullable=False),
        sa.Column("opportunity_id", _UUID, nullable=False),
        sa.Column("overall", sa.Double(), nullable=False),
        sa.Column("evidence_confidence", sa.Double(), nullable=True),
        sa.Column("reasons", _JSONB, server_default=sa.text("'[]'::jsonb"),
                  nullable=False),
        sa.Column("evaluator_key", _TEXT, nullable=True),
        sa.Column("evaluated_at", _TIMESTAMPTZ, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "evidence_confidence BETWEEN 0.0 AND 1.0",
            name=op.f("ck_match_evaluations_evidence_confidence_in_unit_interval")),
        sa.CheckConstraint("overall BETWEEN 0.0 AND 1.0",
                           name=op.f("ck_match_evaluations_overall_in_unit_interval")),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"], ["candidate_profiles.id"],
            name=op.f("fk_match_evaluations_candidate_profile_id_candidate_profiles"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["opportunity_id"], ["opportunities.id"],
            name=op.f("fk_match_evaluations_opportunity_id_opportunities"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_match_evaluations_user_id_users"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_match_evaluations")),
        # One evaluation per (profile, opportunity). Re-evaluating updates the row,
        # which is what makes an import or a re-scoring run repeatable.
        sa.UniqueConstraint(
            "candidate_profile_id", "opportunity_id",
            name=op.f("uq_match_evaluations_candidate_profile_id_opportunity_id")),
    )
    op.create_index("ix_match_evaluations_opportunity_id", "match_evaluations",
                    ["opportunity_id"], unique=False)
    op.create_index("ix_match_evaluations_user_id_evaluated_at", "match_evaluations",
                    ["user_id", "evaluated_at"], unique=False)

    op.create_table(
        "opportunity_source_records",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("opportunity_id", _UUID, nullable=False),
        sa.Column("source_key", _TEXT, nullable=False),
        sa.Column("external_id", _TEXT, nullable=True),
        sa.Column("source_url", _TEXT, nullable=True),
        sa.Column("fetched_at", _TIMESTAMPTZ, nullable=False),
        # Everything the source sent that V2 has no column for. JSONB rather than
        # text: it is queryable, and nothing is thrown away just because this
        # schema does not model it yet.
        sa.Column("raw", _JSONB, server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["opportunity_id"], ["opportunities.id"],
            name=op.f("fk_opportunity_source_records_opportunity_id_opportunities"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opportunity_source_records")),
        # One record per opportunity, enforced rather than assumed: the mapper
        # writes it through a scalar relationship.
        sa.UniqueConstraint(
            "opportunity_id",
            name=op.f("uq_opportunity_source_records_opportunity_id")),
        # And one opportunity per (source, external id) — the constraint the
        # importer relies on to be idempotent.
        sa.UniqueConstraint(
            "source_key", "external_id",
            name=op.f("uq_opportunity_source_records_source_key_external_id")),
    )

    op.create_table(
        "match_dimension_scores",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("match_evaluation_id", _UUID, nullable=False),
        sa.Column("dimension",
                  _enum("match_dimension", "SKILLS_FIT", "EXPERIENCE_FIT",
                        "EDUCATION_FIT", "LANGUAGE_FIT", "LOCATION_FIT",
                        "SCHEDULE_FIT"),
                  nullable=False),
        sa.Column("score", sa.Double(), nullable=False),
        sa.Column("weight", sa.Double(), server_default=sa.text("1.0"),
                  nullable=False),
        sa.Column("reasons", _JSONB, server_default=sa.text("'[]'::jsonb"),
                  nullable=False),
        *_timestamps(),
        sa.CheckConstraint("score BETWEEN 0.0 AND 1.0",
                           name=op.f("ck_match_dimension_scores_score_in_unit_interval")),
        sa.CheckConstraint(
            "weight BETWEEN 0.0 AND 1.0",
            name=op.f("ck_match_dimension_scores_weight_in_unit_interval")),
        sa.ForeignKeyConstraint(
            ["match_evaluation_id"], ["match_evaluations.id"],
            name=op.f("fk_match_dimension_scores_match_evaluation_id_match_evaluations"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_match_dimension_scores")),
        sa.UniqueConstraint(
            "match_evaluation_id", "dimension",
            name=op.f("uq_match_dimension_scores_match_evaluation_id_dimension")),
    )


def downgrade() -> None:
    """Drop the V2 core schema, children first.

    Reverse creation order, so a foreign key never outlives the table it points
    at. The indexes are dropped explicitly for the same reason autogenerate
    creates them explicitly: they were not declared inline on a column.

    This is destructive by nature — `alembic downgrade 0001` on a database holding
    imported data deletes it. That is what a downgrade means; it is here so the
    revision is reversible in development and in the test suite, not because
    running it in production is ever routine.
    """
    op.drop_table("match_dimension_scores")
    op.drop_table("opportunity_source_records")
    op.drop_index("ix_match_evaluations_user_id_evaluated_at",
                  table_name="match_evaluations")
    op.drop_index("ix_match_evaluations_opportunity_id", table_name="match_evaluations")
    op.drop_table("match_evaluations")
    op.drop_index("ix_opportunities_location_point", table_name="opportunities",
                  postgresql_using="gist")
    op.drop_index("ix_opportunities_discovered_at", table_name="opportunities")
    op.drop_index("ix_opportunities_company_id", table_name="opportunities")
    op.drop_table("opportunities")
    op.drop_index("uq_company_locations_company_id_headquarters",
                  table_name="company_locations",
                  postgresql_where=sa.text("is_headquarters"))
    op.drop_index("ix_company_locations_location_point", table_name="company_locations",
                  postgresql_using="gist")
    op.drop_index("ix_company_locations_company_id", table_name="company_locations")
    op.drop_table("company_locations")
    op.drop_index("ix_candidate_profiles_user_id", table_name="candidate_profiles")
    op.drop_table("candidate_profiles")
    op.drop_table("users")
    op.drop_index("ix_companies_name", table_name="companies")
    op.drop_table("companies")
