# tests/test_v2_account_deletion.py
"""Account deletion — permanently erase an account and everything it owns (§26-29, §69).

Phase 16 M6, the destructive counterpart to the M5 export. Where an export copies an account's
data out, deletion removes it: every user-owned row, every stored artifact, every live session —
leaving only a receipt and, in the database, a de-identified billing record that no longer names
anyone. Two layers are proved where each lives.

**The service (`AccountDeletionService`)**, over fakes and two real local artifact stores under
`tmp_path`, so the bytes a §69 assertion checks are real files. Re-authentication comes first: a
wrong password, or an account that no longer exists, raises `ReauthenticationRequired` and touches
nothing — not a row, not a byte, not a session. The correct password erases the account and returns
a receipt whose counts are exactly what was removed: sessions revoked, document artifacts deleted,
export archives deleted.

**The route (`POST /me/deletion`)**, over the real app through `api_harness`. The owner is the
session's, never the body (§63): a request carries only a password, so it can only ever erase its
own account. On success the receipt is returned, the session cookies are cleared, the revoked token
is refused on replay, and the freed address can be registered afresh — the row is gone. A wrong
password is a 403 that leaves the account signed in and intact.

The database cascade (every user-owned row) and the `subscription_events` `ON DELETE SET NULL`
de-identification are proved against real PostgreSQL in the persistence suites; this module never
asserts them over the fakes, which do not model foreign keys.
"""
from uuid import UUID

import pytest

from backend.app.accounts import (
    AccountDeletionReceipt,
    AccountDeletionService,
    ReauthenticationRequired,
)
from backend.app.core.settings import AuthSettings
from backend.app.documents.artifacts import ArtifactNotFound, LocalDocumentArtifactStore
from backend.app.domain.identifiers import AccountExportId
from backend.app.exports.store import ExportNotFound, LocalAccountExportStore
from backend.app.services.authentication import AuthenticationService, SignedInUser
from tests.v2_api import EMAIL, OTHER_EMAIL, PASSWORD, WRONG_PASSWORD, api_harness
from tests.v2_builders import LATER, NOW, OTHER_USER, a_rendered_document
from tests.v2_fakes import (
    FakeCandidateDocumentRepository,
    FakeSessionRepository,
    FakeUserRepository,
)

# An export id the service tests seed an archive under, so a deletion has a real file to remove.
_EXPORT = AccountExportId(UUID("00000000-0000-4000-8000-0000000000d6"))
# A valid artifact key the document tests write bytes under and seed a rendered version at.
_ARTIFACT_KEY = "documents/deletion-fixture/v1.pdf"


async def _register(users: FakeUserRepository, sessions: FakeSessionRepository, *,
                    email: str = EMAIL) -> SignedInUser:
    """Seed a real Argon2-hashed account with one live session, the way registration does.

    Going through `AuthenticationService` rather than hand-building a `User` means the stored hash
    is a real one `verify_password` accepts, so the re-authentication path is exercised for real.
    """
    auth = AuthenticationService(users, sessions, AuthSettings.for_local_http())
    return await auth.register(email=email, password=PASSWORD,
                               display_name="Candidate", now=NOW)


def _service(*, users, sessions, documents, document_store, export_store):
    """An `AccountDeletionService` over fakes and two real local stores."""
    return AccountDeletionService(
        users=users, sessions=sessions, documents=documents,
        document_store=document_store, export_store=export_store)


# --------------------------------------------------------------------------
# The service (§26-29): re-authenticate, then erase — over fakes and real stores.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_wrong_password_deletes_nothing_and_demands_reauthentication(tmp_path) -> None:
    """The re-authentication gate runs first: a wrong password touches no row, byte or session.

    Deletion is irreversible, so a valid session is not enough — the password is verified against
    the stored hash before anything is read or written. A mismatch raises `ReauthenticationRequired`
    and the account, its live session and both stored artifacts are exactly as they were.
    """
    users, sessions = FakeUserRepository(), FakeSessionRepository()
    documents = FakeCandidateDocumentRepository()
    document_store = LocalDocumentArtifactStore(tmp_path / "documents")
    export_store = LocalAccountExportStore(tmp_path / "exports")
    account = await _register(users, sessions)
    await documents.upsert(
        a_rendered_document(storage_key=_ARTIFACT_KEY, user_id=account.user.id))
    document_store.put(_ARTIFACT_KEY, b"%PDF-1.4 fake bytes")
    export_key = export_store.key_for(account.user.id, _EXPORT)
    export_store.put(export_key, b"{}")
    service = _service(users=users, sessions=sessions, documents=documents,
                       document_store=document_store, export_store=export_store)

    with pytest.raises(ReauthenticationRequired):
        await service.delete(account.user.id, password=WRONG_PASSWORD, now=LATER)

    assert await users.get(account.user.id) is not None
    [session] = sessions.sessions.values()
    assert session.revoked_at is None
    assert document_store.get(_ARTIFACT_KEY).content == b"%PDF-1.4 fake bytes"
    assert export_store.get(export_key).content == b"{}"


@pytest.mark.asyncio
async def test_deleting_an_account_that_does_not_exist_demands_reauthentication(tmp_path) -> None:
    """An id that maps to no account is `ReauthenticationRequired`, indistinguishable from a wrong
    password — the endpoint is never an oracle for whether a session's account still exists.
    """
    service = _service(
        users=FakeUserRepository(), sessions=FakeSessionRepository(),
        documents=FakeCandidateDocumentRepository(),
        document_store=LocalDocumentArtifactStore(tmp_path / "documents"),
        export_store=LocalAccountExportStore(tmp_path / "exports"))
    with pytest.raises(ReauthenticationRequired):
        await service.delete(OTHER_USER, password=PASSWORD, now=LATER)


