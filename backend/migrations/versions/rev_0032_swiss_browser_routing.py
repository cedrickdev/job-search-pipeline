"""Scoped browser form identity and durable preparation lane.

Revision ID: 0032
Revises: 0031
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_LANE = (
    "(kind = 'APPLICATION_SUBMISSION' AND lane = 'BROWSER')"
    " OR (kind <> 'APPLICATION_SUBMISSION' AND lane = 'GENERAL')"
)
_NEW_LANE = (
    "(kind IN ('APPLICATION_PREPARATION', 'APPLICATION_SUBMISSION') AND lane = 'BROWSER')"
    " OR (kind NOT IN ('APPLICATION_PREPARATION', 'APPLICATION_SUBMISSION') "
    "AND lane = 'GENERAL')"
)
_OLD_KINDS = (
    "OPPORTUNITY_DISCOVERY", "COMPANY_DISCOVERY", "GEOCODING", "MATCHING",
    "DOCUMENT_GENERATION", "CAREER_ANALYTICS", "ACCOUNT_EXPORT",
    "RETENTION_SWEEP", "APPLICATION_SUBMISSION",
)


def _kind_constraint(kinds: tuple[str, ...]) -> str:
    return "kind IN (" + ", ".join(f"'{kind}'" for kind in kinds) + ")"


def upgrade() -> None:
    op.add_column("applications", sa.Column("form_driver_key", sa.Text(), nullable=True))
    op.add_column("applications", sa.Column("form_capability_id", sa.Text(), nullable=True))
    op.drop_constraint(op.f("ck_task_runs_task_kind"), "task_runs", type_="check")
    op.drop_constraint(op.f("ck_task_runs_lane_matches_kind"), "task_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_task_runs_task_kind"), "task_runs",
        _kind_constraint((*_OLD_KINDS, "APPLICATION_PREPARATION")),
    )
    op.create_check_constraint(op.f("ck_task_runs_lane_matches_kind"), "task_runs",
                               _NEW_LANE)


def downgrade() -> None:
    # Downgrading a queued preparation would silently strand work. Operators must
    # reconcile and remove those exact task rows before rolling back this schema.
    connection = op.get_bind()
    pending = connection.execute(sa.text(
        "SELECT 1 FROM task_runs WHERE kind = 'APPLICATION_PREPARATION' LIMIT 1"
    )).scalar_one_or_none()
    if pending is not None:
        raise RuntimeError("reconcile APPLICATION_PREPARATION tasks before downgrade")
    op.drop_constraint(op.f("ck_task_runs_lane_matches_kind"), "task_runs", type_="check")
    op.drop_constraint(op.f("ck_task_runs_task_kind"), "task_runs", type_="check")
    op.create_check_constraint(op.f("ck_task_runs_task_kind"), "task_runs",
                               _kind_constraint(_OLD_KINDS))
    op.create_check_constraint(op.f("ck_task_runs_lane_matches_kind"), "task_runs",
                               _OLD_LANE)
    op.drop_column("applications", "form_capability_id")
    op.drop_column("applications", "form_driver_key")
