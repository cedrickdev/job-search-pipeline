# tests/test_v2_account_export.py
"""Account data export — a portable, secret-free copy of one account's own data (§23-25).

Phase 16 M5. The feature is four layers, and each is proved where it lives:

- **The domain (`AccountExport`)** — a closed state machine whose validator refuses any field
  combination a status does not allow, and whose availability (`is_downloadable`/`is_expired`)
  is derived from an instant, never a stored flag, so an archive stops being offered the moment
  its window lapses (fail-safe, ahead of the retention sweep).
- **The store (`LocalAccountExportStore`)** — a dumb blob sink that round-trips bytes, is
  idempotent on delete, has no side effect at construction, and refuses a key that escapes its
  root (the traversal guard `LocalDocumentArtifactStore` also carries).
- **The gatherer (`AccountExportGatherer`)** — user-isolated by construction (§68: every read is
  `WHERE user_id = ?`) and secret-free by three disciplines plus a fail-closed backstop (§73:
  `password_hash` excluded, an LLM connection reduced to `has_api_key` metadata, and
  `_assert_no_secrets` raising on any denylisted key).
- **The service (`AccountExportService`)** — request → produce → download, where an export is
  downloadable only once its bytes are stored, any fault marks it `FAILED` with a machine reason
  and stores nothing, and every read is owner-scoped so another account's id is indistinguishable
  from a never-created one.

The route layer is exercised over the real app through `api_harness`: create is a 201 that
produces synchronously, a download of a `READY` export is its bytes, a foreign or missing id is a
404, and a not-`READY` export is a 409. The end-to-end archive a download returns is where §68 and
§73 are asserted against the actual produced bytes, not a hand-built payload.
"""
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.app.core.settings import (
    DEFAULT_EXPORT_ARTIFACT_ROOT,
    DEFAULT_EXPORT_RETENTION_HOURS,
    EXPORT_ARTIFACT_ROOT_VARIABLE,
    EXPORT_RETENTION_HOURS_VARIABLE,
    ExportSettings,
)
from backend.app.domain.account_export import (
    ACCOUNT_EXPORT_SCHEMA_VERSION,
    AccountExport,
    AccountExportStatus,
)
from backend.app.domain.identifiers import AccountExportId, SearchProfileId, UserId
from backend.app.exports import (
    AccountExportNotFound,
    AccountExportNotReady,
    AccountExportService,
    ExportNotFound,
    LocalAccountExportStore,
)
from backend.app.exports.gatherer import (
    _SECRET_KEY_DENYLIST,
    ExportContainsSecret,
    _assert_no_secrets,
    _connection_metadata,
)
from tests.v2_api import EMAIL, NOW, OTHER_EMAIL, api_harness
from tests.v2_builders import (
    OTHER_USER,
    USER,
    a_search_profile,
    an_account_export,
    an_llm_connection,
)
from tests.v2_fakes import FakeAccountExportRepository

# Instants for the pure-domain tests, independent of any harness clock: a request made, its
# archive completed a day later, and a retention window that ends a week after that.
CREATED = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
COMPLETED = datetime(2026, 5, 2, 9, 0, tzinfo=UTC)
EXPIRES = COMPLETED + timedelta(hours=DEFAULT_EXPORT_RETENTION_HOURS)

# Two export ids the harness tests seed foreign/pending rows under, distinct from the builder's
# `ACCOUNT_EXPORT` (…111) so a seed never collides with a produced export.
FOREIGN_EXPORT = AccountExportId(UUID("00000000-0000-4000-8000-000000000112"))
PENDING_EXPORT = AccountExportId(UUID("00000000-0000-4000-8000-000000000113"))


def _pending() -> AccountExport:
    """A freshly requested export — no artifact, no completion, no expiry, no failure."""
    return AccountExport(
        id=AccountExportId(UUID("00000000-0000-4000-8000-0000000000e1")),
        user_id=USER, status=AccountExportStatus.PENDING,
        schema_version=ACCOUNT_EXPORT_SCHEMA_VERSION, created_at=CREATED, updated_at=CREATED)


