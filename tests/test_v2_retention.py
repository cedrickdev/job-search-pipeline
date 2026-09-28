# tests/test_v2_retention.py
"""Retention — purging legitimately-temporary data, idempotently and fail-safe (§30-31, §70).

Phase 16 M7. Retention is the counterweight to "keep everything": data that is temporary *by
design* — an expired session, a lapsed export archive, an idle provider handle — must not linger,
and the platform proves it removes exactly those three and nothing else.

The `§70` test lives here in three shapes:

- **Expired temporary data is removed.** A sweep deletes an expired session, purges a lapsed
  `READY` export's bytes and marks its row `EXPIRED`, and deletes a provider session idle past the
  policy window — counting each honestly.
- **Non-expired data remains.** A fresh session, a still-downloadable export (bytes and all), and
  a recently-used provider session survive the same sweep untouched.
- **Persistent history is never accidentally deleted.** The sweep touches only lapsed `READY`
  exports; a `PENDING` or `FAILED` export — and, structurally, everything the service holds no
  repository for (candidate evidence, applications, outcomes, documents) — is left alone.

And `§31`: a rerun removes nothing and raises nothing, an already-purged archive is a clean no-op,
and a store fault leaves the row `READY` for a retry rather than orphaning bytes behind an
`EXPIRED` row. The service runs over the fakes plus a *real* `LocalAccountExportStore` under
`tmp_path`, so a purge deletes genuine bytes, not a mocked call.
"""
from datetime import timedelta
from uuid import UUID

import pytest

from backend.app.core.settings import RetentionSettings
from backend.app.core.tokens import digest_of, new_token
from backend.app.domain.account_export import AccountExportStatus
from backend.app.domain.identifiers import AccountExportId, new_user_session_id
from backend.app.domain.user import UserSession
from backend.app.exports import LocalAccountExportStore
from backend.app.exports.store import ExportNotFound
from backend.app.retention import (
    RetentionPurgeFailure,
    RetentionService,
    RetentionSweepReport,
    format_retention_report,
)
from tests.v2_builders import (
    CONNECTION,
    NOW,
    USER,
    an_account_export,
    a_provider_session,
)
from tests.v2_fakes import (
    FakeAccountExportRepository,
    FakeProviderSessionRepository,
    FakeSessionRepository,
)

pytestmark = pytest.mark.asyncio

# A sweep instant thirty days after the builders' NOW, so anything born around NOW has lapsed.
SWEEP_AT = NOW + timedelta(days=30)
# A short provider-idle window for the tests: one day, so a session last touched at NOW is idle
# at SWEEP_AT while one touched at SWEEP_AT is fresh.
_IDLE_HOURS = 24

# Distinct export ids, so the store keys and rows never collide across a test's fixtures.
_LAPSED = AccountExportId(UUID("00000000-0000-4000-8000-0000000000f1"))
_FRESH = AccountExportId(UUID("00000000-0000-4000-8000-0000000000f2"))
_FAILED = AccountExportId(UUID("00000000-0000-4000-8000-0000000000f3"))
_PENDING = AccountExportId(UUID("00000000-0000-4000-8000-0000000000f4"))


def _settings(*, batch: int = 500) -> RetentionSettings:
    return RetentionSettings(provider_session_idle_hours=_IDLE_HOURS, sweep_batch_limit=batch)


def _service(*, sessions, provider_sessions, exports, store, batch=500) -> RetentionService:
    return RetentionService(
        sessions=sessions, provider_sessions=provider_sessions, exports=exports,
        export_store=store, settings=_settings(batch=batch))


def _user_session(*, expires_at, user_id=USER) -> UserSession:
    """A live session with distinct digests, its absolute expiry set to `expires_at`."""
    token, csrf = new_token(), new_token()
    issued = min(NOW, expires_at - timedelta(hours=1))
    return UserSession(
        id=new_user_session_id(), user_id=user_id,
        token_digest=digest_of(token), csrf_token_digest=digest_of(csrf),
        issued_at=issued, expires_at=expires_at, last_seen_at=issued)


async def _put_ready_export(exports, store, *, export_id, key, expires_at, completed_at=NOW):
    """Seed a READY export whose real bytes live in `store`, and return the stored export."""
    export = an_account_export(
        id=export_id, status=AccountExportStatus.READY, storage_key=key, byte_size=7,
        completed_at=completed_at, expires_at=expires_at)
    store.put(key, b"archive")
    return await exports.upsert(export)


