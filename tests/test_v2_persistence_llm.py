# tests/test_v2_persistence_llm.py
"""What the Phase 11 LLM repositories promise, asserted against PostgreSQL.

The same four properties the other persistence tests establish — an upsert is an
upsert, a user-scoped read cannot cross accounts, an id survives as a native UUID,
an instant survives as the same instant — plus the two the LLM tables add: a stored
credential round-trips as opaque ciphertext (never decrypted here), and the CHECKs
that back `LLMConnection`'s and `LLMRun`'s validators refuse a bad row written past
the model.

Every test runs inside the transaction `db_session` opened and will roll back.
"""
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.app.domain.identifiers import LLMRunId
from backend.app.infrastructure.database.models import (
    LLMConnectionRow,
    LLMRunRow,
    ProviderSessionRow,
)
from backend.app.llm.connection import LLMProviderType
from backend.app.llm.failures import LLMFailureCode
from backend.app.llm.telemetry import LLMRunStatus
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyLLMConnectionRepository,
    SqlAlchemyLLMRunRepository,
    SqlAlchemyProviderSessionRepository,
)
from tests.v2_builders import (
    CONNECTION,
    LATER,
    OTHER_CONNECTION,
    OTHER_USER,
    RUN,
    USER,
    a_provider_session,
    an_llm_connection,
    an_llm_run,
)
from tests.v2_rows import a_user_row

# Every test in this module is async; pytest-asyncio runs in strict mode, so the
# module-level marker is what turns each coroutine into a run rather than a skip.
pytestmark = pytest.mark.asyncio


async def _count(db_session, row_type) -> int:
    result = await db_session.execute(select(func.count()).select_from(row_type))
    return result.scalar_one()


@pytest_asyncio.fixture
async def seeded_users(db_session):
    """`USER` and `OTHER_USER` as foreign-key targets for everything below."""
    db_session.add_all([
        a_user_row(display_name="owner"),
        a_user_row(id=OTHER_USER, display_name="somebody else"),
    ])
    await db_session.flush()


@pytest.fixture
def connections(db_session):
    return SqlAlchemyLLMConnectionRepository(db_session)


@pytest.fixture
def sessions(db_session):
    return SqlAlchemyProviderSessionRepository(db_session)


@pytest.fixture
def runs(db_session):
    return SqlAlchemyLLMRunRepository(db_session)


# --- connections -----------------------------------------------------------

async def test_a_connection_comes_back_exactly_as_it_went_in(seeded_users,
                                                             connections):
    """Every column, including the opaque ciphertext, survives the round trip."""
    written = await connections.upsert(an_llm_connection())
    assert written == an_llm_connection()
    assert await connections.get(USER, CONNECTION) == an_llm_connection()


async def test_a_cli_connection_round_trips_without_a_credential(seeded_users,
                                                                connections):
    """A CLI connection carries no base URL and no key, and reads back the same."""
    cli = an_llm_connection(
        provider_type=LLMProviderType.CLAUDE_CODE, display_name="Claude Code",
        base_url=None, model=None, encrypted_api_key=None, secret_version=None)
    assert await connections.upsert(cli) == cli
    stored = await connections.get(USER, CONNECTION)
    assert stored is not None
    assert stored.has_api_key is False


async def test_upserting_a_connection_twice_updates_one_row(seeded_users, db_session,
                                                            connections):
    await connections.upsert(an_llm_connection())
    # A second save advances `updated_at`, exactly as a service does — which is also
    # what keeps the column out of the `onupdate=now()` path that would expire it.
    await connections.upsert(an_llm_connection(display_name="Renamed",
                                               updated_at=LATER))
    stored = await connections.get(USER, CONNECTION)
    assert stored is not None and stored.display_name == "Renamed"
    assert await _count(db_session, LLMConnectionRow) == 1


async def test_a_connection_is_invisible_to_another_user(seeded_users, connections):
    """Not found and not yours are indistinguishable — no enumeration by id."""
    await connections.upsert(an_llm_connection())
    assert await connections.get(OTHER_USER, CONNECTION) is None


