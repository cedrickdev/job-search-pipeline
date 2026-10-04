"""Allow multiple source observations for one canonical opportunity.

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _legacy_normalized_url(url: str) -> str | None:
    """Frozen normalization compatible with the catalog writer at revision 0022."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    port = parsed.port
    if port and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    query = urlencode(sorted(
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
    ))
    return urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip("/") or "/",
                       query, ""))


def upgrade() -> None:
    op.add_column("opportunities", sa.Column("canonical_target", sa.Text(), nullable=True))
    op.create_unique_constraint(
        op.f("uq_opportunities_canonical_target"), "opportunities", ["canonical_target"])
    op.add_column(
        "opportunity_source_records",
        sa.Column("normalized_url", sa.Text(), nullable=True),
    )
    connection = op.get_bind()
    # Keep every legacy record. If multiple old rows normalize to the same URL,
    # only the first gets a lookup key; all source URLs remain in source_url.
    seen: set[tuple[str, str]] = set()
    rows = connection.execute(sa.text(
        "SELECT id, source_key, source_url FROM opportunity_source_records "
        "WHERE source_url IS NOT NULL ORDER BY id"
    )).all()
    for source_id, source_key, source_url in rows:
        normalized = _legacy_normalized_url(source_url)
        if normalized is None or (source_key, normalized) in seen:
            continue
        seen.add((source_key, normalized))
        connection.execute(sa.text(
            "UPDATE opportunity_source_records SET normalized_url = :url WHERE id = :id"
        ), {"url": normalized, "id": source_id})
    op.create_unique_constraint(
        op.f("uq_opportunity_source_records_source_key_normalized_url"),
        "opportunity_source_records", ["source_key", "normalized_url"],
    )
    op.drop_constraint(
        op.f("uq_opportunity_source_records_opportunity_id"),
        "opportunity_source_records", type_="unique",
    )


def downgrade() -> None:
    connection = op.get_bind()
    duplicated = connection.execute(sa.text(
        "SELECT opportunity_id FROM opportunity_source_records "
        "GROUP BY opportunity_id HAVING count(*) > 1 LIMIT 1"
    )).scalar_one_or_none()
    if duplicated is not None:
        raise RuntimeError(
            "Cannot downgrade 0022 while an opportunity has multiple source records"
        )
    op.create_unique_constraint(
        op.f("uq_opportunity_source_records_opportunity_id"),
        "opportunity_source_records", ["opportunity_id"],
    )
    op.drop_constraint(
        op.f("uq_opportunity_source_records_source_key_normalized_url"),
        "opportunity_source_records", type_="unique",
    )
    op.drop_column("opportunity_source_records", "normalized_url")
    op.drop_constraint(
        op.f("uq_opportunities_canonical_target"), "opportunities", type_="unique",
    )
    op.drop_column("opportunities", "canonical_target")
