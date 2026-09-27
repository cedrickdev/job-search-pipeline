"""Credential rotation: bring every stored credential up to the active key version (§22).

Rotation is the operational path that lets an old master key be retired. These tests hold
the invariants that make it safe to run and safe to re-run — it re-encrypts a stale
credential at the active version and leaves a current one alone (idempotent), it never
downgrades, it fails closed when a previous key is missing (losing no credential), and
neither its report nor its failures ever carry the plaintext. The service is exercised
against an in-memory store; a final PostgreSQL section proves the real store re-encrypts
across owners and excludes keyless connections.
"""
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr, ValidationError

from backend.app.core.settings import CURRENT_SECRET_VERSION, LLMSecretSettings
from backend.app.domain.identifiers import LLMConnectionId
from backend.app.llm.connection import LLMConnection, LLMProviderType
from backend.app.llm.rotation import (
    CredentialRotationService,
    format_rotation_report,
)
from backend.app.llm.secrets import FernetSecretCipher, generate_master_key
from backend.app.repositories.contracts import DEFAULT_LIMIT
from backend.app.repositories.sqlalchemy_credential_rotation import (
    SqlAlchemyCredentialRotationStore,
)
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyLLMConnectionRepository,
)
from tests.v2_builders import OTHER_USER, USER, an_llm_connection
from tests.v2_rows import a_user_row

# Three independent Fernet keys, generated once: v1 wrote the stored credentials, v2 is
# the key a rotation moves them to, v3 stands in for a value written by a newer key than
# the active one (the no-downgrade case). None is ever a real deployment key.
KEY_V1 = generate_master_key()
KEY_V2 = generate_master_key()
KEY_V3 = generate_master_key()

# The connection ids the fixtures use, distinct so a walk sees a stable order.
CONNECTION_A = LLMConnectionId(UUID("00000000-0000-4000-8000-0000000000f1"))
CONNECTION_B = LLMConnectionId(UUID("00000000-0000-4000-8000-0000000000f2"))
CLI_CONNECTION = LLMConnectionId(UUID("00000000-0000-4000-8000-0000000000f3"))

SECRET_A = "sk-live-aaaaaaaaaaaaaaaaaaaa"  # noqa: S105 — a test plaintext, not a real credential
SECRET_B = "sk-live-bbbbbbbbbbbbbbbbbbbb"  # noqa: S105 — a test plaintext, not a real credential


def _cipher(key: str, *, version: int, older: dict[int, str] | None = None) -> FernetSecretCipher:
    return FernetSecretCipher(key, version=version, older=older)


def _stored(cipher: FernetSecretCipher, plaintext: str, *,
            id: LLMConnectionId = CONNECTION_A, user_id=USER) -> LLMConnection:
    """An API connection whose credential `cipher` encrypted at its version."""
    return an_llm_connection(
        id=id, user_id=user_id,
        encrypted_api_key=cipher.encrypt(SecretStr(plaintext)),
        secret_version=cipher.version)