def _ready(**overrides) -> AccountExport:
    """A produced, downloadable export, its artifact stored and its window set."""
    fields: dict[str, object] = {
        "id": AccountExportId(UUID("00000000-0000-4000-8000-0000000000e1")),
        "user_id": USER, "status": AccountExportStatus.READY,
        "schema_version": ACCOUNT_EXPORT_SCHEMA_VERSION,
        "storage_key": "exports/u/e.json", "byte_size": 2048,
        "completed_at": COMPLETED, "expires_at": EXPIRES,
        "created_at": CREATED, "updated_at": COMPLETED,
    }
    fields.update(overrides)
    return AccountExport(**fields)


# --------------------------------------------------------------------------
# The domain state machine (§23-25). Pure values; no store, no clock, no I/O.
# --------------------------------------------------------------------------


def test_a_ready_export_is_downloadable_until_its_window_lapses() -> None:
    """`is_downloadable` is `True` up to `expires_at` and `False` from it — fail-safe expiry.

    The archive stops being offered the instant its window passes, without waiting for a
    retention sweep to flip the status: the download check reads the clock, not a stored flag.
    """
    ready = _ready()
    assert ready.is_downloadable(COMPLETED) is True
    assert ready.is_downloadable(EXPIRES - timedelta(seconds=1)) is True
    assert ready.is_expired(EXPIRES) is True
    assert ready.is_downloadable(EXPIRES) is False
    assert ready.is_downloadable(EXPIRES + timedelta(days=1)) is False


def test_a_pending_or_failed_export_is_never_downloadable() -> None:
    """Only a `READY` export answers `is_downloadable`; the others have no archive to offer."""
    assert _pending().is_downloadable(COMPLETED) is False
    failed = _pending().failed(reason="PRODUCTION_FAILED", as_of=COMPLETED)
    assert failed.is_downloadable(COMPLETED) is False
    assert failed.is_expired(COMPLETED) is False


@pytest.mark.parametrize("bad", [
    # A PENDING export carries no artifact / completion / expiry / failure.
    {"status": AccountExportStatus.PENDING, "storage_key": "k", "byte_size": 1,
     "completed_at": None, "expires_at": None},
    {"status": AccountExportStatus.PENDING, "completed_at": COMPLETED,
     "storage_key": None, "byte_size": None, "expires_at": None},
    # A READY export must carry a whole artifact, a completion and an expiry, and no failure.
    {"status": AccountExportStatus.READY, "storage_key": None, "byte_size": None},
    {"status": AccountExportStatus.READY, "failure_reason": "X"},
    # A FAILED export carries a reason and a completion, but no artifact and no expiry.
    {"status": AccountExportStatus.FAILED, "storage_key": None, "byte_size": None,
     "completed_at": COMPLETED, "expires_at": None, "failure_reason": None},
    {"status": AccountExportStatus.FAILED, "completed_at": COMPLETED, "expires_at": None,
     "failure_reason": "X"},  # ... but keeps its artifact — refused
    # An EXPIRED export has had its artifact purged.
    {"status": AccountExportStatus.EXPIRED, "completed_at": COMPLETED, "expires_at": EXPIRES},
    # The artifact is both-or-neither: a key without a size (or vice versa) cannot exist.
    {"status": AccountExportStatus.READY, "byte_size": None},
    # The window runs forward: expiry may not precede completion.
    {"status": AccountExportStatus.READY, "expires_at": COMPLETED - timedelta(hours=1)},
    # `updated_at` never precedes `created_at`.
    {"status": AccountExportStatus.READY, "updated_at": CREATED - timedelta(days=1)},
])
def test_an_incoherent_state_cannot_be_constructed(bad: dict[str, object]) -> None:
    """The validator is the state machine made physical: no incoherent export is representable.

    Every case starts from a valid `READY` shape and breaks exactly one rule, so the failure is
    the rule under test and not a second, incidental violation.
    """
    fields: dict[str, object] = {
        "id": AccountExportId(UUID("00000000-0000-4000-8000-0000000000e1")),
        "user_id": USER, "schema_version": ACCOUNT_EXPORT_SCHEMA_VERSION,
        "storage_key": "exports/u/e.json", "byte_size": 2048,
        "completed_at": COMPLETED, "expires_at": EXPIRES,
        "created_at": CREATED, "updated_at": COMPLETED,
    }
    fields.update(bad)
    with pytest.raises(ValidationError):
        AccountExport(**fields)


