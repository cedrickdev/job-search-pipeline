"""phase 14 adaptive interview simulator

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-24 12:00:00.000000

Phase 14 turns the static prep sheet into a practice surface with a memory, and gives
its five pieces a home. `interview_sessions` is a user-owned rehearsal against one role;
`interview_questions` is the questions its engine asked, keyed `(session_id, sequence)`
so re-finalizing a turn writes the same row, with `depth`/`follows_sequence`
reconstructing the adaptive follow-up chain. `interview_answers` is the one immutable
answer per question (keyed on the question alone). `interview_answer_evaluations` is the
structured per-axis grade — and, pointedly, carries **no** readiness, probability or
verdict column, because the platform computes readiness later from a whole session's
evaluations, never a provider per answer. `interview_session_summaries` is the closing
coaching artefact, pairing that deterministic readiness (as JSONB) with guarded prose.

The CHECKs are domain validators made physical, so a row written by a migration or by
psql cannot assert what the simulator could never have produced: `ended_at_matches_status`
restates the session lifecycle, `follow_up_shape_coherent` restates that a primary follows
nothing and a follow-up names an earlier question, `transcript_confidence_only_for_voice`
restates that only a spoken answer has a transcription to be unsure about.

Edited after `alembic revision --autogenerate` the way revisions 0002-0011 document: no
application imports for column types, the repetition factored into the helpers below, and
the enum CHECKs created and dropped implicitly with their tables. The schema-drift test
compares the result against `Base.metadata`, which is what keeps that a claim.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_JSONB = postgresql.JSONB()
_EMPTY_JSON_OBJECT = sa.text("'{}'::jsonb")
_EMPTY_JSON_ARRAY = sa.text("'[]'::jsonb")

# The members of the enums this revision introduces, written out. Each is a `StrEnum`
# in `backend.app.domain.interview`; the CHECK behind the column keeps the two lists
# agreeing, and the schema-drift test keeps this file and `models.py` agreeing.
_MODES = ("RECRUITER_HR", "BEHAVIORAL", "TECHNICAL", "HIRING_MANAGER",
          "CASE_STUDY", "FINAL_INTERVIEW")
_STYLES = ("COACHING", "REALISTIC")
_DIFFICULTIES = ("INTRODUCTORY", "INTERMEDIATE", "ADVANCED")
_STATUSES = ("CREATED", "IN_PROGRESS", "COMPLETED", "ABANDONED")
_QUESTION_TYPES = ("BACKGROUND", "MOTIVATION", "BEHAVIORAL", "SITUATIONAL",
                   "TECHNICAL", "CASE", "ROLE_KNOWLEDGE", "CANDIDATE_QUESTIONS")
_ANSWER_FORMATS = ("TEXT", "VOICE")

# Verbatim copies of the constants in `models.py`, so the drift test sees one expression
# on both sides of each invariant.
_SESSION_ENDED_AT_MATCHES_STATUS = (
    "(status IN ('COMPLETED', 'ABANDONED')) = (ended_at IS NOT NULL)")
_QUESTION_FOLLOW_UP_SHAPE = (
    "(depth = 0 AND follows_sequence IS NULL)"
    " OR (depth > 0 AND follows_sequence IS NOT NULL AND follows_sequence < sequence)")
_ANSWER_CONFIDENCE_ONLY_FOR_VOICE = (
    "format <> 'TEXT' OR transcript_confidence IS NULL")


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
    """Create the Phase 14 interview-simulator schema, parents before children."""
    op.create_table(
        "interview_sessions",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("candidate_profile_id", _UUID, nullable=False),
        sa.Column("opportunity_id", _UUID, nullable=False),
        sa.Column("application_id", _UUID, nullable=True),
        sa.Column("mode", _enum("interview_mode", *_MODES), nullable=False),
        sa.Column("style", _enum("interview_session_style", *_STYLES),
                  server_default=sa.text("'COACHING'"), nullable=False),
        sa.Column("difficulty", _enum("interview_difficulty", *_DIFFICULTIES),
                  server_default=sa.text("'INTERMEDIATE'"), nullable=False),
        sa.Column("status", _enum("interview_session_status", *_STATUSES),
                  server_default=sa.text("'CREATED'"), nullable=False),
        sa.Column("language", sa.String(length=2), nullable=True),
        sa.Column("plan", _JSONB, server_default=_EMPTY_JSON_OBJECT, nullable=False),
        sa.Column("title", _TEXT, nullable=False),
        sa.Column("ended_at", _TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        sa.CheckConstraint(_SESSION_ENDED_AT_MATCHES_STATUS,
                           name=op.f("ck_interview_sessions_ended_at_matches_status")),
        sa.CheckConstraint("language ~ '^[a-z]{2}$'",
                           name=op.f("ck_interview_sessions_language_format")),
        sa.ForeignKeyConstraint(
            ["application_id"], ["applications.id"],
            name=op.f("fk_interview_sessions_application_id_applications"),
            ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["candidate_profile_id"], ["candidate_profiles.id"],
            name=op.f("fk_interview_sessions_candidate_profile_id_candidate_profiles"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["opportunity_id"], ["opportunities.id"],
            name=op.f("fk_interview_sessions_opportunity_id_opportunities"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_interview_sessions_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interview_sessions")),
    )
    op.create_index("ix_interview_sessions_user_id_updated_at", "interview_sessions",
                    ["user_id", "updated_at"], unique=False)
    op.create_index("ix_interview_sessions_opportunity_id", "interview_sessions",
                    ["opportunity_id"], unique=False)

    op.create_table(
        "interview_questions",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("session_id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("question_type", _enum("interview_question_type", *_QUESTION_TYPES),
                  nullable=False),
        sa.Column("difficulty", _enum("interview_difficulty", *_DIFFICULTIES),
                  nullable=False),
        sa.Column("prompt", _TEXT, nullable=False),
        sa.Column("topic_label", _TEXT, nullable=True),
        sa.Column("follows_sequence", sa.Integer(), nullable=True),
        sa.Column("depth", sa.SmallInteger(), server_default=sa.text("0"),
                  nullable=False),
        sa.Column("generator_key", _TEXT, nullable=True),
        sa.Column("asked_at", _TIMESTAMPTZ, nullable=False),
        *_timestamps(),
        sa.CheckConstraint("sequence >= 0",
                           name=op.f("ck_interview_questions_sequence_non_negative")),
        sa.CheckConstraint("depth BETWEEN 0 AND 2",
                           name=op.f("ck_interview_questions_depth_within_bounds")),
        sa.CheckConstraint(_QUESTION_FOLLOW_UP_SHAPE,
                           name=op.f("ck_interview_questions_follow_up_shape_coherent")),
        sa.ForeignKeyConstraint(
            ["session_id"], ["interview_sessions.id"],
            name=op.f("fk_interview_questions_session_id_interview_sessions"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_interview_questions_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interview_questions")),
        sa.UniqueConstraint(
            "session_id", "sequence",
            name=op.f("uq_interview_questions_session_id_sequence")),
    )
    op.create_index("ix_interview_questions_user_id", "interview_questions",
                    ["user_id"], unique=False)

    op.create_table(
        "interview_answers",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("question_id", _UUID, nullable=False),
        sa.Column("session_id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("format", _enum("interview_answer_format", *_ANSWER_FORMATS),
                  nullable=False),
        sa.Column("content", _TEXT, nullable=False),
        sa.Column("transcript_confidence", sa.Double(), nullable=True),
        sa.Column("answered_at", _TIMESTAMPTZ, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "transcript_confidence BETWEEN 0.0 AND 1.0",
            name=op.f("ck_interview_answers_transcript_confidence_in_unit_interval")),
        sa.CheckConstraint(
            _ANSWER_CONFIDENCE_ONLY_FOR_VOICE,
            name=op.f("ck_interview_answers_transcript_confidence_only_for_voice")),
        sa.ForeignKeyConstraint(
            ["question_id"], ["interview_questions.id"],
            name=op.f("fk_interview_answers_question_id_interview_questions"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["interview_sessions.id"],
            name=op.f("fk_interview_answers_session_id_interview_sessions"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_interview_answers_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interview_answers")),
        sa.UniqueConstraint(
            "question_id", name=op.f("uq_interview_answers_question_id")),
    )
    op.create_index("ix_interview_answers_session_id", "interview_answers",
                    ["session_id"], unique=False)

    op.create_table(
        "interview_answer_evaluations",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("answer_id", _UUID, nullable=False),
        sa.Column("session_id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("dimensions", _JSONB, server_default=_EMPTY_JSON_ARRAY,
                  nullable=False),
        sa.Column("confidence", sa.Double(), nullable=True),
        sa.Column("strengths", _JSONB, server_default=_EMPTY_JSON_ARRAY,
                  nullable=False),
        sa.Column("improvements", _JSONB, server_default=_EMPTY_JSON_ARRAY,
                  nullable=False),
        sa.Column("suggested_answer", _TEXT, nullable=True),
        sa.Column("evaluator_key", _TEXT, nullable=True),
        sa.Column("evaluated_at", _TIMESTAMPTZ, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "confidence BETWEEN 0.0 AND 1.0",
            name=op.f("ck_interview_answer_evaluations_confidence_in_unit_interval")),
        sa.ForeignKeyConstraint(
            ["answer_id"], ["interview_answers.id"],
            name=op.f("fk_interview_answer_evaluations_answer_id_interview_answers"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["interview_sessions.id"],
            name=op.f("fk_interview_answer_evaluations_session_id_interview_sessions"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_interview_answer_evaluations_user_id_users"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interview_answer_evaluations")),
        sa.UniqueConstraint(
            "answer_id", name=op.f("uq_interview_answer_evaluations_answer_id")),
    )
    op.create_index("ix_interview_answer_evaluations_session_id",
                    "interview_answer_evaluations", ["session_id"], unique=False)

    op.create_table(
        "interview_session_summaries",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("session_id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("readiness", _JSONB, server_default=_EMPTY_JSON_OBJECT,
                  nullable=False),
        sa.Column("headline", _TEXT, nullable=False),
        sa.Column("strengths", _JSONB, server_default=_EMPTY_JSON_ARRAY,
                  nullable=False),
        sa.Column("focus_areas", _JSONB, server_default=_EMPTY_JSON_ARRAY,
                  nullable=False),
        sa.Column("questions_asked", sa.Integer(), nullable=False),
        sa.Column("answers_evaluated", sa.Integer(), nullable=False),
        sa.Column("generator_key", _TEXT, nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "questions_asked >= 0",
            name=op.f("ck_interview_session_summaries_questions_asked_non_negative")),
        sa.CheckConstraint(
            "answers_evaluated >= 0",
            name=op.f("ck_interview_session_summaries_answers_evaluated_non_negative")),
        sa.ForeignKeyConstraint(
            ["session_id"], ["interview_sessions.id"],
            name=op.f("fk_interview_session_summaries_session_id_interview_sessions"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_interview_session_summaries_user_id_users"),
            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interview_session_summaries")),
        sa.UniqueConstraint(
            "session_id", name=op.f("uq_interview_session_summaries_session_id")),
    )
    op.create_index("ix_interview_session_summaries_user_id_created_at",
                    "interview_session_summaries", ["user_id", "created_at"],
                    unique=False)


def downgrade() -> None:
    """Return the schema to revision 0011, children before parents.

    Destructive: every session, question, answer, evaluation and summary is deleted.
    Explicit indexes are dropped explicitly; the enum CHECKs, unique constraints and
    foreign keys go with their tables.
    """
    op.drop_index("ix_interview_session_summaries_user_id_created_at",
                  table_name="interview_session_summaries")
    op.drop_table("interview_session_summaries")
    op.drop_index("ix_interview_answer_evaluations_session_id",
                  table_name="interview_answer_evaluations")
    op.drop_table("interview_answer_evaluations")
    op.drop_index("ix_interview_answers_session_id", table_name="interview_answers")
    op.drop_table("interview_answers")
    op.drop_index("ix_interview_questions_user_id", table_name="interview_questions")
    op.drop_table("interview_questions")
    op.drop_index("ix_interview_sessions_opportunity_id",
                  table_name="interview_sessions")
    op.drop_index("ix_interview_sessions_user_id_updated_at",
                  table_name="interview_sessions")
    op.drop_table("interview_sessions")
