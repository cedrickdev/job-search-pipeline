"""Reading a restored database back to prove the backup is real (§46).

§46 is explicit: *a backup that has never been restored is not considered verified*. Restoring is
only half of that — the other half is looking at what came back. This is the "look": after a dump
has been restored into an isolated scratch database, the service asks this inspector two questions —

- **what migration revision is the restored schema at?** (it must equal the current Alembic head, or
  the dump predates a migration and a restore would come up on the wrong schema);
- **are the representative records present?** (a dump that restored an empty schema is not a backup
  of anything).

It is a thin read-only seam over a short-lived synchronous engine — synchronous because a one-shot
operator/CI check has no event loop to share, and short-lived because it connects to a throwaway
database that is about to be dropped. A `RestoreInspector` protocol lets the service be unit-tested
with a fake that returns canned answers, while `SqlAlchemyRestoreInspector` is what the real restore
smoke test uses against PostgreSQL.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from sqlalchemy import URL, create_engine, text

from backend.app.backup.connection import PostgresConnectionParams

_DRIVER = "postgresql+psycopg"


def _url_for(params: PostgresConnectionParams) -> URL:
    """A SQLAlchemy URL for the scratch database, password included (it never reaches a log)."""
    return URL.create(
        _DRIVER, username=params.user, password=params.password,
        host=params.host, port=params.port, database=params.database)


class RestoreInspector(Protocol):
    """The two reads a restore verification makes against the restored database."""

    def read_migration_revision(self, params: PostgresConnectionParams) -> str | None:
        """The `alembic_version` the restored schema is at, or `None` if unmanaged."""
        ...

    def count_records(self, params: PostgresConnectionParams,
                      tables: Sequence[str]) -> Mapping[str, int]:
        """`table -> row count` for the representative tables, read from the restored database."""
        ...


class SqlAlchemyRestoreInspector:
    """Reads the restored database over a short-lived synchronous engine, then disposes it."""

    def read_migration_revision(self, params: PostgresConnectionParams) -> str | None:
        engine = create_engine(_url_for(params))
        try:
            with engine.connect() as connection:
                return connection.execute(
                    text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
        finally:
            engine.dispose()

    def count_records(self, params: PostgresConnectionParams,
                      tables: Sequence[str]) -> Mapping[str, int]:
        engine = create_engine(_url_for(params))
        counts: dict[str, int] = {}
        try:
            with engine.connect() as connection:
                for table in tables:
                    # The table names are a fixed, in-code representative set (never user input), so
                    # the interpolation cannot carry anything external; still, they are validated by
                    # being a curated constant, not a value off the wire.
                    counts[table] = connection.execute(
                        text(f'SELECT count(*) FROM "{table}"')).scalar_one()  # noqa: S608
        finally:
            engine.dispose()
        return counts