def test_the_transitions_produce_fresh_coherent_instances() -> None:
    """`completed`/`failed`/`expired` each build a new validated export, never mutate in place.

    A produced export is `READY` and downloadable by construction; a failed one carries its reason
    and no artifact; an expired one clears the bytes but keeps `completed_at`/`expires_at` as
    provenance. The originating `PENDING` value is untouched — the model is frozen.
    """
    pending = _pending()
    ready = pending.completed(storage_key="exports/u/e.json", byte_size=2048,
                              expires_at=EXPIRES, as_of=COMPLETED)
    assert ready.status is AccountExportStatus.READY and ready.is_downloadable(COMPLETED)
    assert pending.status is AccountExportStatus.PENDING  # unchanged

    failed = pending.failed(reason="PRODUCTION_FAILED", as_of=COMPLETED)
    assert failed.status is AccountExportStatus.FAILED
    assert failed.failure_reason == "PRODUCTION_FAILED" and failed.storage_key is None

    expired = ready.expired(as_of=EXPIRES)
    assert expired.status is AccountExportStatus.EXPIRED
    assert expired.storage_key is None and expired.byte_size is None
    assert expired.completed_at == COMPLETED and expired.expires_at == EXPIRES


# --------------------------------------------------------------------------
# The local store (§25): a blob sink, owner-scoped keys, and a traversal guard.
# --------------------------------------------------------------------------


def test_the_store_key_is_owner_scoped_and_id_derived(tmp_path) -> None:
    """`key_for` is `exports/<user>/<export>.json`: stable, greppable, and one user's alone.

    Derived from the two ids so an archive is found without a database lookup and one account's
    key can never name another's file.
    """
    store = LocalAccountExportStore(tmp_path / "exports")
    user = UserId(UUID("00000000-0000-4000-8000-0000000000aa"))
    export = AccountExportId(UUID("00000000-0000-4000-8000-0000000000bb"))
    assert store.key_for(user, export) == f"exports/{user}/{export}.json"


def test_constructing_the_store_writes_nothing(tmp_path) -> None:
    """The root appears on first write, not construction — a test builds one without a directory."""
    root = tmp_path / "exports"
    LocalAccountExportStore(root)
    assert not root.exists()


def test_bytes_put_come_back_with_their_media_type(tmp_path) -> None:
    """A put/get round-trip returns the exact bytes and the archive's JSON media type."""
    store = LocalAccountExportStore(tmp_path / "exports")
    key = "exports/u/e.json"
    store.put(key, b'{"schema_version": 1}')
    stored = store.get(key)
    assert stored.content == b'{"schema_version": 1}'
    assert stored.media_type == "application/json"
    assert stored.storage_key == key


def test_a_missing_key_is_reported_not_swallowed(tmp_path) -> None:
    """`get` on a key that was never written raises `ExportNotFound` carrying the key."""
    store = LocalAccountExportStore(tmp_path / "exports")
    with pytest.raises(ExportNotFound) as caught:
        store.get("exports/u/missing.json")
    assert caught.value.storage_key == "exports/u/missing.json"


def test_delete_is_idempotent(tmp_path) -> None:
    """Deleting present bytes returns `True`; deleting them again is a no-op that returns `False`.

    The property a retention sweep rerun depends on: a second pass reaches the same state without
    raising (§31).
    """
    store = LocalAccountExportStore(tmp_path / "exports")
    key = "exports/u/e.json"
    store.put(key, b"{}")
    assert store.delete(key) is True
    assert store.delete(key) is False


def test_a_key_that_escapes_the_root_is_refused(tmp_path) -> None:
    """The store never lets a `..` or absolute key reach outside its root — a loud programming error.

    Not paranoia about its own id-derived keys, but the guarantee that the store is never the
    component a traversal slips through (docs/ENGINEERING_STANDARDS.md §Security).
    """
    store = LocalAccountExportStore(tmp_path / "exports")
    for escaping in ("../escape.json", "../../etc/passwd", "/etc/passwd"):
        with pytest.raises(ValueError):
            store.get(escaping)
        with pytest.raises(ValueError):
            store.put(escaping, b"{}")


