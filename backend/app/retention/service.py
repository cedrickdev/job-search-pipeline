"""RetentionService: purge legitimately-temporary data, idempotently (§30-31).

One application service that runs a deployment's retention policy against the data that is
temporary *by design* — and only that data. It exists because a platform that keeps everything
forever is a liability, not a feature: an expired session is an attack surface, an expired export
archive is a copy of a user's data lingering past the window it was promised, and an idle provider
handle is state no one will resume. The sweep removes exactly these three, and nothing else.

Three properties make it safe to schedule (a worker will call it, M8) and safe to re-run:

- **It touches only temporary data (§30).** The categories are fixed in code, each backed by a
  repository primitive that names what it deletes: expired sessions, lapsed `READY` export
  archives, idle provider sessions. There is no path from here to CandidateEvidence, application
  audit history, outcomes or user-created documents — the sweep cannot widen to reach them,
  because it has no method that would.
- **It is idempotent (§31).** Deleting an already-gone row or archive is a no-op that removes
  nothing and raises nothing, so a rerun — after a crash, or a redelivered schedule — reaches the
  same state. An export purge marks the row `EXPIRED` only *after* the store confirms the bytes
  are gone, and `list_expired` returns only `READY` rows, so a purged export drops out of the
  work-list and a rerun does not revisit it.
- **It is fail-safe, not destructive.** The one operation that can genuinely fault is deleting an
  archive's bytes from the store; a fault there is recorded as a typed `RetentionPurgeFailure`
  (naming the export, never its contents) and the row is left `READY`, so a rerun retries rather
  than marking a row `EXPIRED` while its bytes still sit on disk.

The service holds no clock: `now` is passed to `sweep`, exactly as it is to the export and
deletion services, so what counts as expired is fully determined by the caller. `--dry-run`
reports what a real sweep *would* remove, using the repositories' count reads, without deleting a
row or a byte.
"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from backend.app.core.settings import RetentionSettings
from backend.app.domain.identifiers import AccountExportId
from backend.app.exports.store import AccountExportStore
from backend.app.repositories.contracts import (
    AccountExportRepository,
    ProviderSessionRepository,
    SessionRepository,
)

# A capped bulk-delete: remove at most `limit` rows, return how many were removed. The two
# looping sweeps (sessions, provider sessions) share this shape, so `_drain` can loop either.
_BatchDelete = Callable[[int], Awaitable[int]]

# A fixed, secret-free reason for an archive whose bytes could not be purged. The export id is
# recorded alongside it; the reason never carries the archive's contents or its storage path.
_REASON_STORE_FAILED: str = "EXPORT_STORE_DELETE_FAILED"


@dataclass(frozen=True)
class RetentionPurgeFailure:
    """One export archive the sweep could not purge — data-free by design.

    Names the export so an operator can act, and a `reason` that is a fixed sentence, never the
    archive's contents or its storage key. A failure means the row was left `READY`: the sweep
    never marks an export `EXPIRED` while its bytes might still be on disk, so a rerun retries.
    """

    export_id: AccountExportId
    reason: str


@dataclass(frozen=True)
class RetentionSweepReport:
    """The tally one sweep returns — counts and typed failures, never any purged data.

    Each count is what was removed; on a `dry_run` it is what *would* be removed, since nothing is
    written. `failed` mirrors `len(failures)`, the export archives whose bytes could not be
    deleted (their rows stay `READY` for a rerun to retry).
    """

    expired_sessions: int
    stale_provider_sessions: int
    expired_exports: int
    dry_run: bool = False
    failures: tuple[RetentionPurgeFailure, ...] = field(default_factory=tuple)

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def is_clean(self) -> bool:
        """Whether the sweep removed (or would remove) every category with no failure."""
        return self.failed == 0


class RetentionService:
    """Runs the retention policy across all owners, idempotently and fail-safe (§30-31).

    Every collaborator is handed in: the three repositories that hold the retention primitives,
    the export store whose bytes an archive purge deletes, and the `RetentionSettings` that fix
    the one idle window the sweep owns (a provider session's) and the per-statement batch cap. The
    service holds no clock — `now` is passed to `sweep` — so what is expired is determined by its
    inputs, exactly like the export and deletion services.
    """

    def __init__(self, *,
                 sessions: SessionRepository,
                 provider_sessions: ProviderSessionRepository,
                 exports: AccountExportRepository,
                 export_store: AccountExportStore,
                 settings: RetentionSettings) -> None:
        self._sessions = sessions
        self._provider_sessions = provider_sessions
        self._exports = exports
        self._export_store = export_store
        self._settings = settings

    async def sweep(self, *, now: datetime, dry_run: bool = False) -> RetentionSweepReport:
        """Purge every temporary category at `now`, or report what a purge would remove.

        Runs the three sub-sweeps in turn — expired sessions, lapsed export archives, idle
        provider sessions — and returns their tally. On `dry_run` the counts come from the
        repositories' count reads and nothing is written; on a real run each category loops its
        capped primitive until a batch comes back short, so no single statement locks a table.
        """
        expired_sessions = await self._sweep_expired_sessions(now, dry_run)
        expired_exports, failures = await self._sweep_expired_exports(now, dry_run)
        stale_provider = await self._sweep_stale_provider_sessions(now, dry_run)
        return RetentionSweepReport(
            expired_sessions=expired_sessions,
            stale_provider_sessions=stale_provider,
            expired_exports=expired_exports,
            dry_run=dry_run,
            failures=tuple(failures))

    async def _sweep_expired_sessions(self, now: datetime, dry_run: bool) -> int:
        """Delete every session whose absolute lifetime has elapsed, or count them (§30)."""
        if dry_run:
            return await self._sessions.count_expired(now)
        return await self._drain(
            lambda limit: self._sessions.delete_expired(now, limit=limit))

    async def _sweep_stale_provider_sessions(self, now: datetime, dry_run: bool) -> int:
        """Delete provider sessions idle past the policy window, or count them (§30).

        A provider session carries no stored expiry, so the cutoff is computed from the policy:
        `now - provider_session_max_idle`. A row untouched since then is state no one will resume.
        """
        cutoff = now - self._settings.provider_session_max_idle
        if dry_run:
            return await self._provider_sessions.count_stale(cutoff)
        return await self._drain(
            lambda limit: self._provider_sessions.delete_stale(cutoff, limit=limit))
    async def _sweep_expired_exports(
            self, now: datetime, dry_run: bool) -> tuple[int, list[RetentionPurgeFailure]]:
        """Purge each lapsed `READY` archive's bytes, then mark its row `EXPIRED` (§30).

        On `dry_run` returns the count without touching a byte. On a real run it pages the
        oldest-lapsed first; for each export it deletes the archive from the store and, only once
        that succeeds, writes the `EXPIRED` transition — so the row drops out of `list_expired`
        and the loop terminates. A store fault leaves the row `READY` and is recorded as a
        failure, so a rerun retries rather than orphaning bytes behind an `EXPIRED` row.
        """
        if dry_run:
            return await self._exports.count_expired(now), []
        purged = 0
        failures: list[RetentionPurgeFailure] = []
        limit = self._settings.sweep_batch_limit
        while True:
            batch = await self._exports.list_expired(now, limit=limit)
            progressed = False
            for export in batch:
                assert export.storage_key is not None  # READY, guaranteed by list_expired
                try:
                    self._export_store.delete(export.storage_key)
                except Exception:  # a store fault is reported, never fatal to the sweep
                    failures.append(RetentionPurgeFailure(
                        export_id=export.id, reason=_REASON_STORE_FAILED))
                    continue
                await self._exports.upsert(export.expired(as_of=now))
                purged += 1
                progressed = True
            # Terminate when a page is short (nothing more to do) or when a full page made no
            # progress — every row on it failed to purge — so a persistent store fault cannot
            # spin the loop forever.
            if len(batch) < limit or not progressed:
                break
        return purged, failures

    async def _drain(self, delete_batch: _BatchDelete) -> int:
        """Call a capped bulk-delete until a batch comes back short; return the total removed.

        The pattern every looping sweep shares: `delete_batch(limit)` removes at most `limit`
        rows and returns how many, so a total smaller than the cap means the table is drained.
        The cap is the policy's `sweep_batch_limit`, so no single statement locks the table.
        """
        limit = self._settings.sweep_batch_limit
        removed = 0
        while True:
            count = await delete_batch(limit)
            removed += count
            if count < limit:
                return removed


def format_retention_report(report: RetentionSweepReport) -> str:
    """A human-readable, data-free summary of a sweep, for the CLI.

    Lists each category's count and any purge failure by export id and fixed reason — enough for
    an operator to act, and nothing that could leak a user's data.
    """
    verb = "would remove" if report.dry_run else "removed"
    lines = [
        f"retention sweep ({'dry run' if report.dry_run else 'applied'})",
        f"  expired sessions        {verb} {report.expired_sessions}",
        f"  expired export archives {verb} {report.expired_exports}",
        f"  stale provider sessions {verb} {report.stale_provider_sessions}",
        f"  failed                  {report.failed}",
    ]
    if report.failures:
        lines.append("  failures:")
        for failure in report.failures:
            lines.append(f"    {failure.export_id}: {failure.reason}")
    return "\n".join(lines)


__all__ = [
    "RetentionPurgeFailure",
    "RetentionService",
    "RetentionSweepReport",
    "format_retention_report",
]
