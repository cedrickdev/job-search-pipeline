"""Store one Swiss morning-run schedule per account.

Revision ID: 0029
Revises: 0028
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "morning_schedules",
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("local_time", sa.Time(timezone=False), nullable=False),
        sa.Column("cadence", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("cadence IN ('daily', 'weekdays')",
                           name=op.f("ck_morning_schedules_cadence_valid")),
        sa.CheckConstraint("EXTRACT(SECOND FROM local_time) = 0",
                           name=op.f("ck_morning_schedules_local_time_minutes")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name=op.f("fk_morning_schedules_user_id_users")),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_morning_schedules")),
    )
    op.create_index("ix_morning_schedules_enabled_local_time_user_id",
                    "morning_schedules", ["enabled", "local_time", "user_id"])


def downgrade() -> None:
    op.drop_index("ix_morning_schedules_enabled_local_time_user_id",
                  table_name="morning_schedules")
    op.drop_table("morning_schedules")