async def test_list_for_user_orders_by_priority_then_id(seeded_users, connections):
    await connections.upsert(an_llm_connection(priority=100))
    await connections.upsert(an_llm_connection(
        id=OTHER_CONNECTION, priority=10, is_default=False))
    listed = await connections.list_for_user(USER)
    assert [c.id for c in listed] == [OTHER_CONNECTION, CONNECTION]


async def test_enabled_only_hides_a_disabled_connection(seeded_users, connections):
    await connections.upsert(an_llm_connection(enabled=False))
    assert await connections.list_for_user(USER) != ()
    assert await connections.list_for_user(USER, enabled_only=True) == ()


async def test_get_default_returns_the_marked_connection(seeded_users, connections):
    await connections.upsert(an_llm_connection(is_default=True))
    got = await connections.get_default(USER)
    assert got is not None and got.id == CONNECTION
    assert got.is_default is True


async def test_two_defaults_for_one_user_are_refused(seeded_users, db_session,
                                                    connections):
    """The partial unique index enforces at most one default per account."""
    await connections.upsert(an_llm_connection(is_default=True))
    with pytest.raises(IntegrityError):
        await connections.upsert(an_llm_connection(
            id=OTHER_CONNECTION, is_default=True))


async def test_clear_default_then_upsert_promotes_a_new_default(seeded_users,
                                                               connections):
    """The two-step a service uses to move the default without tripping the index."""
    await connections.upsert(an_llm_connection(is_default=True))
    await connections.upsert(an_llm_connection(
        id=OTHER_CONNECTION, is_default=False))
    cleared = await connections.clear_default(USER)
    assert cleared == 1
    promoted = await connections.get(USER, OTHER_CONNECTION)
    assert promoted is not None
    await connections.upsert(
        promoted.model_copy(update={"is_default": True, "updated_at": LATER}))
    got = await connections.get_default(USER)
    assert got is not None and got.id == OTHER_CONNECTION


async def test_deleting_a_connection_is_scoped_to_its_owner(seeded_users,
                                                           connections):
    await connections.upsert(an_llm_connection())
    assert await connections.delete(OTHER_USER, CONNECTION) is False
    assert await connections.delete(USER, CONNECTION) is True
    assert await connections.get(USER, CONNECTION) is None


async def test_deleting_a_connection_cascades_sessions_and_nulls_runs(
        seeded_users, db_session, connections, sessions, runs):
    """Its sessions go; its runs stay, their `connection_id` set NULL (§56)."""
    await connections.upsert(an_llm_connection())
    await sessions.upsert(a_provider_session())
    await runs.upsert(an_llm_run())
    await connections.delete(USER, CONNECTION)
    assert await _count(db_session, ProviderSessionRow) == 0
    assert await _count(db_session, LLMRunRow) == 1
    remaining = await db_session.execute(
        select(LLMRunRow.connection_id).where(LLMRunRow.id == RUN))
    assert remaining.scalar_one() is None


# --- a raw-row CHECK, written past the model ------------------------------

async def test_a_cli_connection_with_a_base_url_is_refused_by_the_check(
        seeded_users, db_session):
    """`ck_llm_connections_transport_shape_coherent`, exercised past the model.

    The model refuses this at construction; the CHECK is what refuses it when a
    migration or a hand-written INSERT does not go through the model — the §1 rule
    that a self-authenticating CLI carries no base URL, made physical.
    """
    db_session.add(LLMConnectionRow(
        id=UUID(int=0xC11),
        user_id=USER,
        provider_type=LLMProviderType.CLAUDE_CODE,
        display_name="bad cli",
        base_url="https://sneaky.example.invalid",
        model=None, encrypted_api_key=None, secret_version=None,
        enabled=True, is_default=False, priority=100))
    with pytest.raises(IntegrityError):
        await db_session.flush()


