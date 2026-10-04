"""Owner-scoped discovery runs and persistent saved-search membership.

Revision ID: 0023
Revises: 0022
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column[datetime], sa.Column[datetime]]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )


def upgrade() -> None:
    op.create_unique_constraint("uq_search_profiles_user_id_id",
                                "search_profiles", ["user_id", "id"])
    op.create_table(
        "discovery_runs",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("search_profile_id", UUID(as_uuid=True), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seen_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_health", JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("warning_codes", JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_runs")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name=op.f("fk_discovery_runs_user_id_users")),
        sa.ForeignKeyConstraint(
            ["user_id", "search_profile_id"],
            ["search_profiles.user_id", "search_profiles.id"],
            ondelete="CASCADE", name="fk_discovery_runs_owner_search"),
        sa.CheckConstraint("seen_count >= 0", name=op.f(
            "ck_discovery_runs_seen_count_non_negative")),
        sa.CheckConstraint("matched_count >= 0", name=op.f(
            "ck_discovery_runs_matched_count_non_negative")),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND finished_at IS NULL) OR "
            "(status <> 'RUNNING' AND finished_at IS NOT NULL)",
            name=op.f("ck_discovery_runs_finish_matches_status")),
        sa.CheckConstraint("trigger IN ('MANUAL', 'MORNING')",
                           name=op.f("ck_discovery_runs_discovery_trigger")),
        sa.CheckConstraint("status IN ('RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED')",
                           name=op.f("ck_discovery_runs_discovery_run_status")),
    )
    op.create_index("ix_discovery_runs_user_id_search_profile_id_started_at",
                    "discovery_runs", ["user_id", "search_profile_id", "started_at"])
    op.create_table(
        "discovery_memberships",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("search_profile_id", UUID(as_uuid=True), nullable=False),
        sa.Column("opportunity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_id", UUID(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_memberships")),
        sa.UniqueConstraint("user_id", "search_profile_id", "opportunity_id",
                            name="uq_discovery_memberships_owner_search_opportunity"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name=op.f("fk_discovery_memberships_user_id_users")),
        sa.ForeignKeyConstraint(
            ["user_id", "search_profile_id"],
            ["search_profiles.user_id", "search_profiles.id"],
            ondelete="CASCADE", name="fk_discovery_memberships_owner_search"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"],
                                ondelete="CASCADE", name=op.f(
                                    "fk_discovery_memberships_opportunity_id_opportunities")),
        sa.ForeignKeyConstraint(["last_run_id"], ["discovery_runs.id"],
                                ondelete="SET NULL", name=op.f(
                                    "fk_discovery_memberships_last_run_id_discovery_runs")),
    )
    op.create_index("ix_discovery_memberships_user_search_seen",
                    "discovery_memberships", ["user_id", "search_profile_id",
                                              "last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_discovery_memberships_user_search_seen",
                  table_name="discovery_memberships")
    op.drop_table("discovery_memberships")
    op.drop_index("ix_discovery_runs_user_id_search_profile_id_started_at",
                  table_name="discovery_runs")
    op.drop_table("discovery_runs")
    op.drop_constraint("uq_search_profiles_user_id_id", "search_profiles",
                       type_="unique")
