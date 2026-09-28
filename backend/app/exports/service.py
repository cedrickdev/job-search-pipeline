"""AccountExportService: request an export, produce it, and hand it back (§23-25).

The one application service M5 adds. It turns the three pieces around it — the
`AccountExportGatherer` that reads an account's portable data, the `AccountExportStore` that
holds the bytes, and the `AccountExportRepository` that tracks the request's lifecycle — into a
workflow whose ordering keeps two promises physical:

**Gather, serialize, store, then mark READY — in that order.** An export becomes downloadable
only once its bytes are actually stored, because `completed` is the last step and only a
`READY` export answers `is_downloadable`. Any failure along the way marks the request `FAILED`
with a machine reason and stores nothing, so a half-written archive is never presented and the
failure is an auditable record rather than a dangling `PENDING`. The gatherer's own secret
backstop (`_assert_no_secrets`) runs inside `gather`, before a byte is stored, so a payload that
tripped it fails the export instead of reaching the store.

**The owner comes from the session, never the body.** Every method takes `user_id` and hands it
to the gatherer and the repository, both of which read `WHERE user_id = ?`. Another account's
export id reads as absent and raises the same `AccountExportNotFound` a never-created id does, so
one account can neither produce nor download another's export by guessing an id (§23, §68).

Production is synchronous here so M5 ships a working feature end to end; the request/produce
split is deliberate, so the M8 worker can persist a `PENDING` request on the request path and run
`produce` off a queue without this service changing shape.
"""
import json
from datetime import datetime, timedelta

from backend.app.core.settings import ExportSettings
from backend.app.domain.account_export import (
    ACCOUNT_EXPORT_SCHEMA_VERSION,
    AccountExport,
    AccountExportStatus,
)
from backend.app.domain.identifiers import (
    AccountExportId,
    UserId,
    new_account_export_id,
)
from backend.app.exports.gatherer import AccountExportGatherer, ExportContainsSecret
from backend.app.exports.store import ACCOUNT_EXPORT_MEDIA_TYPE, AccountExportStore, StoredExport
from backend.app.repositories.contracts import DEFAULT_LIMIT, AccountExportRepository

# The machine reasons a production failure is recorded under (§24, domain `ReasonCode`). Distinct
# so an operator reading a `FAILED` export can tell a secret-guard trip — a bug that must be
# fixed at its source — apart from an ordinary gather/store fault.
_REASON_SECRET_GUARD: str = "SECRET_GUARD_TRIPPED"  # noqa: S105 — a machine reason code, not a credential
_REASON_PRODUCTION_FAILED: str = "PRODUCTION_FAILED"


class AccountExportNotFound(Exception):
    """No export is stored under that id for this account.

    User-owned, so "no such export" and "not yours" are one condition, for the reason
    `MatchEvaluationRepository.get` gives: a caller that could tell them apart could enumerate
    another account's exports by id. The API maps it to a 404 that says neither which.
    """


class AccountExportNotReady(Exception):
    """The export exists but has no downloadable archive right now.

    Distinct from `AccountExportNotFound`: the export is this user's, but it is `PENDING`,
    `FAILED`, or `EXPIRED`/lapsed — there is nothing to stream. The API maps it to a 409, not a
    404, because the resource is real and the state is either temporary (produce it, poll it) or
    terminal for a reason the export's own status explains.
    """