# --------------------------------------------------------------------------
# `ExportSettings` — the export root and the retention window, from env or defaults.
# --------------------------------------------------------------------------


def test_export_settings_default_when_the_environment_is_silent() -> None:
    """An empty environment yields the documented defaults, not an error."""
    settings = ExportSettings.from_env({})
    assert settings.artifact_root == DEFAULT_EXPORT_ARTIFACT_ROOT
    assert settings.retention_hours == DEFAULT_EXPORT_RETENTION_HOURS


def test_export_settings_read_the_root_and_window_from_env() -> None:
    """Both variables are honoured when set."""
    settings = ExportSettings.from_env({
        EXPORT_ARTIFACT_ROOT_VARIABLE: "/srv/exports",
        EXPORT_RETENTION_HOURS_VARIABLE: "24",
    })
    assert settings.artifact_root == "/srv/exports"
    assert settings.retention_hours == 24


def test_a_non_positive_retention_window_is_refused() -> None:
    """`retention_hours` must be `>= 1`: a zero window would make every archive born expired.

    Refused both at the constructor and through `from_env`, so a misconfiguration is a startup
    error rather than a silent disabling of downloads.
    """
    with pytest.raises(ValidationError):
        ExportSettings(retention_hours=0)
    with pytest.raises(ValidationError):
        ExportSettings.from_env({EXPORT_RETENTION_HOURS_VARIABLE: "0"})


def test_a_non_integer_retention_window_refuses_to_guess() -> None:
    """A non-integer value is a named error, never a silent fallback to the default."""
    with pytest.raises(ValueError):
        ExportSettings.from_env({EXPORT_RETENTION_HOURS_VARIABLE: "soon"})


# --------------------------------------------------------------------------
# The gatherer's secret discipline (§24, §73), unit-tested on its own helpers.
# --------------------------------------------------------------------------


def test_a_safe_payload_passes_the_secret_sweep() -> None:
    """A structure carrying only data field names walks clean — the sweep matches keys, not values.

    A value that merely looks credential-ish (a base URL, a display name) is a leaf and safe; only
    a denylisted *key* trips the guard.
    """
    _assert_no_secrets({
        "account_id": "abc", "has_api_key": True,
        "connections": [{"model": "external-model", "base_url": "https://x/v1"}],
    })


@pytest.mark.parametrize("secret_key", sorted(_SECRET_KEY_DENYLIST))
def test_every_denylisted_key_fails_the_sweep_wherever_it_hides(secret_key: str) -> None:
    """Each credential field name raises `ExportContainsSecret`, nested in a dict or a list.

    The backstop is fail-closed and depth-agnostic: a secret a future field reintroduces breaks the
    export loudly wherever it appears, rather than shipping in the archive.
    """
    with pytest.raises(ExportContainsSecret) as top:
        _assert_no_secrets({secret_key: "leak"})
    assert top.value.key == secret_key
    with pytest.raises(ExportContainsSecret):
        _assert_no_secrets({"settings": {"connections": [{secret_key: "leak"}]}})


def test_a_connection_is_reduced_to_safe_metadata() -> None:
    """`_connection_metadata` names `has_api_key`, never the ciphertext or its version (§24).

    Built field by field, so adding a credential-bearing field to `LLMConnection` cannot silently
    ride along. `has_api_key` records *that* a key is configured — what a re-import needs — without
    exporting it, and the result itself passes the secret sweep.
    """
    metadata = _connection_metadata(an_llm_connection())
    assert metadata["has_api_key"] is True
    assert "encrypted_api_key" not in metadata
    assert "secret_version" not in metadata
    assert metadata["model"] == "external-model"
    _assert_no_secrets(metadata)


# --------------------------------------------------------------------------
# The service (§23-25): request → produce → download, over a stub gatherer.
#
# A stub isolates the service's own contract — lifecycle ordering, idempotency, failure mapping,
# owner scope — from the 19-repository gatherer, which is proved end to end over the real app in
# the route tests below. The stub lets a fault be injected precisely: a payload that trips the
# secret guard, or a gather that simply raises.
# --------------------------------------------------------------------------