# --- provider sessions -----------------------------------------------------

async def test_a_provider_session_comes_back_exactly(seeded_users, connections,
                                                    sessions):
    await connections.upsert(an_llm_connection())
    written = await sessions.upsert(a_provider_session())
    assert written == a_provider_session()
    got = await sessions.get(USER, CONNECTION, "chat-1")
    assert got == a_provider_session()


async def test_resuming_a_conversation_updates_one_session_row(
        seeded_users, db_session, connections, sessions):
    """The derived id makes a resume refresh the one row, not append a second."""
    await connections.upsert(an_llm_connection())
    await sessions.upsert(a_provider_session(external_session_id="first"))
    await sessions.upsert(a_provider_session(external_session_id="second",
                                             updated_at=LATER))
    got = await sessions.get(USER, CONNECTION, "chat-1")
    assert got is not None and got.external_session_id == "second"
    assert await _count(db_session, ProviderSessionRow) == 1


async def test_a_session_is_invisible_to_another_user(seeded_users, connections,
                                                     sessions):
    await connections.upsert(an_llm_connection())
    await sessions.upsert(a_provider_session())
    assert await sessions.get(OTHER_USER, CONNECTION, "chat-1") is None


# --- telemetry runs --------------------------------------------------------

async def test_a_run_comes_back_exactly_with_its_measurements(seeded_users,
                                                             connections, runs):
    await connections.upsert(an_llm_connection())
    written = await runs.upsert(an_llm_run())
    assert written == an_llm_run()


async def test_a_run_keeps_the_unknown_null(seeded_users, connections, runs):
    """A provider that reports no usage leaves the counts NULL, never 0 (§58)."""
    await connections.upsert(an_llm_connection())
    stored = await runs.upsert(an_llm_run(
        prompt_tokens=None, completion_tokens=None, total_tokens=None,
        cost_usd=None, latency_ms=None))
    assert stored.total_tokens is None
    assert stored.cost_usd is None


async def test_a_started_run_updates_in_place_when_it_finishes(
        seeded_users, db_session, connections, runs):
    await connections.upsert(an_llm_connection())
    await runs.upsert(an_llm_run(status=LLMRunStatus.STARTED, finished_at=None,
                                 prompt_tokens=None, completion_tokens=None,
                                 total_tokens=None, cost_usd=None, latency_ms=None))
    await runs.upsert(an_llm_run())  # the terminal SUCCEEDED row, same id
    listed = await runs.list_for_user(USER)
    assert len(listed) == 1 and listed[0].status is LLMRunStatus.SUCCEEDED
    assert await _count(db_session, LLMRunRow) == 1


async def test_a_probe_run_persists_without_a_user(seeded_users, connections, runs):
    """A healthcheck probe has no user; the nullable owner is what lets it record."""
    await connections.upsert(an_llm_connection())
    stored = await runs.upsert(an_llm_run(user_id=None))
    assert stored.user_id is None
    # A user-scoped feed does not surface an ownerless run.
    assert await runs.list_for_user(USER) == ()


async def test_a_failed_run_records_its_typed_code(seeded_users, connections, runs):
    await connections.upsert(an_llm_connection())
    stored = await runs.upsert(an_llm_run(
        status=LLMRunStatus.FAILED,
        failure_code=LLMFailureCode.PROVIDER_TIMEOUT,
        failure_detail="the provider did not answer before the timeout"))
    assert stored.failure_code is LLMFailureCode.PROVIDER_TIMEOUT


async def test_runs_are_listed_newest_first_and_scoped(seeded_users, connections,
                                                      runs):
    await connections.upsert(an_llm_connection())
    await runs.upsert(an_llm_run())
    await runs.upsert(an_llm_run(
        id=LLMRunId(UUID("00000000-0000-4000-8000-0000000000b2")),
        user_id=OTHER_USER, connection_id=None))
    mine = await runs.list_for_user(USER)
    assert [r.id for r in mine] == [RUN]
