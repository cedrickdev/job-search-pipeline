import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

from pipeline import paths
from pipeline.db import connect, init_db

# A tracked, fully synthetic CV library. The real one (cv/base_cv.yaml) carries
# the operator's identity, is gitignored, and is therefore absent from a clean
# clone — 31 tests used to fail there with FileNotFoundError, and the six that
# assert on the profile's DENSITY could not be satisfied by the sparse
# cv/base_cv.template.yaml either. See docs/V1_BASELINE.md §9.
BASE_CV_FIXTURE = Path(__file__).parent / "fixtures" / "base_cv.yaml"


@pytest.fixture(autouse=True)
def synthetic_base_cv(monkeypatch):
    """Point every test at the synthetic CV library, never the operator's.

    Autouse and unconditional on purpose: if it fell back to the real profile
    when present, the suite would assert different things on a developer's
    machine than in CI, which is the reproducibility bug this closes. To check
    a freshly onboarded real profile instead, run `python -m pipeline.cv_render`
    (prints pages and fill for cv/base_cv.yaml in both languages).
    """
    monkeypatch.setattr(paths, "BASE_CV_PATH", BASE_CV_FIXTURE)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def api_client(tmp_path):
    from fastapi.testclient import TestClient
    from server.app import create_app
    db_path = tmp_path / "api.db"
    settings_path = tmp_path / "settings.json"
    bootstrap = connect(db_path)
    init_db(bootstrap)
    bootstrap.close()
    with TestClient(create_app(db_path=db_path, settings_path=settings_path)) as c:
        c.db_path = db_path
        c.settings_path = settings_path
        yield c


@pytest.fixture
def db_conn(api_client):
    """A raw connection to the SAME database the app under test reads/writes.

    Phase 8/9 route tests seed rows via this fixture and then assert through
    api_client. It MUST bind to api_client.db_path — NOT the existing `conn`
    fixture, which opens a different file (tmp_path/"test.db") the app never sees.
    """
    c = connect(api_client.db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    yield c
    c.close()


# --------------------------------------------------------------------------
# Phase 2 — the V2 PostgreSQL/PostGIS fixtures.
#
# Everything below is imported lazily, inside the fixtures. The V1 suite is 460
# tests that must keep running on a machine with no container up, and a module
# that imported SQLAlchemy or `backend.app` at collection time would make every
# one of them depend on the V2 stack being importable.
#
# The session/function split is deliberate: the schema is built once per session
# by *synchronous* Alembic, and each test gets a *function*-scoped async session.
# pytest-asyncio's fixture loop scope is pinned to `function` (pyproject.toml), so
# a session-scoped async fixture would be bound to an event loop that no longer
# exists by the second test.
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_settings():
    """The test database's settings, or a skip explaining how to get one."""
    from backend.app.core.settings import DatabaseSettings
    from tests.v2_database import unreachable_reason

    settings = DatabaseSettings.for_tests()
    reason = unreachable_reason(settings)
    if reason is not None:
        pytest.skip(reason)
    return settings


@pytest.fixture(scope="session")
def migrated_database(postgres_settings):
    """A test database at `head`, built from an empty schema by the migrations.

    Once per session: `alembic upgrade head` costs about a second, and per-test
    isolation is provided by the transaction in `db_session` instead — which is
    both faster and a stronger guarantee than recreating tables.
    """
    from backend.app.infrastructure.database.engine import create_sync_database_engine
    from tests.v2_database import reset_schema

    engine = create_sync_database_engine(postgres_settings)
    try:
        reset_schema(engine)
    finally:
        engine.dispose()
    return postgres_settings


@pytest_asyncio.fixture
async def db_engine(migrated_database):
    """A function-scoped `AsyncEngine` with no pool.

    `NullPool` because a pooled connection outliving its event loop is the
    classic asyncio/SQLAlchemy crash, and each test here gets its own loop.
    """
    from sqlalchemy.pool import NullPool

    from backend.app.infrastructure.database.engine import create_async_database_engine

    engine = create_async_database_engine(migrated_database, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    """An `AsyncSession` whose every write is rolled back when the test ends.

    The session runs inside a transaction this fixture opened and will abort, and
    `join_transaction_mode="create_savepoint"` means the session's own
    `commit()` — which `session_scope` calls — commits a savepoint instead of the
    outer transaction. So a test can exercise the real commit path and still
    leave no row behind, which is what makes the schema safe to build once per
    session.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(bind=connection,
                               join_transaction_mode="create_savepoint",
                               expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
