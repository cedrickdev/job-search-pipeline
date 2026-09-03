"""enable postgis

Revision ID: 0001
Revises:
Create Date: 2026-09-03 21:46:47.030988

Every V2 table that stores a coordinate declares `geography(Point,4326)`, a type
that does not exist until the extension does — so this runs first, and alone.

It is a migration rather than a line in the database image's entrypoint because
`alembic upgrade head` has to be sufficient by itself (docs/PERSISTENCE.md
§Migrations): the test fixtures, CI and a managed PostgreSQL where nobody gets a
psql superuser shell during provisioning all reach a usable schema the same way.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Install PostGIS.

    `IF NOT EXISTS` because the development image installs the extension into its
    own default database: this must be a silent no-op there and do the real work
    on the test database, which is created empty on purpose so that the migration
    path itself is what the suite exercises.
    """
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")


def downgrade() -> None:
    """Deliberately a no-op.

    By the time this runs, rev 0002 has dropped the tables holding the geography
    columns, so `DROP EXTENSION postgis` would succeed — and take the extension
    out from under anything else in the database that uses it, since PostGIS is
    installed per database, not per schema. An unused extension left installed
    costs nothing; one dropped from under a dependent schema needs a restore.
    """
