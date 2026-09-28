"""`python -m backend.app.cli.run_retention` — sweep legitimately-temporary data (§30-31).

The operational entrypoint for retention. It runs the deployment's `RetentionSettings` against
the three temporary categories — expired sessions, lapsed `READY` export archives, idle provider
sessions — and prints a data-free tally. A worker will schedule this (M8); until then an operator
runs it directly, and `--dry-run` reports what a real sweep *would* remove without deleting a row
or a byte.

It never prints a user's data: the report is counts, and — for any archive whose bytes could not
be purged — the export id and a fixed reason. The sweep is idempotent, so re-running it after a
crash or a redelivered schedule reaches the same state.

Exit codes mirror the other CLIs: 0 clean, 3 finished with failures (some export archive's bytes
could not be purged — its row stays `READY`; investigate the store and re-run).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from backend.app.core.settings import (
    DatabaseSettings,
    ExportSettings,
    RetentionSettings,
)
from backend.app.exports.store import LocalAccountExportStore
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
    session_scope,
)
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyAccountExportRepository,
    SqlAlchemyProviderSessionRepository,
    SqlAlchemySessionRepository,
)
from backend.app.retention import (
    RetentionService,
    RetentionSweepReport,
    format_retention_report,
)

EXIT_OK: Final[int] = 0
EXIT_INCOMPLETE: Final[int] = 3


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.cli.run_retention",
        description="Purge legitimately-temporary data — expired sessions, lapsed export "
                    "archives, idle provider sessions — under the deployment's retention "
                    "policy. Persistent candidate and application history is never touched.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what a sweep would remove, but delete nothing",
    )
    return parser.parse_args(argv)


async def _run_sweep(*, dry_run: bool) -> RetentionSweepReport:
    export_settings = ExportSettings.from_env()
    retention_settings = RetentionSettings.from_env()
    engine = create_async_database_engine(DatabaseSettings.from_env())
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            service = RetentionService(
                sessions=SqlAlchemySessionRepository(session),
                provider_sessions=SqlAlchemyProviderSessionRepository(session),
                exports=SqlAlchemyAccountExportRepository(session),
                export_store=LocalAccountExportStore(Path(export_settings.artifact_root)),
                settings=retention_settings)
            return await service.sweep(now=datetime.now(UTC), dry_run=dry_run)
    finally:
        await engine.dispose()


def _run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = asyncio.run(_run_sweep(dry_run=args.dry_run))
    except Exception as exc:  # a CLI reports a failure, it does not traceback
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE

    print(format_retention_report(report))
    return EXIT_OK if report.is_clean else EXIT_INCOMPLETE


if __name__ == "__main__":  # pragma: no cover - exercised through _run()
    raise SystemExit(_run())