class _StubGatherer:
    """Stands in for `AccountExportGatherer`: returns a canned payload or raises on demand.

    Counts its calls so a test can prove `produce` does not re-gather an already-produced export.
    """

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls = 0

    async def gather(self, user_id: UserId, *, generated_at: datetime) -> dict[str, object]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return {"schema_version": ACCOUNT_EXPORT_SCHEMA_VERSION, "account_id": str(user_id)}


def _service(tmp_path, *, gatherer=None, exports=None, store=None, retention_hours=168):
    """An `AccountExportService` over a stub gatherer, a real local store and a fake repository."""
    resolved_store = store if store is not None else LocalAccountExportStore(tmp_path / "exports")
    return AccountExportService(
        gatherer=gatherer if gatherer is not None else _StubGatherer(),
        exports=exports if exports is not None else FakeAccountExportRepository(),
        store=resolved_store,
        settings=ExportSettings(artifact_root=str(tmp_path / "exports"),
                                retention_hours=retention_hours))


@pytest.mark.asyncio
async def test_create_produces_a_ready_export_with_its_bytes_stored(tmp_path) -> None:
    """The synchronous path: gather, serialize, store, mark `READY` — downloadable by the end.

    `byte_size` equals the length of the bytes actually written, and those bytes are at the
    store's owner-scoped key — so the metadata the model carries is the metadata of a real archive.
    """
    store = LocalAccountExportStore(tmp_path / "exports")
    service = _service(tmp_path, store=store)

    export = await service.create(USER, now=NOW)

    assert export.status is AccountExportStatus.READY
    assert export.is_downloadable(NOW) is True
    stored = store.get(store.key_for(USER, export.id))
    assert export.byte_size == len(stored.content)
    assert json.loads(stored.content)["account_id"] == str(USER)


@pytest.mark.asyncio
async def test_request_persists_a_pending_export_before_it_is_produced(tmp_path) -> None:
    """`request` alone leaves a `PENDING` row — the seam the M8 worker splits produce off."""
    service = _service(tmp_path)
    export = await service.request(USER, now=NOW)
    assert export.status is AccountExportStatus.PENDING
    assert (await service.get(USER, export.id)).status is AccountExportStatus.PENDING


@pytest.mark.asyncio
async def test_producing_an_already_produced_export_does_not_gather_again(tmp_path) -> None:
    """`produce` is idempotent on a non-`PENDING` export: a retried produce is a no-op.

    The gatherer is called once for the first produce and never again, so a re-run after success
    returns the same `READY` record rather than rebuilding the archive.
    """
    gatherer = _StubGatherer()
    service = _service(tmp_path, gatherer=gatherer)
    export = await service.create(USER, now=NOW)
    again = await service.produce(USER, export.id, now=NOW + timedelta(hours=1))
    assert gatherer.calls == 1
    assert again == export


@pytest.mark.asyncio
async def test_a_secret_guard_trip_fails_the_export_and_stores_nothing(tmp_path) -> None:
    """A payload that trips the secret backstop is a `FAILED` export, distinct from a plain fault.

    The reason is `SECRET_GUARD_TRIPPED` so an operator can tell a leak-prevention bug — to fix at
    its source — from an ordinary gather/store fault, and no archive is written for either.
    """
    store = LocalAccountExportStore(tmp_path / "exports")
    service = _service(tmp_path, gatherer=_StubGatherer(error=ExportContainsSecret("password_hash")),
                       store=store)
    export = await service.create(USER, now=NOW)
    assert export.status is AccountExportStatus.FAILED
    assert export.failure_reason == "SECRET_GUARD_TRIPPED"
    with pytest.raises(ExportNotFound):
        store.get(store.key_for(USER, export.id))


@pytest.mark.asyncio
async def test_an_ordinary_fault_fails_the_export_with_a_production_reason(tmp_path) -> None:
    """Any other fault marks the export `FAILED` with `PRODUCTION_FAILED` — an auditable record."""
    service = _service(tmp_path, gatherer=_StubGatherer(error=RuntimeError("disk full")))
    export = await service.create(USER, now=NOW)
    assert export.status is AccountExportStatus.FAILED
    assert export.failure_reason == "PRODUCTION_FAILED"


