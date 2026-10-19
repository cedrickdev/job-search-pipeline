from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from backend.app.claude_terminal.contracts import (
    ClaudeTerminalSession,
    ClaudeTerminalState,
)
from backend.app.claude_terminal.service import ClaudeTerminalSessionService
from backend.app.domain.identifiers import UserId


NOW = datetime(2026, 10, 8, tzinfo=UTC)


class FakeTerminalSessions:
    def __init__(self) -> None:
        self.sessions: dict[object, ClaudeTerminalSession] = {}

    async def create(self, session: ClaudeTerminalSession) -> ClaudeTerminalSession:
        self.sessions[session.id] = session
        return session

    async def get_for_user(self, user_id: UserId, session_id: object) -> ClaudeTerminalSession | None:
        session = self.sessions.get(session_id)
        return session if session is not None and session.owner_user_id == user_id else None

    async def save(self, session: ClaudeTerminalSession) -> ClaudeTerminalSession:
        self.sessions[session.id] = session
        return session

    async def list_expired_active(
        self, now: datetime, *, limit: int,
    ) -> tuple[ClaudeTerminalSession, ...]:
        return tuple(
            session for session in self.sessions.values()
            if not session.state.is_final and session.expires_at <= now
        )[:limit]


@pytest.fixture
def owner() -> UserId:
    return UserId(uuid4())


@pytest.fixture
def other_user() -> UserId:
    return UserId(uuid4())


@pytest.fixture
def service() -> ClaudeTerminalSessionService:
    return ClaudeTerminalSessionService(
        FakeTerminalSessions(), session_ttl=timedelta(minutes=30))


@pytest.mark.asyncio
async def test_owner_can_stop_a_session_twice(
    service: ClaudeTerminalSessionService, owner: UserId,
) -> None:
    terminal = await service.create_for_user(owner, now=NOW)

    first = await service.stop_for_user(owner, terminal.id, now=NOW)
    second = await service.stop_for_user(owner, terminal.id, now=NOW)

    assert first is not None
    assert second is not None
    assert first.state is ClaudeTerminalState.TERMINATED
    assert second.state is ClaudeTerminalState.TERMINATED
    assert first.ended_at == NOW
    assert second.ended_at == NOW


@pytest.mark.asyncio
async def test_foreign_owner_cannot_read_or_stop_a_known_session(
    service: ClaudeTerminalSessionService, owner: UserId, other_user: UserId,
) -> None:
    terminal = await service.create_for_user(owner, now=NOW)

    assert await service.get_for_user(other_user, terminal.id) is None
    assert await service.stop_for_user(other_user, terminal.id, now=NOW) is None


@pytest.mark.asyncio
async def test_expiring_active_session_is_final_and_safe(
    service: ClaudeTerminalSessionService, owner: UserId,
) -> None:
    terminal = await service.create_for_user(owner, now=NOW)
    active = await service.activate_for_user(owner, terminal.id, runtime_reference="opaque", now=NOW)

    expired = await service.expire_due(now=NOW + timedelta(minutes=31))

    assert [session.id for session in expired] == [active.id]
    refreshed = await service.get_for_user(owner, active.id)
    assert refreshed is not None
    assert refreshed.state is ClaudeTerminalState.EXPIRED
    assert refreshed.runtime_reference == "opaque"
    assert refreshed.ended_at == NOW + timedelta(minutes=31)
