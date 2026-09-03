"""Engines, session factories and the transaction boundary.

Two engines over one DSN, because Phase 2 has two kinds of caller:

- the repositories are `async`, so they get an `AsyncEngine` — FastAPI is the
  consumer from Phase 4 on and a blocking driver call inside an event loop stalls
  every other request;
- Alembic and the V1 importer are synchronous. Alembic's migration API is sync by
  design, and the importer reads SQLite, which has no async driver worth the
  complexity.

`postgresql+psycopg` is what makes that one URL rather than two: psycopg 3 has
both a sync and an async implementation behind the same dialect, so
`create_engine` and `create_async_engine` accept the identical string. asyncpg
would have forced a second DSN and a second set of credentials to keep in step.

The transaction boundary is `session_scope`, not the repositories. A repository
that commits decides for its caller that a unit of work ends there, which is how
half an import ends up committed; here the repositories flush at most, and one
place commits or rolls back.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import Pool

from backend.app.core.settings import DatabaseSettings


def create_async_database_engine(settings: DatabaseSettings, *,
                                 poolclass: type[Pool] | None = None) -> AsyncEngine:
    """The engine the repositories use.

    `pool_pre_ping` costs one round-trip per checkout and buys immunity to the
    most common local failure: `docker compose restart postgres` leaves every
    pooled connection dead, and without it the next request fails instead of
    reconnecting.

    `poolclass` is injectable so the test suite can pass `NullPool` — a pooled
    connection that outlives an event loop is the classic asyncio/SQLAlchemy
    crash, and one connection per test is cheap against a local container.
    """
    return create_async_engine(settings.url, echo=settings.echo, pool_pre_ping=True,
                               poolclass=poolclass)


def create_sync_database_engine(settings: DatabaseSettings, *,
                                poolclass: type[Pool] | None = None) -> Engine:
    """The engine Alembic and the V1 importer use."""
    return create_engine(settings.url, echo=settings.echo, pool_pre_ping=True,
                         poolclass=poolclass)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory with the two settings this application depends on.

    `expire_on_commit=False`: after a commit, SQLAlchemy would otherwise expire
    every loaded attribute, so reading a mapped object after the unit of work
    closed triggers a lazy refresh — which under asyncio raises rather than
    silently blocking. Mappers hand back frozen domain objects anyway, so nothing
    needs the refresh.

    `autoflush` stays on: a repository that queries after writing must see its own
    pending changes, and that is what the V1 importer does on every row.
    """
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
        session_factory: async_sessionmaker[AsyncSession],
        *, commit: bool = True) -> AsyncIterator[AsyncSession]:
    """One unit of work: commit on success, roll back on any exception.

    This is the only place in the V2 backend that commits. A caller that needs
    two independent units of work opens the scope twice, which is an explicit
    decision rather than an accident of where a repository happened to call
    `commit()`.

    `commit=False` runs the work and then rolls it back. That is what
    `import-v1 --dry-run` is: every row is mapped, every constraint is exercised
    against the real schema, and nothing is kept — a rehearsal that reports what
    the real run would do. Expressed here rather than in the importer so there is
    still exactly one place where a transaction ends.
    """
    async with session_factory() as session:
        try:
            yield session
        except BaseException:
            # `BaseException`, not `Exception`: a cancelled task or a KeyboardInterrupt
            # must not leave a half-written transaction open on the connection that
            # the pool is about to hand to somebody else.
            await session.rollback()
            raise
        if commit:
            await session.commit()
        else:
            await session.rollback()