@pytest.mark.asyncio
async def test_reads_are_owner_scoped_and_a_stranger_gets_the_same_error_as_a_missing_id(
        tmp_path) -> None:
    """`get` scopes to the owner: another account's id and a never-created id both raise `NotFound`.

    One error for both, so a caller cannot tell "not yours" from "no such export" and enumerate
    another account's exports by id (§68).
    """
    exports = FakeAccountExportRepository()
    service = _service(tmp_path, exports=exports)
    mine = await service.create(USER, now=NOW)

    assert (await service.get(USER, mine.id)).id == mine.id
    with pytest.raises(AccountExportNotFound):
        await service.get(OTHER_USER, mine.id)
    with pytest.raises(AccountExportNotFound):
        await service.get(USER, FOREIGN_EXPORT)


@pytest.mark.asyncio
async def test_the_list_is_this_accounts_exports_most_recently_updated_first(tmp_path) -> None:
    """`list` returns one account's exports newest-first and never another account's."""
    exports = FakeAccountExportRepository()
    service = _service(tmp_path, exports=exports)
    first = await service.create(USER, now=NOW)
    second = await service.create(USER, now=NOW + timedelta(hours=1))
    await exports.upsert(an_account_export(id=FOREIGN_EXPORT, user_id=OTHER_USER,
                                           completed_at=NOW, updated_at=NOW + timedelta(hours=2)))

    listed = await service.list(USER)
    assert [e.id for e in listed] == [second.id, first.id]


@pytest.mark.asyncio
async def test_download_gates_on_ready_and_unexpired_and_propagates_a_storage_fault(
        tmp_path) -> None:
    """Download hands back a `READY`, unexpired archive; every other state is refused correctly.

    A `PENDING` export is `NotReady`; a `READY` one past its window is `NotReady` before any sweep;
    another account's id is `NotFound`; and a `READY` row whose bytes the store has lost surfaces
    the store's `ExportNotFound` rather than being disguised as "not ready" — a storage fault, not
    a client error.
    """
    store = LocalAccountExportStore(tmp_path / "exports")
    service = _service(tmp_path, store=store, retention_hours=168)
    ready = await service.create(USER, now=NOW)

    downloaded = await service.download(USER, ready.id, now=NOW)
    assert downloaded.content and downloaded.media_type == "application/json"

    lapsed = NOW + timedelta(hours=169)
    with pytest.raises(AccountExportNotReady):
        await service.download(USER, ready.id, now=lapsed)
    with pytest.raises(AccountExportNotFound):
        await service.download(OTHER_USER, ready.id, now=NOW)

    pending = await service.request(USER, now=NOW)
    with pytest.raises(AccountExportNotReady):
        await service.download(USER, pending.id, now=NOW)

    # The row is READY and unexpired, but the bytes are gone: a storage fault, surfaced as such.
    store.delete(store.key_for(USER, ready.id))
    with pytest.raises(ExportNotFound):
        await service.download(USER, ready.id, now=NOW)


# --------------------------------------------------------------------------
# The route layer over the real app (§23-25). Owner from the session, never the path.
# --------------------------------------------------------------------------

# A search id for the foreign row the isolation test seeds, distinct from the builder default.
_FOREIGN_SEARCH = SearchProfileId(UUID("00000000-0000-4000-8000-000000000072"))


