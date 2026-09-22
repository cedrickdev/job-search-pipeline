"""phase 12 autonomous application engine

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-22 10:00:00.000000

Phase 12 turns a decision into an audited application, and gives its five pieces a
home. `application_policies` is a user's standing rules — the mode, the approval
brake, the score floors and the rate budget — defaulting to the cautious `MANUAL`
policy. `application_decisions` is the record of intent the matcher produces.
`applications` is the execution aggregate: its lifecycle `state`, the exact document
versions it pinned, the answers it resolved, and — the load-bearing column — a UNIQUE
`idempotency_key`, so a second application for the same target collides rather than
opening a duplicate (§36). `application_events` is the append-only audit trail (§41),
and `submission_attempts` records each try at the irreversible act (§39).

The CHECKs are domain validators made physical, so a row written by a migration or by
psql cannot assert a state the engine could never have produced. `brake_coherent`
restates "only AUTOPILOT may submit unattended"; `target_matches_kind` and
`target_singular` restate the one-target rule; and the five `submission_attempts`
CHECKs restate `SubmissionResult`'s field/outcome agreement, each written so an
in-flight attempt (outcome NULL) passes — the crash evidence §88 depends on.

Edited after `alembic revision --autogenerate` the way revisions 0002-0008 document:
no application imports for column types, the repetition factored into the helpers
below, and the enum CHECKs created and dropped implicitly with their tables. The
schema-drift test compares the result against `Base.metadata`, which is what keeps
that a claim.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_JSONB = postgresql.JSONB()
_EMPTY_JSON_ARRAY = sa.text("'[]'::jsonb")
_EMPTY_TEXT_ARRAY = sa.text("'{}'::text[]")

# The members of the enums this revision introduces, written out. Each is a
# `StrEnum` in `backend.app.domain`; the CHECK behind the column keeps the two lists
# agreeing, and the schema-drift test keeps this file and `models.py` agreeing.
_AUTOMATION_MODES = ("MANUAL", "COPY_ASSISTED", "SUPERVISED", "AUTOPILOT")
_DECISION_KINDS = ("SKIP", "SAVE", "PREPARE", "REQUIRE_REVIEW", "AUTO_APPLY",
                   "APPLY_AND_OUTREACH", "SPONTANEOUS_APPLICATION")
_CHANNELS = ("ATS_API", "ATS_FORM", "DIRECT_FORM", "BROWSER", "EMAIL", "MANUAL",
             "UNSUPPORTED")
_STATES = ("PLANNED", "PREPARING", "READY_FOR_REVIEW", "REQUIRES_HUMAN", "APPROVED",
           "SUBMITTING", "SUBMITTED", "SUBMISSION_STATE_UNKNOWN", "FAILED",
           "CANCELLED", "WITHDRAWN")
_EVENT_TYPES = ("CREATED", "PREPARATION_STARTED", "PREPARED", "GATE_EVALUATED",
                "HUMAN_REQUIRED", "APPROVED", "SUBMISSION_STARTED", "SUBMITTED",
                "SUBMISSION_STATE_UNKNOWN", "FAILED", "CANCELLED", "WITHDRAWN",
                "DUPLICATE_BLOCKED", "RATE_LIMITED")
_EVENT_ACTORS = ("SYSTEM", "USER", "WORKER")
_SUBMISSION_OUTCOMES = ("SUBMITTED", "REQUIRES_HUMAN", "FAILED", "STATE_UNKNOWN")
_HUMAN_REASONS = ("CAPTCHA_PRESENT", "MFA_REQUIRED", "LOGIN_REQUIRED",
                  "UNKNOWN_REQUIRED_FIELD", "SENSITIVE_QUESTION", "AMBIGUOUS_FORM",
                  "UPLOAD_UNRESOLVED", "FORM_CHANGED", "UNSUPPORTED_CHANNEL")
_FAILURE_CODES = ("APPLICATION_CHANNEL_UNSUPPORTED", "APPLICATION_DOCUMENT_NOT_READY",
                  "APPLICATION_FORM_CHANGED", "APPLICATION_MISSING_ANSWER",
                  "APPLICATION_ADAPTER_ERROR", "APPLICATION_SUBMISSION_UNKNOWN",
                  "APPLICATION_RATE_LIMITED", "APPLICATION_DUPLICATE")

# Verbatim copies of the constants in `models.py`, so the drift test sees one
# expression on both sides. Each is an implication a CHECK reads as written.
_POLICY_BRAKE_COHERENT = (
    "mode = 'AUTOPILOT' OR require_approval_before_submission"
)
_DECISION_TARGET_MATCHES_KIND = (
    "(kind = 'SPONTANEOUS_APPLICATION' AND company_id IS NOT NULL)"
    " OR (kind <> 'SPONTANEOUS_APPLICATION' AND opportunity_id IS NOT NULL)"
)
_APPLICATION_TARGET_SINGULAR = "(opportunity_id IS NULL) <> (company_id IS NULL)"
_ATTEMPT_REQUIRES_HUMAN_HAS_REASON = (
    "outcome IS NULL OR outcome <> 'REQUIRES_HUMAN'"
    " OR human_required_reason IS NOT NULL"
)
_ATTEMPT_HUMAN_REASON_ONLY_ON_HUMAN = (
    "human_required_reason IS NULL OR outcome = 'REQUIRES_HUMAN'"
)
_ATTEMPT_FAILED_HAS_CODE = (
    "outcome IS NULL OR outcome <> 'FAILED' OR failure_code IS NOT NULL"
)
_ATTEMPT_CODE_ONLY_ON_FAILED = (
    "failure_code IS NULL OR outcome = 'FAILED'"
)
_ATTEMPT_CONFIRMATION_ONLY_ON_SUBMITTED = (
    "confirmation_reference IS NULL OR outcome = 'SUBMITTED'"
)


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
    """Create the Phase 12 application-engine schema, parents before children."""
    op.create_table(
        "application_policies",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("name", _TEXT, nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"),
                  nullable=False),
        sa.Column("mode", _enum("application_automation_mode", *_AUTOMATION_MODES),
                  nullable=False),
        sa.Column("require_approval_before_submission", sa.Boolean(),
                  server_default=sa.text("true"), nullable=False),
        sa.Column("allowed_opportunity_types", postgresql.ARRAY(sa.Text()),
                  server_default=_EMPTY_TEXT_ARRAY, nullable=False),
        sa.Column("minimum_overall_score", sa.Double(), nullable=True),
        sa.Column("dimension_thresholds", _JSONB, server_default=_EMPTY_JSON_ARRAY,
                  nullable=False),
        sa.Column("allow_incomplete_eligibility", sa.Boolean(),
                  server_default=sa.text("false"), nullable=False),
        sa.Column("allow_spontaneous_applications", sa.Boolean(),
                  server_default=sa.text("false"), nullable=False),
        sa.Column("max_applications_per_day", sa.Integer(), nullable=True),
        sa.Column("max_applications_per_week", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("minimum_overall_score BETWEEN 0.0 AND 1.0",
                           name=op.f("ck_application_policies_minimum_overall_score_in_unit_interval")),
        sa.CheckConstraint(
            "allowed_opportunity_types <@ ARRAY['FULL_TIME', 'PART_TIME',"
            " 'STUDENT_JOB', 'INTERNSHIP', 'APPRENTICESHIP', 'WORK_STUDY',"
            " 'GRADUATE', 'TEMPORARY', 'FREELANCE']::text[]",
            name=op.f("ck_application_policies_allowed_opportunity_types_members")),
        sa.CheckConstraint(_POLICY_BRAKE_COHERENT,
                           name=op.f("ck_application_policies_brake_coherent")),
        sa.CheckConstraint(
            "max_applications_per_day IS NULL OR max_applications_per_day >= 0",
            name=op.f("ck_application_policies_max_per_day_non_negative")),
        sa.CheckConstraint(
            "max_applications_per_week IS NULL OR max_applications_per_week >= 0",
            name=op.f("ck_application_policies_max_per_week_non_negative")),
        sa.CheckConstraint(
            "max_applications_per_day IS NULL OR max_applications_per_week IS NULL"
            " OR max_applications_per_day <= max_applications_per_week",
            name=op.f("ck_application_policies_day_within_week")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_application_policies_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_policies")),
    )
    op.create_index("ix_application_policies_user_id", "application_policies",
                    ["user_id"], unique=False)

    op.create_table(
        "application_decisions",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("candidate_profile_id", _UUID, nullable=False),
        sa.Column("opportunity_id", _UUID, nullable=True),
        sa.Column("company_id", _UUID, nullable=True),
        sa.Column("policy_id", _UUID, nullable=True),
        sa.Column("kind", _enum("application_decision_kind", *_DECISION_KINDS),
                  nullable=False),
        sa.Column("reasons", _JSONB, server_default=_EMPTY_JSON_ARRAY,
                  nullable=False),
        sa.Column("confidence", sa.Double(), nullable=True),
        sa.Column("requires_human_review", sa.Boolean(),
                  server_default=sa.text("false"), nullable=False),
        sa.Column("decided_by", _TEXT, nullable=True),
        sa.Column("decided_at", _TIMESTAMPTZ, nullable=False),
        *_timestamps(),
        sa.CheckConstraint("confidence BETWEEN 0.0 AND 1.0",
                           name=op.f("ck_application_decisions_confidence_in_unit_interval")),
        sa.CheckConstraint(_DECISION_TARGET_MATCHES_KIND,
                           name=op.f("ck_application_decisions_target_matches_kind")),
        sa.CheckConstraint("jsonb_array_length(reasons) >= 1",
                           name=op.f("ck_application_decisions_reasons_present")),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"], ["candidate_profiles.id"],
            name="fk_application_decisions_candidate_profile",
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"],
            name=op.f("fk_application_decisions_company_id_companies"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["opportunity_id"], ["opportunities.id"],
            name=op.f("fk_application_decisions_opportunity_id_opportunities"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["application_policies.id"],
            name=op.f("fk_application_decisions_policy_id_application_policies"),
            ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_application_decisions_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_decisions")),
    )
    op.create_index("ix_application_decisions_user_id_decided_at",
                    "application_decisions", ["user_id", "decided_at"], unique=False)
    op.create_index(
        "ix_application_decisions_candidate_profile_id_opportunity_id",
        "application_decisions", ["candidate_profile_id", "opportunity_id"],
        unique=False)

    op.create_table(
        "applications",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("candidate_profile_id", _UUID, nullable=False),
        sa.Column("decision_id", _UUID, nullable=False),
        sa.Column("channel", _enum("application_channel", *_CHANNELS),
                  nullable=False),
        sa.Column("state", _enum("application_state", *_STATES), nullable=False),
        sa.Column("idempotency_key", _TEXT, nullable=False),
        sa.Column("opportunity_id", _UUID, nullable=True),
        sa.Column("company_id", _UUID, nullable=True),
        sa.Column("policy_id", _UUID, nullable=True),
        sa.Column("pinned_documents", _JSONB, server_default=_EMPTY_JSON_ARRAY,
                  nullable=False),
        sa.Column("answers", _JSONB, server_default=_EMPTY_JSON_ARRAY,
                  nullable=False),
        sa.Column("form_fingerprint", _TEXT, nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"),
                  nullable=False),
        sa.Column("correlation_id", _TEXT, nullable=True),
        *_timestamps(),
        sa.CheckConstraint(_APPLICATION_TARGET_SINGULAR,
                           name=op.f("ck_applications_target_singular")),
        sa.CheckConstraint("attempt_count >= 0",
                           name=op.f("ck_applications_attempt_count_non_negative")),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"], ["candidate_profiles.id"],
            name=op.f("fk_applications_candidate_profile_id_candidate_profiles"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"],
            name=op.f("fk_applications_company_id_companies"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["application_decisions.id"],
            name=op.f("fk_applications_decision_id_application_decisions"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["opportunity_id"], ["opportunities.id"],
            name=op.f("fk_applications_opportunity_id_opportunities"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["application_policies.id"],
            name=op.f("fk_applications_policy_id_application_policies"),
            ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_applications_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applications")),
        sa.UniqueConstraint("idempotency_key",
                            name=op.f("uq_applications_idempotency_key")),
    )
    op.create_index("ix_applications_user_id_updated_at", "applications",
                    ["user_id", "updated_at"], unique=False)
    op.create_index("ix_applications_state", "applications", ["state"], unique=False)

    op.create_table(
        "application_events",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("application_id", _UUID, nullable=False),
        sa.Column("event_type", _enum("application_event_type", *_EVENT_TYPES),
                  nullable=False),
        sa.Column("actor", _enum("application_event_actor", *_EVENT_ACTORS),
                  server_default=sa.text("'SYSTEM'"), nullable=False),
        sa.Column("from_state", _enum("application_event_from_state", *_STATES),
                  nullable=True),
        sa.Column("to_state", _enum("application_event_to_state", *_STATES),
                  nullable=True),
        sa.Column("detail", _TEXT, nullable=True),
        sa.Column("reasons", _JSONB, server_default=_EMPTY_JSON_ARRAY,
                  nullable=False),
        sa.Column("correlation_id", _TEXT, nullable=True),
        sa.Column("occurred_at", _TIMESTAMPTZ, nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"],
            name=op.f("fk_application_events_application_id_applications"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_events")),
    )
    op.create_index("ix_application_events_application_id_occurred_at",
                    "application_events", ["application_id", "occurred_at"],
                    unique=False)

    op.create_table(
        "submission_attempts",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("application_id", _UUID, nullable=False),
        sa.Column("attempt_number", sa.SmallInteger(), nullable=False),
        sa.Column("adapter_key", _TEXT, nullable=False),
        sa.Column("outcome", _enum("submission_attempt_outcome", *_SUBMISSION_OUTCOMES),
                  nullable=True),
        sa.Column("detail", _TEXT, nullable=True),
        sa.Column("confirmation_reference", _TEXT, nullable=True),
        sa.Column("human_required_reason",
                  _enum("submission_attempt_human_reason", *_HUMAN_REASONS),
                  nullable=True),
        sa.Column("failure_code",
                  _enum("submission_attempt_failure_code", *_FAILURE_CODES),
                  nullable=True),
        sa.Column("correlation_id", _TEXT, nullable=True),
        sa.Column("started_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("finished_at", _TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        sa.CheckConstraint("attempt_number >= 1",
                           name=op.f("ck_submission_attempts_attempt_number_positive")),
        sa.CheckConstraint(_ATTEMPT_REQUIRES_HUMAN_HAS_REASON,
                           name=op.f("ck_submission_attempts_requires_human_has_reason")),
        sa.CheckConstraint(_ATTEMPT_HUMAN_REASON_ONLY_ON_HUMAN,
                           name=op.f("ck_submission_attempts_human_reason_only_on_human")),
        sa.CheckConstraint(_ATTEMPT_FAILED_HAS_CODE,
                           name=op.f("ck_submission_attempts_failed_has_code")),
        sa.CheckConstraint(_ATTEMPT_CODE_ONLY_ON_FAILED,
                           name=op.f("ck_submission_attempts_code_only_on_failed")),
        sa.CheckConstraint(_ATTEMPT_CONFIRMATION_ONLY_ON_SUBMITTED,
                           name=op.f("ck_submission_attempts_confirmation_only_on_submitted")),
        sa.CheckConstraint("finished_at IS NULL OR finished_at >= started_at",
                           name=op.f("ck_submission_attempts_finished_after_started")),
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"],
            name=op.f("fk_submission_attempts_application_id_applications"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_submission_attempts")),
        sa.UniqueConstraint(
            "application_id", "attempt_number",
            name=op.f("uq_submission_attempts_application_id_attempt_number")),
    )


def downgrade() -> None:
    """Return the schema to revision 0008, children before parents.

    Destructive: every policy, decision, application, event and attempt is deleted.
    Explicit indexes are dropped explicitly; the enum CHECKs, unique constraints and
    foreign keys go with their tables.
    """
    op.drop_table("submission_attempts")
    op.drop_index("ix_application_events_application_id_occurred_at",
                  table_name="application_events")
    op.drop_table("application_events")
    op.drop_index("ix_applications_state", table_name="applications")
    op.drop_index("ix_applications_user_id_updated_at", table_name="applications")
    op.drop_table("applications")
    op.drop_index("ix_application_decisions_candidate_profile_id_opportunity_id",
                  table_name="application_decisions")
    op.drop_index("ix_application_decisions_user_id_decided_at",
                  table_name="application_decisions")
    op.drop_table("application_decisions")
    op.drop_index("ix_application_policies_user_id",
                  table_name="application_policies")
    op.drop_table("application_policies")