async def test_the_sweep_removes_expired_temporary_data_and_keeps_the_rest(tmp_path) -> None:
    """§70, clauses 1 & 2: expired temporary data is removed; non-expired data remains.

    An expired session, a lapsed `READY` export and an idle provider session go; a fresh session,
    a still-downloadable export (bytes and all) and a recently-used provider session stay.
    """
    sessions = FakeSessionRepository()
    providers = FakeProviderSessionRepository()
    exports = FakeAccountExportRepository()
    store = LocalAccountExportStore(tmp_path)

    expired = await sessions.upsert(_user_session(expires_at=NOW + timedelta(days=7)))
    fresh = await sessions.upsert(_user_session(expires_at=SWEEP_AT + timedelta(days=7)))
    idle = await providers.upsert(a_provider_session(conversation_key="idle", updated_at=NOW))
    used = await providers.upsert(
        a_provider_session(conversation_key="used", updated_at=SWEEP_AT))
    lapsed = await _put_ready_export(
        exports, store, export_id=_LAPSED, key="exports/u/lapsed.json",
        expires_at=NOW + timedelta(days=7))
    kept = await _put_ready_export(
        exports, store, export_id=_FRESH, key="exports/u/fresh.json",
        expires_at=SWEEP_AT + timedelta(days=7), completed_at=SWEEP_AT)

    report = await _service(sessions=sessions, provider_sessions=providers,
                            exports=exports, store=store).sweep(now=SWEEP_AT)

    assert report == RetentionSweepReport(
        expired_sessions=1, stale_provider_sessions=1, expired_exports=1)
    assert report.is_clean
    # Expired removed, fresh kept.
    assert expired.id not in sessions.sessions and fresh.id in sessions.sessions
    assert idle.id not in providers.sessions and used.id in providers.sessions
    # The lapsed archive's bytes are gone and its row is EXPIRED; the fresh one is untouched.
    with pytest.raises(ExportNotFound):
        store.get(lapsed.storage_key)
    assert (await exports.get(USER, _LAPSED)).status is AccountExportStatus.EXPIRED
    assert store.get(kept.storage_key).content == b"archive"
    assert (await exports.get(USER, _FRESH)).status is AccountExportStatus.READY


async def test_the_sweep_never_touches_persistent_export_history(tmp_path) -> None:
    """§70, clause 3: a PENDING or FAILED export is persistent history the sweep must not remove.

    The export sweep purges only lapsed `READY` rows. A `FAILED` export (an auditable record of a
    failed production) and a `PENDING` one (a request not yet produced) both survive — and the
    service holds no repository for candidate evidence, applications, outcomes or documents at all,
    so there is no path from a sweep to that persistent history.
    """
    sessions = FakeSessionRepository()
    providers = FakeProviderSessionRepository()
    exports = FakeAccountExportRepository()
    store = LocalAccountExportStore(tmp_path)

    failed = an_account_export(
        id=_FAILED, status=AccountExportStatus.FAILED, storage_key=None, byte_size=None,
        expires_at=None, failure_reason="PRODUCTION_FAILED")
    pending = an_account_export(
        id=_PENDING, status=AccountExportStatus.PENDING, storage_key=None, byte_size=None,
        completed_at=None, expires_at=None)
    await exports.upsert(failed)
    await exports.upsert(pending)

    report = await _service(sessions=sessions, provider_sessions=providers,
                            exports=exports, store=store).sweep(now=SWEEP_AT)

    assert report.expired_exports == 0 and report.is_clean
    assert (await exports.get(USER, _FAILED)).status is AccountExportStatus.FAILED
    assert (await exports.get(USER, _PENDING)).status is AccountExportStatus.PENDING


async def test_a_rerun_removes_nothing_and_raises_nothing(tmp_path) -> None:
    """§31: a second sweep reaches the same state — every count zero, no error.

    Sessions and provider sessions are gone after the first sweep; the purged export is `EXPIRED`,
    which `list_expired` excludes, so the rerun finds nothing to do rather than revisiting it.
    """
    sessions = FakeSessionRepository()
    providers = FakeProviderSessionRepository()
    exports = FakeAccountExportRepository()
    store = LocalAccountExportStore(tmp_path)

    await sessions.upsert(_user_session(expires_at=NOW + timedelta(days=7)))
    await providers.upsert(a_provider_session(conversation_key="idle", updated_at=NOW))
    await _put_ready_export(exports, store, export_id=_LAPSED, key="exports/u/lapsed.json",
                            expires_at=NOW + timedelta(days=7))
    service = _service(sessions=sessions, provider_sessions=providers,
                       exports=exports, store=store)

    first = await service.sweep(now=SWEEP_AT)
    second = await service.sweep(now=SWEEP_AT)

    assert first == RetentionSweepReport(
        expired_sessions=1, stale_provider_sessions=1, expired_exports=1)
    assert second == RetentionSweepReport(
        expired_sessions=0, stale_provider_sessions=0, expired_exports=0)


async def test_an_already_purged_archive_is_a_clean_no_op(tmp_path) -> None:
    """§31: a lapsed export whose bytes are already gone still transitions cleanly, no error.

    The store's `delete` is idempotent — a missing key is a no-op, not a raise — so the sweep marks
    the row `EXPIRED` regardless and never fails because an artifact was already deleted.
    """
    exports = FakeAccountExportRepository()
    store = LocalAccountExportStore(tmp_path)
    # A lapsed READY row pointing at bytes the store never held (nothing was `put`).
    orphan = an_account_export(
        id=_LAPSED, status=AccountExportStatus.READY, storage_key="exports/u/gone.json",
        byte_size=7, completed_at=NOW, expires_at=NOW + timedelta(days=7))
    await exports.upsert(orphan)

    report = await _service(sessions=FakeSessionRepository(),
                            provider_sessions=FakeProviderSessionRepository(),
                            exports=exports, store=store).sweep(now=SWEEP_AT)

    assert report.expired_exports == 1 and report.is_clean
    assert (await exports.get(USER, _LAPSED)).status is AccountExportStatus.EXPIRED