@pytest.mark.asyncio
async def test_creating_an_export_over_http_produces_a_downloadable_archive(tmp_path) -> None:
    """`POST /me/exports` is a 201 that produces synchronously; the download is the archive bytes.

    Each request is its own resource (201, not an idempotent replace), and because production is
    synchronous the response is the finished `READY` export — a client polls nothing in the common
    case. The download returns the bytes with the archive's media type and a filename, and the
    parsed archive is self-describing: a schema version and the requesting account's own id.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = (await api.users.get_by_email(EMAIL)).id

        created = await api.write("POST", "/me/exports")
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] == AccountExportStatus.READY.value
        assert body["is_downloadable"] is True
        assert body["byte_size"] > 0
        assert body["failure_reason"] is None
        assert "storage_key" not in body  # an internal locator, never surfaced

        download = await api.read(f"/me/exports/{body['id']}/download")
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("application/json")
        assert f'filename="account-export-{body["id"]}.json"' \
            in download.headers["content-disposition"]
        archive = json.loads(download.content)
        assert archive["account_id"] == str(user_id)
        assert archive["schema_version"] == ACCOUNT_EXPORT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_the_archive_holds_only_this_accounts_data_and_no_secret(tmp_path) -> None:
    """The produced bytes carry this account's rows alone (§68) and no credential (§73).

    A distinctive search of the requesting account appears; a second account's search does not.
    The account's password hash, a stored connection's ciphertext and its key version are nowhere
    in the bytes, and the exported connection is reduced to `has_api_key` metadata — the same thing
    the API tells a client, never the key.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = (await api.users.get_by_email(EMAIL)).id
        await api.searches.upsert(a_search_profile(user_id=user_id, name="alpha-search-mine"))
        await api.searches.upsert(a_search_profile(
            id=_FOREIGN_SEARCH, user_id=OTHER_USER, name="beta-search-theirs"))
        await api.llm_connections.upsert(an_llm_connection(user_id=user_id))

        created = await api.write("POST", "/me/exports")
        assert created.status_code == 201, created.text
        download = await api.read(f"/me/exports/{created.json()['id']}/download")
        text = download.text

        assert "alpha-search-mine" in text
        assert "beta-search-theirs" not in text  # another account's row never enters (§68)
        # No credential field name and no ciphertext value survives the gather (§73).
        for forbidden in ("password_hash", "encrypted_api_key", "secret_version",
                          "gAAAAAB-placeholder-ciphertext-not-a-real-key"):
            assert forbidden not in text, forbidden
        (connection,) = json.loads(text)["settings"]["llm_connections"]
        assert connection["has_api_key"] is True


@pytest.mark.asyncio
async def test_another_accounts_export_and_a_missing_id_are_both_404(tmp_path) -> None:
    """A read is owner-scoped: a foreign or unknown export id is a 404, never another's row (§68)."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await api.account_exports.upsert(an_account_export(
            id=FOREIGN_EXPORT, user_id=OTHER_USER))

        for path in (f"/me/exports/{FOREIGN_EXPORT}",
                     f"/me/exports/{FOREIGN_EXPORT}/download",
                     f"/me/exports/{PENDING_EXPORT}"):
            response = await api.read(path)
            assert response.status_code == 404, path
            assert response.json()["error"] == "account_export_not_found", path


@pytest.mark.asyncio
async def test_a_pending_exports_download_is_a_409_not_a_404(tmp_path) -> None:
    """A real but not-`READY` export is a 409: the resource exists, the archive is not ready.

    The record reads back with its `PENDING` status and `is_downloadable` false, and the download
    is refused with `account_export_not_ready` — a distinct signal from "no such export".
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = (await api.users.get_by_email(EMAIL)).id
        await api.account_exports.upsert(an_account_export(
            id=PENDING_EXPORT, user_id=user_id, status=AccountExportStatus.PENDING,
            storage_key=None, byte_size=None, completed_at=None))

        record = await api.read(f"/me/exports/{PENDING_EXPORT}")
        assert record.status_code == 200
        assert record.json()["status"] == AccountExportStatus.PENDING.value
        assert record.json()["is_downloadable"] is False

        download = await api.read(f"/me/exports/{PENDING_EXPORT}/download")
        assert download.status_code == 409
        assert download.json()["error"] == "account_export_not_ready"


@pytest.mark.asyncio
async def test_the_export_history_lists_this_accounts_requests_newest_first(tmp_path) -> None:
    """`GET /me/exports` is this account's history, most recently updated first, and only theirs."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await api.account_exports.upsert(an_account_export(
            id=FOREIGN_EXPORT, user_id=OTHER_USER))  # another account's — never listed

        first = (await api.write("POST", "/me/exports")).json()
        api.clock.advance(timedelta(hours=1))
        second = (await api.write("POST", "/me/exports")).json()

        listed = (await api.read("/me/exports")).json()["exports"]
        assert [e["id"] for e in listed] == [second["id"], first["id"]]
