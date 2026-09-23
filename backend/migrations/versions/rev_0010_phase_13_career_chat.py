"""phase 13 career chat control plane

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-23 12:00:00.000000

Phase 13 turns the career chat into a control plane, and gives its four pieces a home.
`conversations` is a user-owned thread; `chat_messages` is its turns, keyed
`(conversation_id, sequence)` so re-finalizing a turn writes the same rows rather than
duplicating the exchange. `chat_action_proposals` is the persisted heart of "prose has
zero authority": a typed action the model proposed, parsed and validated out of an
assistant turn, that changes nothing until a human confirms it — its `kind` is a column
so "my open submit proposals" is one indexed query, its `action` the whole validated
`ChatAction` as JSONB. `chat_action_executions` is the executor's audit, keyed on the
proposal so a double-confirm collides rather than running the action twice.

The CHECKs are domain validators made physical, so a row written by a migration or by
psql cannot assert what the chat could never have produced. `user_has_no_run` restates
`ChatMessage`'s rule that a candidate's own turn carries no LLM telemetry; the
`sequence`/`ordinal` bounds restate the monotonic counters.

Edited after `alembic revision --autogenerate` the way revisions 0002-0009 document:
no application imports for column types, the repetition factored into the helpers below,
and the enum CHECKs created and dropped implicitly with their tables. The schema-drift
test compares the result against `Base.metadata`, which is what keeps that a claim.
"""
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UUID = sa.UUID()
_TEXT = sa.Text()
_TIMESTAMPTZ = sa.DateTime(timezone=True)
_JSONB = postgresql.JSONB()
_EMPTY_JSON_OBJECT = sa.text("'{}'::jsonb")

# The members of the enums this revision introduces, written out. Each is a
# `StrEnum` in `backend.app.domain.chat`; the CHECK behind the column keeps the two
# lists agreeing, and the schema-drift test keeps this file and `models.py` agreeing.
_MESSAGE_ROLES = ("USER", "ASSISTANT")
_ACTION_KINDS = ("GENERATE_RESUME", "GENERATE_COVER_LETTER", "CREATE_APPLICATION",
                 "PREPARE_APPLICATION", "APPROVE_APPLICATION", "SUBMIT_APPLICATION",
                 "CANCEL_APPLICATION", "SET_SEARCH_RADIUS", "UPDATE_SEARCH_KEYWORDS",
                 "NAVIGATE", "OPEN_INTERVIEW_PREP")
_PROPOSAL_STATUSES = ("PROPOSED", "EXECUTED", "REJECTED", "FAILED", "DISMISSED")
_EXECUTION_OUTCOMES = ("SUCCEEDED", "REJECTED", "FAILED")

# Verbatim copy of the constant in `models.py`, so the drift test sees one expression
# on both sides: a candidate's own turn never carries the telemetry of an LLM call.
_CHAT_MESSAGE_USER_HAS_NO_RUN = (
    "role <> 'USER' OR (llm_run_id IS NULL AND provider_key IS NULL)")


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
    """Create the Phase 13 career-chat schema, parents before children."""
    op.create_table(
        "conversations",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("title", _TEXT, nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False),
        sa.Column("last_message_at", _TIMESTAMPTZ, nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_conversations_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index("ix_conversations_user_id_last_message_at", "conversations",
                    ["user_id", "last_message_at"], unique=False)

    op.create_table(
        "chat_messages",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("role", _enum("chat_message_role", *_MESSAGE_ROLES),
                  nullable=False),
        sa.Column("content", _TEXT, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("llm_run_id", _UUID, nullable=True),
        sa.Column("provider_key", _TEXT, nullable=True),
        *_timestamps(),
        sa.CheckConstraint("sequence >= 0",
                           name=op.f("ck_chat_messages_sequence_non_negative")),
        sa.CheckConstraint(_CHAT_MESSAGE_USER_HAS_NO_RUN,
                           name=op.f("ck_chat_messages_user_has_no_run")),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"],
            name=op.f("fk_chat_messages_conversation_id_conversations"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["llm_run_id"], ["llm_runs.id"],
            name=op.f("fk_chat_messages_llm_run_id_llm_runs"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_chat_messages_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
        sa.UniqueConstraint(
            "conversation_id", "sequence",
            name=op.f("uq_chat_messages_conversation_id_sequence")),
    )
    op.create_index("ix_chat_messages_user_id", "chat_messages", ["user_id"],
                    unique=False)

    op.create_table(
        "chat_action_proposals",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("message_id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", _enum("chat_action_kind", *_ACTION_KINDS), nullable=False),
        sa.Column("action", _JSONB, server_default=_EMPTY_JSON_OBJECT,
                  nullable=False),
        sa.Column("status",
                  _enum("chat_action_proposal_status", *_PROPOSAL_STATUSES),
                  server_default=sa.text("'PROPOSED'"), nullable=False),
        sa.Column("summary", _TEXT, nullable=False),
        *_timestamps(),
        sa.CheckConstraint("ordinal >= 0",
                           name=op.f("ck_chat_action_proposals_ordinal_non_negative")),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"],
            name=op.f("fk_chat_action_proposals_conversation_id_conversations"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["message_id"], ["chat_messages.id"],
            name=op.f("fk_chat_action_proposals_message_id_chat_messages"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_chat_action_proposals_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_action_proposals")),
        sa.UniqueConstraint(
            "message_id", "ordinal",
            name=op.f("uq_chat_action_proposals_message_id_ordinal")),
    )
    op.create_index("ix_chat_action_proposals_user_id_status",
                    "chat_action_proposals", ["user_id", "status"], unique=False)
    op.create_index("ix_chat_action_proposals_conversation_id",
                    "chat_action_proposals", ["conversation_id"], unique=False)

    op.create_table(
        "chat_action_executions",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("proposal_id", _UUID, nullable=False),
        sa.Column("user_id", _UUID, nullable=False),
        sa.Column("outcome",
                  _enum("chat_action_execution_outcome", *_EXECUTION_OUTCOMES),
                  nullable=False),
        sa.Column("detail", _TEXT, nullable=True),
        sa.Column("result_ref", _TEXT, nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["chat_action_proposals.id"],
            name=op.f("fk_chat_action_executions_proposal_id_chat_action_proposals"),
            ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_chat_action_executions_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_action_executions")),
        sa.UniqueConstraint(
            "proposal_id", name=op.f("uq_chat_action_executions_proposal_id")),
    )
    op.create_index("ix_chat_action_executions_user_id", "chat_action_executions",
                    ["user_id"], unique=False)


def downgrade() -> None:
    """Return the schema to revision 0009, children before parents.

    Destructive: every conversation, message, proposal and execution is deleted.
    Explicit indexes are dropped explicitly; the enum CHECKs, unique constraints and
    foreign keys go with their tables.
    """
    op.drop_index("ix_chat_action_executions_user_id",
                  table_name="chat_action_executions")
    op.drop_table("chat_action_executions")
    op.drop_index("ix_chat_action_proposals_conversation_id",
                  table_name="chat_action_proposals")
    op.drop_index("ix_chat_action_proposals_user_id_status",
                  table_name="chat_action_proposals")
    op.drop_table("chat_action_proposals")
    op.drop_index("ix_chat_messages_user_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_conversations_user_id_last_message_at",
                  table_name="conversations")
    op.drop_table("conversations")