async def test_a_dry_run_reports_what_it_would_remove_but_deletes_nothing(tmp_path) -> None:
    """§30: `--dry-run` counts every category truthfully without touching a row or a byte."""
    sessions = FakeSessionRepository()
    providers = FakeProviderSessionRepository()
    exports = FakeAccountExportRepository()
    store = LocalAccountExportStore(tmp_path)

    session = await sessions.upsert(_user_session(expires_at=NOW + timedelta(days=7)))
    provider = await providers.upsert(a_provider_session(conversation_key="idle", updated_at=NOW))
    lapsed = await _put_ready_export(exports, store, export_id=_LAPSED,
                                     key="exports/u/lapsed.json",
                                     expires_at=NOW + timedelta(days=7))

    report = await _service(sessions=sessions, provider_sessions=providers,
                            exports=exports, store=store).sweep(now=SWEEP_AT, dry_run=True)

    assert report == RetentionSweepReport(
        expired_sessions=1, stale_provider_sessions=1, expired_exports=1, dry_run=True)
    # Nothing was actually removed.
    assert session.id in sessions.sessions and provider.id in providers.sessions
    assert (await exports.get(USER, _LAPSED)).status is AccountExportStatus.READY
    assert store.get(lapsed.storage_key).content == b"archive"


async def test_the_sweep_pages_beyond_the_batch_limit(tmp_path) -> None:
    """A sweep drains more rows than its `sweep_batch_limit`, looping until a page comes short."""
    sessions = FakeSessionRepository()
    providers = FakeProviderSessionRepository()
    exports = FakeAccountExportRepository()
    store = LocalAccountExportStore(tmp_path)

    for i in range(5):
        await sessions.upsert(_user_session(expires_at=NOW + timedelta(days=7)))
        await providers.upsert(a_provider_session(conversation_key=f"idle-{i}", updated_at=NOW))
        await _put_ready_export(
            exports, store, export_id=AccountExportId(UUID(f"00000000-0000-4000-8000-00000000e1{i:02d}")),
            key=f"exports/u/lapsed-{i}.json", expires_at=NOW + timedelta(days=7))

    report = await _service(sessions=sessions, provider_sessions=providers, exports=exports,
                            store=store, batch=2).sweep(now=SWEEP_AT)

    assert report == RetentionSweepReport(
        expired_sessions=5, stale_provider_sessions=5, expired_exports=5)
    assert not sessions.sessions and not providers.sessions


class _FaultyStore(LocalAccountExportStore):
    """A store whose `delete` raises for one key, to prove a purge fault is fail-safe."""

    def __init__(self, root, *, failing_key: str) -> None:
        super().__init__(root)
        self._failing_key = failing_key

    def delete(self, storage_key: str) -> bool:
        if storage_key == self._failing_key:
            raise OSError("cannot reach the object store")
        return super().delete(storage_key)


async def test_a_store_fault_is_reported_and_leaves_the_row_ready(tmp_path) -> None:
    """§31: an archive whose bytes cannot be purged is a typed failure; its row stays READY.

    The sweep never marks an export `EXPIRED` while its bytes might still be on disk, so a rerun
    retries. A healthy export in the same batch is still purged, and the report is not clean.
    """
    exports = FakeAccountExportRepository()
    store = _FaultyStore(tmp_path, failing_key="exports/u/lapsed.json")
    faulting = await _put_ready_export(exports, store, export_id=_LAPSED,
                                       key="exports/u/lapsed.json",
                                       expires_at=NOW + timedelta(days=7))
    healthy = await _put_ready_export(exports, store, export_id=_FRESH,
                                      key="exports/u/other.json",
                                      expires_at=NOW + timedelta(days=8))

    report = await _service(sessions=FakeSessionRepository(),
                            provider_sessions=FakeProviderSessionRepository(),
                            exports=exports, store=store).sweep(now=SWEEP_AT)

    assert report.expired_exports == 1  # only the healthy one
    assert not report.is_clean and report.failed == 1
    assert report.failures == (
        RetentionPurgeFailure(export_id=_LAPSED, reason="EXPORT_STORE_DELETE_FAILED"),)
    # The faulting row stays READY (retry next run); the healthy one is EXPIRED and its bytes gone.
    assert (await exports.get(USER, _LAPSED)).status is AccountExportStatus.READY
    assert store.get(faulting.storage_key).content == b"archive"
    assert (await exports.get(USER, _FRESH)).status is AccountExportStatus.EXPIRED
    with pytest.raises(ExportNotFound):
        store.get(healthy.storage_key)
    # The formatted report names the failing export and never leaks its bytes.
    text = format_retention_report(report)
    assert str(_LAPSED) in text and "EXPORT_STORE_DELETE_FAILED" in text
