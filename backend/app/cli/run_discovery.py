"""Explicit CH morning-run enqueue command; never schedules itself or submits."""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.settings import DatabaseSettings
from backend.app.discovery.requests import countries_in_scope
from backend.app.domain.identifiers import UserId
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
    session_scope,
)
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemySearchProfileRepository,
    SqlAlchemyTaskRunRepository,
)
from backend.app.services.discovery_catalog import DiscoveryEnqueueService
from backend.app.tasks.dispatcher import PersistedTaskDispatcher

MAX_PROFILES_PER_INVOCATION = 100


async def enqueue_for_user(
    factory: async_sessionmaker[AsyncSession], user_id: UserId, *,
    dry_run: bool, trigger: Literal["MANUAL", "MORNING"] = "MORNING",
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Enqueue active CH searches only; a rehearsal rolls back the same writes."""
    if trigger not in {"MANUAL", "MORNING"}:
        raise ValueError("trigger must be MANUAL or MORNING")
    async with session_scope(factory, commit=not dry_run) as session:
        searches = SqlAlchemySearchProfileRepository(session)
        profiles = await searches.list_for_user(
            user_id, active_only=True, limit=MAX_PROFILES_PER_INVOCATION)
        dispatcher = PersistedTaskDispatcher(
            SqlAlchemyTaskRunRepository(session), clock=clock)
        service = DiscoveryEnqueueService(searches, dispatcher, clock=clock)
        count = 0
        for profile in profiles:
            if "CH" not in countries_in_scope(profile):
                continue
            await service.enqueue_discovery(user_id, profile.id, trigger=trigger)
            count += 1
        return count


def _args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.cli.run_discovery",
        description="Queue reviewed Swiss discovery for one account; no applications are sent.")
    parser.add_argument("--user-id", type=UUID, required=True)
    parser.add_argument("--trigger", choices=("MORNING", "MANUAL"), default="MORNING")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and count selected searches without persisting tasks")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _args(argv)

    async def run() -> int:
        engine = create_async_database_engine(DatabaseSettings.from_env())
        try:
            return await enqueue_for_user(
                create_session_factory(engine), UserId(args.user_id),
                dry_run=args.dry_run, trigger=args.trigger)
        finally:
            await engine.dispose()

    count = asyncio.run(run())
    action = "eligible" if args.dry_run else "queued"
    print(f"{count} Swiss saved search(es) {action}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
