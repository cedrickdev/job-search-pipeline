"""Store candidate-supplied contact facts for application forms.

Revision ID: 0026
Revises: 0025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("candidate_profiles", sa.Column("contact_email", sa.String(320), nullable=True))
    op.add_column("candidate_profiles", sa.Column("contact_phone", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("candidate_profiles", "contact_phone")
    op.drop_column("candidate_profiles", "contact_email")