class FakeCredentialRotationStore:
    """An in-memory `CredentialRotationStore`, mirroring the SQL store's two rules.

    Keeps a `LLMConnection` per id; `list_credentialed` streams only the rows that
    carry a credential, keyset-ordered by id, and `reencrypt` rewrites exactly the
    ciphertext and version of a still-credentialed row — a `model_copy`, so the walk
    can read back what it wrote and every other field is left as it was.
    """

    def __init__(self, *connections: LLMConnection) -> None:
        self._by_id: dict[LLMConnectionId, LLMConnection] = {
            connection.id: connection for connection in connections}
        self.writes = 0

    async def list_credentialed(
        self, *, after_id: LLMConnectionId | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> tuple[LLMConnection, ...]:
        rows = sorted(
            (c for c in self._by_id.values() if c.encrypted_api_key is not None),
            key=lambda c: c.id)
        if after_id is not None:
            rows = [c for c in rows if c.id > after_id]
        return tuple(rows[:limit])

    async def reencrypt(self, connection_id: LLMConnectionId, *,
                        ciphertext: str, secret_version: int) -> bool:
        current = self._by_id.get(connection_id)
        if current is None or current.encrypted_api_key is None:
            return False
        self._by_id[connection_id] = current.model_copy(
            update={"encrypted_api_key": ciphertext, "secret_version": secret_version})
        self.writes += 1
        return True

    def current(self, connection_id: LLMConnectionId) -> LLMConnection:
        return self._by_id[connection_id]


# --- settings: reading a rotation window from the environment ----------------

def test_from_env_reads_the_active_version_and_previous_keys():
    """A rotation window: a new active key at v2, the old key still available at v1."""
    settings = LLMSecretSettings.from_env({
        "JOBSEARCH_LLM_SECRET_KEY": KEY_V2,
        "JOBSEARCH_LLM_SECRET_KEY_VERSION": "2",
        "JOBSEARCH_LLM_SECRET_KEY_V1": KEY_V1,
    })
    assert settings.active_version == 2
    assert dict(settings.previous_keys) == {1: KEY_V1}
    assert settings.master_key == KEY_V2


def test_from_env_defaults_to_version_one_with_no_previous_keys():
    """Outside a rotation the version is the default and no previous key is held."""
    settings = LLMSecretSettings.from_env({"JOBSEARCH_LLM_SECRET_KEY": KEY_V1})
    assert settings.active_version == CURRENT_SECRET_VERSION
    assert dict(settings.previous_keys) == {}


def test_only_digit_suffixed_previous_key_vars_are_read():
    """`_VERSION` and `_VERSIONX` are not previous keys; only `_V<digits>` is."""
    settings = LLMSecretSettings.from_env({
        "JOBSEARCH_LLM_SECRET_KEY": KEY_V2,
        "JOBSEARCH_LLM_SECRET_KEY_VERSION": "2",
        "JOBSEARCH_LLM_SECRET_KEY_V1": KEY_V1,
        "JOBSEARCH_LLM_SECRET_KEY_VERSIONX": "noise",
    })
    assert dict(settings.previous_keys) == {1: KEY_V1}


def test_a_previous_key_under_the_active_version_is_refused():
    """A previous key at the active version would shadow it — a misconfiguration."""
    with pytest.raises(ValidationError):
        LLMSecretSettings.from_env({
            "JOBSEARCH_LLM_SECRET_KEY": KEY_V1,
            "JOBSEARCH_LLM_SECRET_KEY_VERSION": "1",
            "JOBSEARCH_LLM_SECRET_KEY_V1": KEY_V2,
        })


def test_repr_carries_no_key_material():
    """The repr may name versions; it must never render a key."""
    settings = LLMSecretSettings.from_env({
        "JOBSEARCH_LLM_SECRET_KEY": KEY_V2,
        "JOBSEARCH_LLM_SECRET_KEY_VERSION": "2",
        "JOBSEARCH_LLM_SECRET_KEY_V1": KEY_V1,
    })
    rendered = repr(settings)
    assert KEY_V1 not in rendered
    assert KEY_V2 not in rendered
    assert "previous_versions=[1]" in rendered


# --- the service, against an in-memory store ---------------------------------

def _rotation_cipher(older: dict[int, str] | None = None) -> FernetSecretCipher:
    """The cipher an operator runs a rotation with: active at v2, plus a window."""
    return _cipher(KEY_V2, version=2, older=older)


@pytest.mark.asyncio
async def test_a_stale_credential_is_reencrypted_at_the_active_version():
    cipher = _rotation_cipher(older={1: KEY_V1})
    store = FakeCredentialRotationStore(_stored(_cipher(KEY_V1, version=1), SECRET_A))
    service = CredentialRotationService(store, cipher)

    report = await service.rotate()

    assert (report.scanned, report.rotated, report.already_current, report.failed) == (
        1, 1, 0, 0)
    assert report.is_clean
    rotated = store.current(CONNECTION_A)
    assert rotated.secret_version == 2
    assert cipher.decrypt(
        rotated.encrypted_api_key, secret_version=2).get_secret_value() == SECRET_A


@pytest.mark.asyncio
async def test_rotation_is_idempotent_on_a_second_run():
    cipher = _rotation_cipher(older={1: KEY_V1})
    store = FakeCredentialRotationStore(_stored(_cipher(KEY_V1, version=1), SECRET_A))
    service = CredentialRotationService(store, cipher)

    await service.rotate()
    again = await service.rotate()

    assert (again.rotated, again.already_current, again.failed) == (0, 1, 0)
    assert store.writes == 1  # the second run wrote nothing


@pytest.mark.asyncio
async def test_a_current_credential_is_left_untouched():
    cipher = _rotation_cipher(older={1: KEY_V1})
    at_active = _stored(cipher, SECRET_A)  # already written under the active version
    store = FakeCredentialRotationStore(at_active)
    service = CredentialRotationService(store, cipher)

    report = await service.rotate()

    assert (report.rotated, report.already_current) == (0, 1)
    assert store.writes == 0
    assert store.current(CONNECTION_A).encrypted_api_key == at_active.encrypted_api_key


@pytest.mark.asyncio
async def test_a_dry_run_reports_what_it_would_do_but_writes_nothing():
    cipher = _rotation_cipher(older={1: KEY_V1})
    stale = _stored(_cipher(KEY_V1, version=1), SECRET_A)
    store = FakeCredentialRotationStore(stale)
    service = CredentialRotationService(store, cipher)

    report = await service.rotate(dry_run=True)

    assert report.dry_run and report.rotated == 1 and report.is_clean
    assert store.writes == 0
    assert store.current(CONNECTION_A).encrypted_api_key == stale.encrypted_api_key


@pytest.mark.asyncio
async def test_a_missing_previous_key_fails_closed_without_touching_the_row():
    cipher = _rotation_cipher()  # no window: cannot decrypt the v1 row
    stale = _stored(_cipher(KEY_V1, version=1), SECRET_A)
    store = FakeCredentialRotationStore(stale)
    service = CredentialRotationService(store, cipher)

    report = await service.rotate()

    assert (report.rotated, report.failed) == (0, 1)
    assert not report.is_clean
    failure = report.failures[0]
    assert failure.connection_id == CONNECTION_A and failure.stored_version == 1
    assert store.writes == 0
    assert store.current(CONNECTION_A).encrypted_api_key == stale.encrypted_api_key


@pytest.mark.asyncio
async def test_a_version_newer_than_active_is_never_downgraded():
    cipher = _rotation_cipher(older={1: KEY_V1})  # active v2
    newer = _stored(_cipher(KEY_V3, version=3), SECRET_A)  # stored under v3
    store = FakeCredentialRotationStore(newer)
    service = CredentialRotationService(store, cipher)

    report = await service.rotate()

    assert (report.rotated, report.failed) == (0, 1)
    assert report.failures[0].stored_version == 3
    assert "downgrade" in report.failures[0].reason
    assert store.writes == 0
    assert store.current(CONNECTION_A).secret_version == 3


@pytest.mark.asyncio
async def test_neither_the_report_nor_its_failures_carry_the_plaintext():
    cipher = _rotation_cipher(older={1: KEY_V1})  # active v2, can read v1
    store = FakeCredentialRotationStore(
        _stored(_cipher(KEY_V1, version=1), SECRET_A, id=CONNECTION_A),
        _stored(_cipher(KEY_V3, version=3), SECRET_B, id=CONNECTION_B))  # a v3 failure
    service = CredentialRotationService(store, cipher)

    report = await service.rotate()

    assert (report.rotated, report.failed) == (1, 1)
    rendered = format_rotation_report(report)
    for leak in (SECRET_A, SECRET_B):
        assert leak not in rendered
        assert leak not in repr(report)
        assert all(leak not in str(failure) for failure in report.failures)


@pytest.mark.asyncio
async def test_rotation_pages_across_batches():
    """A batch smaller than the table still rotates every row, once each."""
    cipher = _rotation_cipher(older={1: KEY_V1})
    v1 = _cipher(KEY_V1, version=1)
    store = FakeCredentialRotationStore(
        _stored(v1, SECRET_A, id=CONNECTION_A),
        _stored(v1, SECRET_B, id=CONNECTION_B),
        _stored(v1, SECRET_A, id=CLI_CONNECTION))
    service = CredentialRotationService(store, cipher, batch_size=1)

    report = await service.rotate()

    assert (report.scanned, report.rotated, report.failed) == (3, 3, 0)
    assert store.writes == 3


# --- the real store, against PostgreSQL --------------------------------------

@pytest_asyncio.fixture
async def seeded_users(db_session):
    """`USER` and `OTHER_USER` as owners for the connections a rotation walks."""
    db_session.add_all([
        a_user_row(display_name="owner"),
        a_user_row(id=OTHER_USER, display_name="somebody else"),
    ])
    await db_session.flush()


@pytest.mark.asyncio
async def test_the_store_reencrypts_across_owners_and_skips_keyless(
        seeded_users, db_session):
    """A rotation crosses every account and leaves a self-authenticating CLI alone."""
    v1 = _cipher(KEY_V1, version=1)
    repo = SqlAlchemyLLMConnectionRepository(db_session)
    await repo.upsert(_stored(v1, SECRET_A, id=CONNECTION_A, user_id=USER))
    await repo.upsert(_stored(v1, SECRET_B, id=CONNECTION_B, user_id=OTHER_USER))
    await repo.upsert(an_llm_connection(
        id=CLI_CONNECTION, provider_type=LLMProviderType.CLAUDE_CODE,
        display_name="Claude Code", base_url=None, model=None,
        encrypted_api_key=None, secret_version=None))

    cipher = _cipher(KEY_V2, version=2, older={1: KEY_V1})
    store = SqlAlchemyCredentialRotationStore(db_session)
    report = await CredentialRotationService(store, cipher).rotate()

    # Only the two credentialed rows are scanned, and both cross-owner rows rotate.
    assert (report.scanned, report.rotated, report.failed) == (2, 2, 0)
    a = await repo.get(USER, CONNECTION_A)
    b = await repo.get(OTHER_USER, CONNECTION_B)
    assert a is not None and a.secret_version == 2
    assert b is not None and b.secret_version == 2
    assert cipher.decrypt(
        a.encrypted_api_key, secret_version=2).get_secret_value() == SECRET_A
    assert cipher.decrypt(
        b.encrypted_api_key, secret_version=2).get_secret_value() == SECRET_B
    cli = await repo.get(USER, CLI_CONNECTION)
    assert cli is not None and cli.has_api_key is False


@pytest.mark.asyncio
async def test_a_second_store_run_finds_everything_already_current(
        seeded_users, db_session):
    """Re-running against the real store rewrites nothing — the idempotence §22 needs."""
    v1 = _cipher(KEY_V1, version=1)
    repo = SqlAlchemyLLMConnectionRepository(db_session)
    await repo.upsert(_stored(v1, SECRET_A, id=CONNECTION_A, user_id=USER))

    cipher = _cipher(KEY_V2, version=2, older={1: KEY_V1})
    service = CredentialRotationService(
        SqlAlchemyCredentialRotationStore(db_session), cipher)
    await service.rotate()
    again = await service.rotate()

    assert (again.rotated, again.already_current, again.failed) == (0, 1, 0)
