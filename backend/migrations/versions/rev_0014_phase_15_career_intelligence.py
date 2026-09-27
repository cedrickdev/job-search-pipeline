"""phase 15 outcome tracking and the career-intelligence loop

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-26 12:00:00.000000

Phase 15 closes the loop — observe, measure, recommend, and (only after a human approves)
let an existing service execute — and gives its persisted pieces a home. `application_outcomes`
records real-world hiring milestones, kept deliberately apart from the Phase 12 execution
lifecycle: there is no `ApplicationState` column here, and a recruiter's REJECTED is a fact
about hiring, never an execution failure (§2, §84). `role_classifications` is the by-role axis
the funnel is sliced on, idempotent on `(user_id, opportunity_id)` and honest about an
unclassified role. `career_recommendations` + `career_recommendation_evidence` are the
evidence-backed suggestions the engine draws, an aggregate whose child rows carry the numbers
that justify the parent — "evidence or nothing", and never a claim from a sample below the
floor. `strategy_change_proposals` + `strategy_change_executions` are the one link allowed to
touch search or policy state, and only after an explicit approval: the proposal changes
nothing until confirmed, and the execution is the executor's write-once audit of having
applied it.

The CHECKs are domain validators made physical, so a row written by a migration or by psql
cannot assert what the domain layer could never have produced: `no_self_supersede` restates
that a correction cannot supersede itself, `manual_names_a_family` restates that a human's
MANUAL verdict must name a family, the evidence CHECKs restate "one metric shape per item"
in full (a rate XOR a timing, a rate's counts within its denominator, a timing's median),
and `target_matches_kind` restates that a proposal's queryable `target` agrees with the change
family it wraps.

Edited after `alembic revision --autogenerate` the way revisions 0002-0013 document: no
application imports for column types, the repetition factored into the helpers below, and the
enum CHECKs created and dropped implicitly with their tables. The three foreign keys whose
convention-generated names would exceed PostgreSQL's 63-character identifier limit are named
explicitly, verbatim as `models.py` names them. The schema-drift test compares the result
against `Base.metadata`, which is what keeps that a claim.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_INTEGER = sa.Integer()
_DOUBLE: sa.Double[float] = sa.Double()
_JSONB = postgresql.JSONB()
_EMPTY_JSON_OBJECT = sa.text("'{}'::jsonb")

# The floor a cited metric's sample must clear, restated from
# `backend.app.domain.recommendation.MIN_RECOMMENDATION_SAMPLE_SIZE`. Written out rather
# than imported, the way this file avoids application imports; the CHECK behind the column
# and the drift test keep the two agreeing.
_MIN_RECOMMENDATION_SAMPLE_SIZE = 5

# The members of the enums this revision introduces, written out. Each is a `StrEnum` in
# `backend.app.domain.{outcome,role,recommendation,analytics,strategy_change}`; the CHECK
# behind the column keeps the two lists agreeing, and the drift test keeps this file and
# `models.py` agreeing.
_OUTCOME_KINDS = ("ACKNOWLEDGED", "SCREEN", "ASSESSMENT", "INTERVIEW", "OFFER_RECEIVED",
                  "OFFER_ACCEPTED", "OFFER_DECLINED", "REJECTED", "WITHDRAWN")
_OUTCOME_SOURCES = ("MANUAL_USER", "EMAIL", "ATS", "IMPORTED")
_OUTCOME_STATUSES = ("EFFECTIVE", "SUPERSEDED", "RETRACTED")
_ROLE_FAMILIES = ("SOFTWARE_ENGINEERING", "DATA_AND_ANALYTICS",
                  "INFRASTRUCTURE_AND_DEVOPS", "SECURITY", "PRODUCT_MANAGEMENT", "DESIGN",
                  "PROJECT_AND_PROGRAM", "IT_SUPPORT", "SALES", "MARKETING",
                  "CUSTOMER_SUCCESS", "OPERATIONS", "FINANCE", "HUMAN_RESOURCES")
_ROLE_PROVENANCES = ("DETERMINISTIC_TITLE", "MANUAL")
_RECOMMENDATION_KINDS = ("PRIORITIZE_ROLE_FAMILY", "DEPRIORITIZE_ROLE_FAMILY",
                         "PRIORITIZE_SOURCE", "DEPRIORITIZE_SOURCE",
                         "REVIEW_OPPORTUNITY_TYPE_MIX", "REVIEW_DOCUMENT_STRATEGY",
                         "REVIEW_APPLICATION_VOLUME", "REVIEW_INTERVIEW_PREPARATION")
_DIMENSION_KINDS = ("ROLE_FAMILY", "SOURCE", "OPPORTUNITY_TYPE", "DOCUMENT_STRATEGY")
_RATE_KINDS = ("RESPONSE", "INTERVIEW_CONVERSION", "OFFER_CONVERSION", "ACCEPTANCE")
_TIMING_KINDS = ("TIME_TO_FIRST_RESPONSE", "TIME_TO_INTERVIEW", "TIME_TO_OFFER",
                 "TIME_TO_DECISION")
_STRATEGY_TARGETS = ("SEARCH_PROFILE", "APPLICATION_POLICY")
_STRATEGY_KINDS = ("SET_SEARCH_RADIUS", "SET_SEARCH_KEYWORDS", "SET_SEARCH_SOURCES",
                   "SET_SEARCH_OPPORTUNITY_TYPES", "SET_POLICY_OPPORTUNITY_TYPES",
                   "SET_APPLICATION_VOLUME", "SET_MINIMUM_SCORE")
_STRATEGY_PROPOSAL_STATUSES = ("PROPOSED", "EXECUTED", "REJECTED", "FAILED", "DISMISSED",
                               "EXPIRED")
_STRATEGY_EXECUTION_OUTCOMES = ("SUCCEEDED", "REJECTED", "FAILED")

# Verbatim copies of the constants in `models.py`, so the drift test — and a reader — see
# one expression on both sides of each invariant.
_ROLE_CLASSIFICATION_MANUAL_NAMES_A_FAMILY = (
    "provenance <> 'MANUAL' OR role_family IS NOT NULL")
_RECOMMENDATION_EVIDENCE_ONE_METRIC_SHAPE = (
    "(rate_kind IS NULL) <> (timing_kind IS NULL)")
_RECOMMENDATION_EVIDENCE_RATE_SHAPE = (
    "rate_kind IS NULL"
    " OR (numerator IS NOT NULL AND denominator IS NOT NULL"
    "     AND median_days IS NULL AND numerator <= denominator)")
_RECOMMENDATION_EVIDENCE_TIMING_SHAPE = (
    "timing_kind IS NULL"
    " OR (median_days IS NOT NULL AND numerator IS NULL AND denominator IS NULL)")
_RECOMMENDATION_EVIDENCE_OVERALL_HAS_NO_KEY = (
    "dimension IS NOT NULL OR dimension_key IS NULL")
_STRATEGY_PROPOSAL_TARGET_MATCHES_KIND = (
    "(target = 'SEARCH_PROFILE' AND kind IN"
    " ('SET_SEARCH_RADIUS', 'SET_SEARCH_KEYWORDS', 'SET_SEARCH_SOURCES',"
    "  'SET_SEARCH_OPPORTUNITY_TYPES'))"
    " OR (target = 'APPLICATION_POLICY' AND kind IN"
    " ('SET_POLICY_OPPORTUNITY_TYPES', 'SET_APPLICATION_VOLUME', 'SET_MINIMUM_SCORE'))")


def _enum(name: str, *members: str, length: int = 32) -> sa.Enum:
    """A `VARCHAR(length)` plus a CHECK on the permitted values, as revision 0002 gives."""
    return sa.Enum(*members, name=name, native_enum=False, create_constraint=True,
                   length=length)


def _timestamps() -> tuple[sa.Column[Any], ...]:
    """`created_at` and `updated_at`, as `TimestampedMixin` declares them."""
    return (
        sa.Column("created_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", _TIMESTAMPTZ, server_default=sa.text("now()"),
                  nullable=False),
    )


def upgrade() -> None:
    """Create the Phase 15 career-intelligence schema, parents before children."""
    op.create_table(
        "application_outcomes",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("application_id", _UUID, nullable=False),
        sa.Column("kind", _enum("application_outcome_kind", *_OUTCOME_KINDS),
                  nullable=False),
        sa.Column("source", _enum("application_outcome_source", *_OUTCOME_SOURCES),
                  server_default=sa.text("'MANUAL_USER'"), nullable=False),
        sa.Column("status", _enum("application_outcome_status", *_OUTCOME_STATUSES),
                  server_default=sa.text("'EFFECTIVE'"), nullable=False),
        sa.Column("outcome_key", _TEXT, nullable=False),
        sa.Column("occurred_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("recorded_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("supersedes_id", _UUID, nullable=True),
        sa.Column("detail", _TEXT, nullable=True),
        *_timestamps(),
        sa.CheckConstraint("supersedes_id IS NULL OR supersedes_id <> id",
                           name=op.f("ck_application_outcomes_no_self_supersede")),
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"],
            name=op.f("fk_application_outcomes_application_id_applications"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["application_outcomes.id"],
            name=op.f("fk_application_outcomes_supersedes_id_application_outcomes"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_application_outcomes_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_outcomes")),
        sa.UniqueConstraint(
            "application_id", "outcome_key",
            name=op.f("uq_application_outcomes_application_id_outcome_key")),
    )
    op.create_index("ix_application_outcomes_user_id", "application_outcomes",
                    ["user_id"], unique=False)
    op.create_index("ix_application_outcomes_application_id_occurred_at",
                    "application_outcomes", ["application_id", "occurred_at"],
                    unique=False)

    op.create_table(
        "role_classifications",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("opportunity_id", _UUID, nullable=False),
        sa.Column("role_family", _enum("role_family", *_ROLE_FAMILIES), nullable=True),
        sa.Column("provenance", _enum("role_family_provenance", *_ROLE_PROVENANCES),
                  server_default=sa.text("'DETERMINISTIC_TITLE'"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(_ROLE_CLASSIFICATION_MANUAL_NAMES_A_FAMILY,
                           name=op.f("ck_role_classifications_manual_names_a_family")),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_role_classifications_updated_at_after_created_at")),
        sa.ForeignKeyConstraint(
            ["opportunity_id"], ["opportunities.id"],
            name=op.f("fk_role_classifications_opportunity_id_opportunities"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_role_classifications_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_classifications")),
        sa.UniqueConstraint(
            "user_id", "opportunity_id",
            name=op.f("uq_role_classifications_user_id_opportunity_id")),
    )
    op.create_index("ix_role_classifications_user_id_role_family",
                    "role_classifications", ["user_id", "role_family"], unique=False)
    op.create_index("ix_role_classifications_opportunity_id", "role_classifications",
                    ["opportunity_id"], unique=False)

    op.create_table(
        "career_recommendations",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("kind", _enum("career_recommendation_kind", *_RECOMMENDATION_KINDS),
                  nullable=False),
        sa.Column("analytics_version", _TEXT, nullable=False),
        sa.Column("summary", _TEXT, nullable=False),
        sa.Column("detail", _TEXT, nullable=True),
        sa.Column("generator_key", _TEXT, nullable=True),
        sa.Column("llm_run_id", _UUID, nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["llm_run_id"], ["llm_runs.id"],
            name=op.f("fk_career_recommendations_llm_run_id_llm_runs"),
            ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_career_recommendations_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_career_recommendations")),
    )
    op.create_index("ix_career_recommendations_user_id_created_at",
                    "career_recommendations", ["user_id", "created_at"], unique=False)

    op.create_table(
        "career_recommendation_evidence",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("recommendation_id", _UUID, nullable=False),
        sa.Column("ordinal", _INTEGER, nullable=False),
        sa.Column("dimension", _enum("evidence_dimension", *_DIMENSION_KINDS),
                  nullable=True),
        sa.Column("dimension_key", _TEXT, nullable=True),
        sa.Column("rate_kind", _enum("evidence_rate_kind", *_RATE_KINDS), nullable=True),
        sa.Column("timing_kind", _enum("evidence_timing_kind", *_TIMING_KINDS),
                  nullable=True),
        sa.Column("numerator", _INTEGER, nullable=True),
        sa.Column("denominator", _INTEGER, nullable=True),
        sa.Column("median_days", _DOUBLE, nullable=True),
        sa.Column("sample_size", _INTEGER, nullable=False),
        sa.Column("detail", _TEXT, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "ordinal >= 0",
            name=op.f("ck_career_recommendation_evidence_ordinal_non_negative")),
        sa.CheckConstraint(
            f"sample_size >= {_MIN_RECOMMENDATION_SAMPLE_SIZE}",
            name=op.f("ck_career_recommendation_evidence_sample_size_meets_minimum")),
        sa.CheckConstraint(
            "numerator IS NULL OR numerator >= 0",
            name=op.f("ck_career_recommendation_evidence_numerator_non_negative")),
        sa.CheckConstraint(
            "denominator IS NULL OR denominator >= 0",
            name=op.f("ck_career_recommendation_evidence_denominator_non_negative")),
        sa.CheckConstraint(
            "median_days IS NULL OR median_days >= 0.0",
            name=op.f("ck_career_recommendation_evidence_median_days_non_negative")),
        sa.CheckConstraint(
            _RECOMMENDATION_EVIDENCE_ONE_METRIC_SHAPE,
            name=op.f("ck_career_recommendation_evidence_cites_one_metric_shape")),
        sa.CheckConstraint(
            _RECOMMENDATION_EVIDENCE_RATE_SHAPE,
            name=op.f("ck_career_recommendation_evidence_rate_shape_coherent")),
        sa.CheckConstraint(
            _RECOMMENDATION_EVIDENCE_TIMING_SHAPE,
            name=op.f("ck_career_recommendation_evidence_timing_shape_coherent")),
        sa.CheckConstraint(
            _RECOMMENDATION_EVIDENCE_OVERALL_HAS_NO_KEY,
            name=op.f("ck_career_recommendation_evidence_overall_metric_has_no_key")),
        sa.ForeignKeyConstraint(
            ["recommendation_id"], ["career_recommendations.id"],
            name="fk_recommendation_evidence_recommendation_id", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_career_recommendation_evidence")),
        sa.UniqueConstraint(
            "recommendation_id", "ordinal",
            name=op.f("uq_career_recommendation_evidence_recommendation_id_ordinal")),
    )

    op.create_table(
        "strategy_change_proposals",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("target", _enum("strategy_change_target", *_STRATEGY_TARGETS),
                  nullable=False),
        sa.Column("target_id", _UUID, nullable=False),
        sa.Column("kind", _enum("strategy_change_kind", *_STRATEGY_KINDS),
                  nullable=False),
        sa.Column("change", _JSONB, server_default=_EMPTY_JSON_OBJECT, nullable=False),
        sa.Column("target_version", _TIMESTAMPTZ, nullable=False),
        sa.Column("summary", _TEXT, nullable=False),
        sa.Column("source_recommendation_id", _UUID, nullable=True),
        sa.Column("generator_key", _TEXT, nullable=True),
        sa.Column("llm_run_id", _UUID, nullable=True),
        sa.Column("status",
                  _enum("strategy_change_proposal_status", *_STRATEGY_PROPOSAL_STATUSES),
                  server_default=sa.text("'PROPOSED'"), nullable=False),
        sa.Column("expires_at", _TIMESTAMPTZ, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            _STRATEGY_PROPOSAL_TARGET_MATCHES_KIND,
            name=op.f("ck_strategy_change_proposals_target_matches_kind")),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_strategy_change_proposals_updated_at_after_created_at")),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_strategy_change_proposals_expires_at_after_created_at")),
        sa.ForeignKeyConstraint(
            ["llm_run_id"], ["llm_runs.id"],
            name=op.f("fk_strategy_change_proposals_llm_run_id_llm_runs"),
            ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["source_recommendation_id"], ["career_recommendations.id"],
            name="fk_strategy_proposals_source_recommendation_id", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_strategy_change_proposals_user_id_users"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_change_proposals")),
    )
    op.create_index("ix_strategy_change_proposals_user_id_status",
                    "strategy_change_proposals", ["user_id", "status"], unique=False)
    op.create_index("ix_strategy_change_proposals_target_id",
                    "strategy_change_proposals", ["target_id"], unique=False)

    op.create_table(
        "strategy_change_executions",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("proposal_id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("outcome",
                  _enum("strategy_change_execution_outcome",
                        *_STRATEGY_EXECUTION_OUTCOMES),
                  nullable=False),
        sa.Column("observed_target_version", _TIMESTAMPTZ, nullable=True),
        sa.Column("detail", _TEXT, nullable=True),
        sa.Column("result_ref", _TEXT, nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["strategy_change_proposals.id"],
            name="fk_strategy_executions_proposal_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_strategy_change_executions_user_id_users"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategy_change_executions")),
        sa.UniqueConstraint(
            "proposal_id",
            name=op.f("uq_strategy_change_executions_proposal_id")),
    )
    op.create_index("ix_strategy_change_executions_user_id",
                    "strategy_change_executions", ["user_id"], unique=False)


def downgrade() -> None:
    """Return the schema to revision 0013, children before parents.

    Destructive: every recorded outcome, role classification, recommendation, proposal and
    execution is deleted. Explicit indexes are dropped explicitly; the enum CHECKs, unique
    constraints and foreign keys go with their tables.
    """
    op.drop_index("ix_strategy_change_executions_user_id",
                  table_name="strategy_change_executions")
    op.drop_table("strategy_change_executions")
    op.drop_index("ix_strategy_change_proposals_target_id",
                  table_name="strategy_change_proposals")
    op.drop_index("ix_strategy_change_proposals_user_id_status",
                  table_name="strategy_change_proposals")
    op.drop_table("strategy_change_proposals")
    op.drop_table("career_recommendation_evidence")
    op.drop_index("ix_career_recommendations_user_id_created_at",
                  table_name="career_recommendations")
    op.drop_table("career_recommendations")
    op.drop_index("ix_role_classifications_opportunity_id",
                  table_name="role_classifications")
    op.drop_index("ix_role_classifications_user_id_role_family",
                  table_name="role_classifications")
    op.drop_table("role_classifications")
    op.drop_index("ix_application_outcomes_application_id_occurred_at",
                  table_name="application_outcomes")
    op.drop_index("ix_application_outcomes_user_id",
                  table_name="application_outcomes")
    op.drop_table("application_outcomes")




