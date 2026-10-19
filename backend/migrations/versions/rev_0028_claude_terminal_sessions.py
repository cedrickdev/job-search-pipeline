"""Persist safe metadata for integrated Claude terminal sessions.

Revision ID: 0028
Revises: 0027
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATE_VALUES = ("CREATING", "ACTIVE", "STOPPING", "TERMINATED", "FAILED", "EXPIRED")
_TERMINAL_STATE = sa.Enum(*_STATE_VALUES, name="claude_terminal_state",
                          native_enum=False, create_constraint=True, length=32)


def upgrade() -> None:
    op.create_table(
        "claude_terminal_sessions",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("state", _TERMINAL_STATE, nullable=False),
        sa.Column("runtime_reference", sa.Text(), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint(
            "(state IN ('TERMINATED', 'FAILED', 'EXPIRED')) = (ended_at IS NOT NULL)",
            name=op.f("ck_claude_terminal_sessions_ended_at_matches_final_state")),
        sa.CheckConstraint(
            "safe_error_code IS NULL OR char_length(safe_error_code) <= 64",
            name=op.f("ck_claude_terminal_sessions_safe_error_code_bounded")),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], ondelete="CASCADE",
            name=op.f("fk_claude_terminal_sessions_owner_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claude_terminal_sessions")),
    )
    op.create_index("ix_claude_terminal_sessions_owner_state", "claude_terminal_sessions",
                    ["owner_user_id", "state"], unique=False)
    op.create_index("ix_claude_terminal_sessions_expires_at", "claude_terminal_sessions",
                    ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_claude_terminal_sessions_expires_at",
                  table_name="claude_terminal_sessions")
    op.drop_index("ix_claude_terminal_sessions_owner_state",
                  table_name="claude_terminal_sessions")
    op.drop_table("claude_terminal_sessions")
