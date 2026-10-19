"""Clean expired, isolated Claude terminal sessions without exposing runtime metadata.

The command uses the normal database lifecycle rows plus the private terminal-manager
boundary.  It never receives an Anthropic credential, Docker socket, prompt, terminal output,
or workspace path.  Re-running is safe: a session becomes ``EXPIRED`` only after the manager
acknowledges that its one labelled runtime has stopped.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.claude_terminal.service import ClaudeTerminalSessionService
from backend.app.core.settings import ClaudeTerminalSettings, DatabaseSettings
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
    session_scope,
)
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyClaudeTerminalSessionRepository,
)
from backend.app.services.claude_terminal import (
    ClaudeTerminalService,
    InternalTerminalManagerClient,
)

EXIT_OK: Final[int] = 0
EXIT_INCOMPLETE: Final[int] = 3


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.cli.reap_claude_terminals",
        description="Stop one bounded page of expired isolated Claude terminal sessions.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report the bounded number of due sessions without contacting terminal-manager",
    )
    return parser.parse_args(argv)


def _service(session: AsyncSession, settings: ClaudeTerminalSettings) -> ClaudeTerminalService:
    if not settings.enabled or not settings.manager_url or not settings.manager_token:
        raise RuntimeError("integrated Claude terminal is not enabled")
    lifecycle = ClaudeTerminalSessionService(
        SqlAlchemyClaudeTerminalSessionRepository(session),
        session_ttl=timedelta(minutes=settings.session_ttl_minutes),
    )
    return ClaudeTerminalService(
        lifecycle,
        InternalTerminalManagerClient(base_url=settings.manager_url, token=settings.manager_token),
    )


async def _run_reaper(*, dry_run: bool) -> tuple[int, int]:
    settings = ClaudeTerminalSettings.from_env()
    engine = create_async_database_engine(DatabaseSettings.from_env())
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            service = _service(session, settings)
            if dry_run:
                return await service.count_due(now=datetime.now(UTC)), 0
            report = await service.reap_due(now=datetime.now(UTC))
            return report.expired, report.failed
    finally:
        await engine.dispose()


def _run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        expired, failed = asyncio.run(_run_reaper(dry_run=args.dry_run))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE
    action = "would stop" if args.dry_run else "stopped"
    mode = "dry run" if args.dry_run else "applied"
    print(f"Claude terminal cleanup ({mode}): {action} {expired}")
    print(f"failed: {failed}")
    return EXIT_OK if failed == 0 else EXIT_INCOMPLETE


if __name__ == "__main__":  # pragma: no cover - exercised through _run()
    raise SystemExit(_run())
