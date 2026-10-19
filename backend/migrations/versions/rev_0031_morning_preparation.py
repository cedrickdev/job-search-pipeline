"""Owner-scoped checkpoints for reversible Swiss Morning Run preparation.

Revision ID: 0031
Revises: 0030
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_discovery_runs_id_user_id", "discovery_runs",
                                ["id", "user_id"])
    op.create_table(
        "morning_preparations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("discovery_run_id", sa.Uuid(), nullable=False),
        sa.Column("opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("decision_id", sa.Uuid()),
        sa.Column("resume_version_id", sa.Uuid()),
        sa.Column("cover_letter_version_id", sa.Uuid()),
        sa.Column("reason_code", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "discovery_run_id", "opportunity_id",
                                name="pk_morning_preparations"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name="fk_morning_preparations_user_id_users"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"],
                                ondelete="CASCADE",
                                name="fk_morning_preparations_opportunity_id_opportunities"),
        sa.ForeignKeyConstraint(["discovery_run_id", "user_id"],
                                ["discovery_runs.id", "discovery_runs.user_id"],
                                ondelete="CASCADE", name="fk_morning_preparations_owner_run"),
        sa.CheckConstraint(
            "stage IN ('DISCOVERED', 'MATCHED', 'CV_RENDERED', 'DOCUMENTS_RENDERED', "
            "'SKIPPED', 'REQUIRES_HUMAN', 'FAILED')",
            name="ck_morning_preparations_preparation_stage"),
        sa.CheckConstraint(
            "(stage NOT IN ('CV_RENDERED', 'DOCUMENTS_RENDERED') "
            "OR resume_version_id IS NOT NULL) AND "
            "(stage <> 'DOCUMENTS_RENDERED' OR cover_letter_version_id IS NOT NULL) AND "
            "(cover_letter_version_id IS NULL OR resume_version_id IS NOT NULL) AND "
            "(stage NOT IN ('SKIPPED', 'REQUIRES_HUMAN', 'FAILED') "
            "OR reason_code IS NOT NULL)",
            name="ck_morning_preparations_coherent_stage"),
    )
    op.create_index("ix_morning_preparations_user_run", "morning_preparations",
                    ["user_id", "discovery_run_id"])


def downgrade() -> None:
    op.drop_index("ix_morning_preparations_user_run", table_name="morning_preparations")
    op.drop_table("morning_preparations")
    op.drop_constraint("uq_discovery_runs_id_user_id", "discovery_runs", type_="unique")
