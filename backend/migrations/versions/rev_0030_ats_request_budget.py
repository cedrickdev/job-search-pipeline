"""Durable global request budget for reviewed Swiss ATS employer boards.

Revision ID: 0030
Revises: 0029
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ats_request_budgets",
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("board_token", sa.Text(), nullable=False),
        sa.Column("swiss_date", sa.Date(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("source_key IN ('greenhouse', 'lever')",
                           name=op.f("ck_ats_request_budgets_supported_source")),
        sa.CheckConstraint("length(board_token) BETWEEN 1 AND 128",
                           name=op.f("ck_ats_request_budgets_board_token_length")),
        sa.CheckConstraint("request_count >= 1",
                           name=op.f("ck_ats_request_budgets_positive_request_count")),
        sa.PrimaryKeyConstraint("source_key", "board_token", "swiss_date",
                                name=op.f("pk_ats_request_budgets")),
    )


def downgrade() -> None:
    op.drop_table("ats_request_budgets")