class AccountExportService:
    """Request, produce, read and download an account's data exports (§23-25).

    Every collaborator is handed in: the gatherer (user-scoped reads only), the store (a blob
    sink), the repository (the lifecycle rows), and the `ExportSettings` that fix the retention
    window. The service holds no clock and no owner of its own — `now` and `user_id` are passed on
    every call — so what an export contains and when it lapses are fully determined by its inputs.
    """

    def __init__(self, *,
                 gatherer: AccountExportGatherer,
                 exports: AccountExportRepository,
                 store: AccountExportStore,
                 settings: ExportSettings) -> None:
        self._gatherer = gatherer
        self._exports = exports
        self._store = store
        self._settings = settings

    async def create(self, user_id: UserId, *, now: datetime) -> AccountExport:
        """Request an export and produce it now, returning the finished `READY`/`FAILED` record.

        The synchronous path M5 ships: `request` persists a `PENDING` row, then `produce` gathers,
        serializes, stores and marks it `READY` — or `FAILED` if anything went wrong. A worker
        (M8) would instead call `request`, enqueue the id, and call `produce` off the queue; the
        split is why that change touches the wiring, not this service.
        """
        export = await self.request(user_id, now=now)
        return await self.produce(user_id, export.id, now=now)

    async def request(self, user_id: UserId, *, now: datetime) -> AccountExport:
        """Persist a fresh `PENDING` export for this account and return it.

        A random id, because an export is an event a user asks for at an instant, not an
        idempotent fact — asking twice is two exports. Carries the current schema version so the
        archive it later produces is self-describing.
        """
        export = AccountExport(
            id=new_account_export_id(),
            user_id=user_id,
            status=AccountExportStatus.PENDING,
            schema_version=ACCOUNT_EXPORT_SCHEMA_VERSION,
            created_at=now,
            updated_at=now,
        )
        return await self._exports.upsert(export)

    async def produce(self, user_id: UserId, export_id: AccountExportId, *,
                      now: datetime) -> AccountExport:
        """Gather, serialize and store the archive, marking the export `READY` — or `FAILED`.

        Idempotent on a non-`PENDING` export: a request already produced (or already failed) is
        returned unchanged rather than gathered a second time, so a retried produce after a
        successful one is a no-op. Any failure — including the gatherer's secret backstop — stores
        nothing and records a `FAILED` export with a machine reason, so a fault is auditable and a
        half-written archive is never presented.
        """
        export = await self._load(user_id, export_id)
        if export.status is not AccountExportStatus.PENDING:
            return export
        try:
            content = self._serialize(await self._gatherer.gather(user_id, generated_at=now))
            storage_key = self._store.key_for(user_id, export_id)
            self._store.put(storage_key, content, media_type=ACCOUNT_EXPORT_MEDIA_TYPE)
            expires_at = now + timedelta(hours=self._settings.retention_hours)
            ready = export.completed(
                storage_key=storage_key, byte_size=len(content),
                expires_at=expires_at, as_of=now)
            return await self._exports.upsert(ready)
        except ExportContainsSecret:
            failed = export.failed(reason=_REASON_SECRET_GUARD, as_of=now)
            return await self._exports.upsert(failed)
        except Exception:
            failed = export.failed(reason=_REASON_PRODUCTION_FAILED, as_of=now)
            return await self._exports.upsert(failed)

    async def get(self, user_id: UserId, export_id: AccountExportId) -> AccountExport:
        """One export's lifecycle record, or raise `AccountExportNotFound`.

        A pure read, scoped by owner: another account's id reads as absent and raises the same
        error a never-created id does, so neither can be told from the other.
        """
        return await self._load(user_id, export_id)

    async def list(self, user_id: UserId, *,
                   limit: int = DEFAULT_LIMIT) -> tuple[AccountExport, ...]:
        """This account's exports, most recently updated first."""
        return await self._exports.list_for_user(user_id, limit=limit)

    async def download(self, user_id: UserId, export_id: AccountExportId, *,
                       now: datetime) -> StoredExport:
        """The archive bytes for a download, or raise if it is not downloadable now.

        `AccountExportNotFound` when the export is not this account's; `AccountExportNotReady`
        when it is real but not `READY`-and-unexpired at `now` — the fail-safe expiry check runs
        in the domain, so an archive past its window reads as not downloadable even before a
        retention sweep has flipped its status. An `ExportNotFound` from the store (the row points
        at bytes the store no longer holds) is left to propagate: that is a storage fault, not a
        client error, and must not be disguised as "not ready".
        """
        export = await self._load(user_id, export_id)
        if not export.is_downloadable(now):
            raise AccountExportNotReady(str(export_id))
        assert export.storage_key is not None  # guaranteed by is_downloadable
        return self._store.get(export.storage_key)

    async def _load(self, user_id: UserId,
                    export_id: AccountExportId) -> AccountExport:
        export = await self._exports.get(user_id, export_id)
        if export is None:
            raise AccountExportNotFound(str(export_id))
        return export

    @staticmethod
    def _serialize(payload: dict[str, object]) -> bytes:
        """The export payload as stable, human-readable UTF-8 JSON bytes.

        `sort_keys` so a re-produced export of unchanged data is byte-identical (a stable
        `byte_size`, and a diffable archive); `ensure_ascii=False` so a candidate's name or a
        posting's title keeps its own characters rather than being escaped into noise.
        """
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        return text.encode("utf-8")
