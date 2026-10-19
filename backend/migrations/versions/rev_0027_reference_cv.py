"""Store the active reference CV metadata on the candidate profile.

Revision ID: 0027
Revises: 0026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("candidate_profiles", sa.Column(
        "reference_cv_purpose", sa.String(32), nullable=True))
    op.add_column("candidate_profiles", sa.Column(
        "reference_cv_filename", sa.String(255), nullable=True))
    op.add_column("candidate_profiles", sa.Column(
        "reference_cv_storage_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("candidate_profiles", "reference_cv_storage_key")
    op.drop_column("candidate_profiles", "reference_cv_filename")
    op.drop_column("candidate_profiles", "reference_cv_purpose")
