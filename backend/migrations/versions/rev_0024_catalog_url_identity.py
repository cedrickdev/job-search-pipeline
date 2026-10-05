"""Do not merge distinct source IDs merely because a listing URL is shared.

Revision ID: 0024
Revises: 0023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("uq_opportunity_source_records_source_key_normalized_url"),
        "opportunity_source_records", type_="unique")
    op.create_index(
        "uq_opportunity_source_records_unkeyed_url",
        "opportunity_source_records", ["source_key", "normalized_url"],
        unique=True, postgresql_where=sa.text("external_id IS NULL"))


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(sa.text(
        "SELECT source_key, normalized_url FROM opportunity_source_records "
        "WHERE normalized_url IS NOT NULL "
        "GROUP BY source_key, normalized_url HAVING count(*) > 1 LIMIT 1"
    )).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot downgrade 0024 while distinct source IDs share a URL")
    op.drop_index("uq_opportunity_source_records_unkeyed_url",
                  table_name="opportunity_source_records")
    op.create_unique_constraint(
        op.f("uq_opportunity_source_records_source_key_normalized_url"),
        "opportunity_source_records", ["source_key", "normalized_url"])