@pytest.mark.asyncio
async def test_the_correct_password_erases_the_account_and_returns_accurate_counts(
        tmp_path) -> None:
    """The whole erasure: the account row gone, every session revoked, both artifacts deleted (§69).

    Two sessions are live (a register and a later login), one rendered document's PDF and one export
    archive are stored; the receipt's counts are each exactly what was removed, and the bytes the
    cascade cannot reach are gone from both stores — not merely dereferenced.
    """
    users, sessions = FakeUserRepository(), FakeSessionRepository()
    documents = FakeCandidateDocumentRepository()
    document_store = LocalDocumentArtifactStore(tmp_path / "documents")
    export_store = LocalAccountExportStore(tmp_path / "exports")
    account = await _register(users, sessions)
    # A second live session, so `sessions_revoked` is more than the register's one.
    await AuthenticationService(users, sessions, AuthSettings.for_local_http()).log_in(
        email=EMAIL, password=PASSWORD, now=NOW)
    await documents.upsert(
        a_rendered_document(storage_key=_ARTIFACT_KEY, user_id=account.user.id))
    document_store.put(_ARTIFACT_KEY, b"%PDF-1.4 fake bytes")
    export_key = export_store.key_for(account.user.id, _EXPORT)
    export_store.put(export_key, b"{}")
    service = _service(users=users, sessions=sessions, documents=documents,
                       document_store=document_store, export_store=export_store)

    receipt = await service.delete(account.user.id, password=PASSWORD, now=LATER)

    assert receipt == AccountDeletionReceipt(
        user_id=account.user.id, deleted_at=LATER, sessions_revoked=2,
        document_artifacts_removed=1, export_archives_removed=1)
    assert await users.get(account.user.id) is None
    assert all(session.revoked_at == LATER for session in sessions.sessions.values())
    with pytest.raises(ArtifactNotFound):
        document_store.get(_ARTIFACT_KEY)
    with pytest.raises(ExportNotFound):
        export_store.get(export_key)


@pytest.mark.asyncio
async def test_an_account_with_no_artifacts_deletes_cleanly_with_zero_counts(tmp_path) -> None:
    """An account that stored nothing deletes without error: zero artifacts, its one session gone."""
    users, sessions = FakeUserRepository(), FakeSessionRepository()
    account = await _register(users, sessions)
    service = _service(
        users=users, sessions=sessions, documents=FakeCandidateDocumentRepository(),
        document_store=LocalDocumentArtifactStore(tmp_path / "documents"),
        export_store=LocalAccountExportStore(tmp_path / "exports"))

    receipt = await service.delete(account.user.id, password=PASSWORD, now=LATER)

    assert receipt.document_artifacts_removed == 0
    assert receipt.export_archives_removed == 0
    assert receipt.sessions_revoked == 1
    assert await users.get(account.user.id) is None


# --------------------------------------------------------------------------
# The route (§26-29, §63): owner from the session, password in the body, cookies cleared.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_an_account_over_http_returns_a_receipt_and_signs_the_browser_out(
        tmp_path) -> None:
    """`POST /me/deletion` erases the session's account and clears its cookies (§26-29).

    The receipt counts what was removed and echoes no account data — no password, no storage key.
    Afterwards the browser holds no session, the revoked token is refused on replay (server-side
    revocation, not just a cleared cookie), and the freed address registers afresh: the row is gone.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        user_id = (await api.users.get_by_email(EMAIL)).id
        created = await api.write("POST", "/me/exports")  # a real archive for deletion to remove
        assert created.status_code == 201, created.text
        captured_session = api.session_token

        deleted = await api.write("POST", "/me/deletion",
                                  json={"password": PASSWORD.get_secret_value()})
        assert deleted.status_code == 200, deleted.text
        body = deleted.json()
        assert body["user_id"] == str(user_id)
        assert body["sessions_revoked"] >= 1
        assert body["export_archives_removed"] >= 1
        assert "password" not in body and "storage_key" not in body

        assert api.session_token is None
        assert (await api.read("/me/exports")).status_code == 401
        api.plant_cookie(api.settings.session_cookie_name, captured_session)
        assert (await api.read("/me/exports")).status_code == 401
        assert (await api.register()).status_code == 201  # the address is free again
        assert await api.users.get_by_email(EMAIL) is not None

@pytest.mark.asyncio
async def test_a_wrong_password_over_http_is_a_403_and_leaves_the_account_intact(tmp_path) -> None:
    """A wrong password is a 403 `reauthentication_required`; the account stays signed in and whole.

    The gate raises before a byte is touched and before the cookies are cleared, so the same
    session still reads its data and the account still exists.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        refused = await api.write("POST", "/me/deletion",
                                  json={"password": WRONG_PASSWORD.get_secret_value()})
        assert refused.status_code == 403
        assert refused.json()["error"] == "reauthentication_required"
        assert (await api.read("/me/exports")).status_code == 200
        assert await api.users.get_by_email(EMAIL) is not None


@pytest.mark.asyncio
async def test_deletion_erases_only_the_sessions_account_never_another(tmp_path) -> None:
    """The owner is the session's, never the body (§63): a deletion cannot reach another account.

    Two accounts exist; the request carries only a password and no id, so it erases the account the
    session names (the second) and leaves the first — which the request never mentions — untouched.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in(email=EMAIL)                                     # account A
        assert (await api.register(email=OTHER_EMAIL)).status_code == 201  # account B, now current

        deleted = await api.write("POST", "/me/deletion",
                                  json={"password": PASSWORD.get_secret_value()})
        assert deleted.status_code == 200, deleted.text

        assert await api.users.get_by_email(OTHER_EMAIL) is None  # the session's account, gone
        assert await api.users.get_by_email(EMAIL) is not None    # the other account, intact
