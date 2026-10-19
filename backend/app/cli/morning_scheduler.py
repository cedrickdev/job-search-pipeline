"""A bounded Swiss wall-clock scheduler over the durable V2 discovery queue."""

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.settings import DatabaseSettings
from backend.app.discovery.ats_board_catalog import ATS_REVIEWED_BOARDS_PATH, load_reviewed_boards
from backend.app.domain.morning_schedule import is_due
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
    session_scope,
)
from backend.app.repositories.morning_schedule import SqlAlchemyMorningScheduleRepository
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemySearchProfileRepository,
    SqlAlchemyTaskRunRepository,
)
from backend.app.services.discovery_catalog import DiscoveryEnqueueService
from backend.app.services.discovery_source_access import SourceAccessPolicy
from backend.app.services.morning_run import MorningRunService
from backend.app.tasks.dispatcher import PersistedTaskDispatcher

_LOG = logging.getLogger(__name__)
_MAX_PAGE_SIZE = 100


async def tick(factory: async_sessionmaker[AsyncSession], now: datetime,
               page_size: int = 100, *, dry_run: bool = False) -> int:
    """Evaluate due accounts, recheck each in its own transaction, enqueue CH work.

    Returns the number of due accounts processed, not a claim that a worker ran or
    that new offers were found. The queue's Swiss-date key coalesces repeat ticks.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise ValueError("page_size must be between 1 and 100")
    source_policy = SourceAccessPolicy.from_env(load_reviewed_boards(ATS_REVIEWED_BOARDS_PATH))
    processed = 0
    offset = 0
    while True:
        async with session_scope(factory, commit=False) as session:
            due_page = await SqlAlchemyMorningScheduleRepository(session).list_due(
                now, limit=page_size, offset=offset)
        if not due_page:
            break
        for scheduled in due_page:
            try:
                async with session_scope(factory, commit=not dry_run) as session:
                    current = await SqlAlchemyMorningScheduleRepository(session).get(
                        scheduled.user_id)
                    if current is None or not is_due(current, now):
                        continue
                    searches = SqlAlchemySearchProfileRepository(session)
                    tasks = SqlAlchemyTaskRunRepository(session)
                    def clock() -> datetime:
                        return now
                    enqueuer = DiscoveryEnqueueService(
                        searches, PersistedTaskDispatcher(tasks, clock=clock), clock=clock,
                        source_policy=source_policy)
                    service = MorningRunService(searches, enqueuer, tasks)
                    await service.enqueue_active_ch(current.user_id, trigger="MORNING")
                    processed += 1
            except Exception as exc:
                # One account's broken search must not stop all other schedules.
                # No owner id or upstream payload goes into the shared log.
                _LOG.warning("morning_schedule_enqueue_failed",
                             extra={"error_type": type(exc).__name__})
        offset += len(due_page)
    _LOG.info("morning_scheduler_tick", extra={"due_accounts": processed,
                                               "dry_run": dry_run})
    return processed


def _args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m backend.app.cli.morning_scheduler")
    parser.add_argument("--once", action="store_true", help="evaluate one tick and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="roll back the queued tasks (requires --once)")
    parser.add_argument("--interval-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    if args.dry_run and not args.once:
        parser.error("--dry-run requires --once")
    if not 60 <= args.interval_seconds <= 3600:
        parser.error("--interval-seconds must be between 60 and 3600")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _args(argv)
    if os.getenv("JOBSEARCH_MORNING_SCHEDULER_ENABLED", "false").lower() != "true" \
            and not args.dry_run:
        if args.once:
            print("Swiss V2 scheduler disabled: reviewed sources must be explicitly enabled",
                  file=sys.stderr)
            return 2
        _LOG.info("morning_scheduler_disabled")
        return 0

    async def run() -> None:
        engine = create_async_database_engine(DatabaseSettings.from_env())
        factory = create_session_factory(engine)
        try:
            if args.once:
                count = await tick(factory, datetime.now(UTC), dry_run=args.dry_run)
                print(f"{count} due Swiss schedule(s) checked")
                return
            while True:
                await tick(factory, datetime.now(UTC))
                await asyncio.sleep(args.interval_seconds)
        finally:
            await engine.dispose()

    asyncio.run(run())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
