# tests/v2_database.py
"""What a database-backed V2 test needs before it can assert anything.

Three helpers, and one rule that shapes all of them: **the schema under test is
built by Alembic, never by `metadata.create_all()`**. A suite that creates its
tables from the mapped classes proves the mappers agree with themselves and says
nothing about the migrations a deployment will actually run — and Phase 2's
acceptance criterion is `alembic upgrade head` against an empty database.

`reset_schema` therefore drops `public` outright, PostGIS included, so revision
0001 has to reinstall the extension every time.

Kept out of `conftest.py` because the migration tests use these directly: they
need to drop and rebuild the schema mid-test, which is the one thing a fixture
must not do behind another test's back.
"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, text

from backend.app.core.settings import DatabaseSettings
from backend.app.infrastructure.database.engine import create_sync_database_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# Printed by every skip, so the reader is told how to run these tests rather than
# left with "1 skipped".
SKIP_HINT = ("PostgreSQL/PostGIS is not reachable at {url}. Start it with "
             "`docker compose up -d postgres`, or point TEST_DATABASE_URL at "
             "another instance.")

# What a full `alembic downgrade base` is allowed to leave behind: Alembic's own
# bookkeeping table, and PostGIS's `spatial_ref_sys` — 8500 rows of projection
# definitions that belong to the extension and that revision 0001 deliberately
# does not drop.
EXPECTED_AFTER_DOWNGRADE = frozenset({"alembic_version", "spatial_ref_sys"})


def alembic_config(connection: Connection) -> Config:
    """An Alembic config that migrates the caller's open connection.

    `configure_logger=False` matters more than it looks: `env.py` calls
    `fileConfig()` otherwise, which reconfigures the root logger and tears down
    pytest's capture for every test that runs afterwards.
    """
    config = Config(str(ALEMBIC_INI))
    config.attributes["connection"] = connection
    config.attributes["configure_logger"] = False
    return config


def upgrade_to_head(connection: Connection) -> None:
    """Run every revision on an open connection."""
    command.upgrade(alembic_config(connection), "head")


def downgrade_to_base(connection: Connection) -> None:
    """Unwind every revision, back to an empty schema."""
    command.downgrade(alembic_config(connection), "base")


def reset_schema(engine: Engine) -> None:
    """Return the database to "empty", then migrate it to head.

    `DROP SCHEMA public CASCADE` is the whole point: it removes the tables, the
    enum CHECKs, `alembic_version` *and* the PostGIS extension, so what the next
    line exercises is the same cold start as a fresh deployment.

    Only ever called against the test database. `DatabaseSettings.for_tests`
    resolves a separate URL from the development one for exactly this reason.
    """
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    with engine.begin() as connection:
        upgrade_to_head(connection)


def unreachable_reason(settings: DatabaseSettings) -> str | None:
    """`None` when the test database answers, otherwise why it did not.

    Used to skip rather than fail: a developer with no container running is not
    looking at a broken build, and the message says what to start. CI has no
    excuse, which is why the workflow's database job asserts these tests ran.
    """
    engine = create_sync_database_engine(settings)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        # Broad by intent: every way of not having a database — refused, no such
        # database, bad password, missing driver — is the same skip.
        return f"{SKIP_HINT.format(url=settings.redacted_url)} ({type(exc).__name__})"
    finally:
        engine.dispose()
    return None
