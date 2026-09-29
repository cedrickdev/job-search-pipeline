"""The real dump → restore → verify → drop cycle, against PostgreSQL (§46, §74).

The unit tests (`tests/test_v2_backup.py`) prove the service's *orchestration* with fakes. This is
the other half §46 insists on: a backup that has never actually been restored is not verified. So
this test takes a real `pg_dump` of the test database, restores it into an isolated scratch database
with the real `pg_restore`, reads the restored schema back over a real engine, and asserts it landed
at the current Alembic head with the seeded representative row present — then confirms the scratch
database was dropped.

It is the one test that needs the libpq binaries *and* a live PostgreSQL, so it skips (never fails)
when either is missing: a developer with no `pg_dump` on PATH or no database container is not looking
at a broken build. CI, which has both, is where this actually runs (§74).
"""
from __future__ import annotations

import shutil
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, create_engine, text

from backend.app.backup import (
    BackupService,
    PostgresConnectionParams,
    SqlAlchemyRestoreInspector,
    SubprocessPgToolRunner,
)
from backend.app.infrastructure.database.engine import create_session_factory, session_scope
from tests.v2_database import ALEMBIC_INI
from tests.v2_rows import a_user_row

pytestmark = pytest.mark.asyncio

_REQUIRED_BINARIES = ("pg_dump", "pg_restore", "psql")


def _expected_head() -> str:
    head = ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()
    assert head is not None
    return head


def _skip_if_binaries_missing() -> None:
    missing = [name for name in _REQUIRED_BINARIES if shutil.which(name) is None]
    if missing:
        pytest.skip(f"libpq tools not on PATH: {', '.join(missing)} — install PostgreSQL clients")


def _maintenance_engine(source: PostgresConnectionParams):
    """A short-lived sync engine on the `postgres` maintenance DB, to check scratch cleanup."""
    return create_engine(URL.create(
        "postgresql+psycopg", username=source.user, password=source.password,
        host=source.host, port=source.port, database="postgres"))


@pytest_asyncio.fixture
async def seeded_test_database(db_engine):
    """Commit one representative `users` row so a real dump has something to capture, then clear it.

    `db_session`'s rollback cannot be used here: `pg_dump` reads a separate libpq connection and only
    sees *committed* rows. So this commits through a factory and truncates `users` on teardown, like
    the worker/observability persistence fixtures, leaving the shared session schema clean.
    """
    factory = create_session_factory(db_engine)
    async with session_scope(factory) as session:
        session.add(a_user_row(display_name="backup-smoke"))
    try:
        yield
    finally:
        async with db_engine.begin() as connection:
            await connection.execute(text("TRUNCATE users RESTART IDENTITY CASCADE"))


async def test_a_real_dump_restores_into_an_isolated_scratch_at_head_with_its_data(
        postgres_settings, seeded_test_database, tmp_path) -> None:
    _skip_if_binaries_missing()
    head = _expected_head()
    source = PostgresConnectionParams.from_url(postgres_settings.url)
    service = BackupService(
        runner=SubprocessPgToolRunner(
            pg_dump_path="pg_dump", pg_restore_path="pg_restore", psql_path="psql"),
        inspector=SqlAlchemyRestoreInspector(),
        artifact_root=tmp_path,
        retention_days=30,
        expected_revision=head)

    artifact = service.create_backup(source, now=datetime.now(UTC))
    assert artifact.path.exists() and artifact.size_bytes > 0

    verification = service.verify(artifact, source=source)

    # §46: the restored schema is at head, and the representative row survived the round trip.
    assert verification.verified is True
    assert verification.revision == head
    assert verification.record_counts["users"] >= 1

    # The scratch database was dropped — a verification leaves nothing behind on the server.
    engine = _maintenance_engine(source)
    try:
        with engine.connect() as connection:
            leftover = connection.execute(
                text("SELECT count(*) FROM pg_database WHERE datname LIKE :pattern"),
                {"pattern": f"{source.database}_verify_%"}).scalar_one()
    finally:
        engine.dispose()
    assert leftover == 0
