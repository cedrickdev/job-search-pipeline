"""Bounded, retry-safe expiry of isolated Claude terminal sessions."""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from backend.app.claude_terminal.contracts import ClaudeTerminalSession, ClaudeTerminalState
from backend.app.claude_terminal.service import ClaudeTerminalSessionService
from backend.app.domain.identifiers import UserId
from backend.app.services.claude_terminal import ClaudeTerminalService, TerminalManagerError


NOW = datetime(2026, 10, 8, tzinfo=UTC)


class FakeTerminalSessions:
    def __init__(self) -> None:
        self.sessions: dict[object, ClaudeTerminalSession] = {}

    async def create(self, session: ClaudeTerminalSession) -> ClaudeTerminalSession:
        self.sessions[session.id] = session
        return session

    async def get_for_user(
        self, user_id: UserId, session_id: object,
    ) -> ClaudeTerminalSession | None:
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
def sessions() -> ClaudeTerminalSessionService:
    return ClaudeTerminalSessionService(
        FakeTerminalSessions(), session_ttl=timedelta(minutes=5), expiry_batch_limit=1)


@pytest.mark.asyncio
async def test_reaper_stops_runtime_before_marking_expired(
    sessions: ClaudeTerminalSessionService, owner: UserId,
) -> None:
    manager = AsyncMock()
    service = ClaudeTerminalService(sessions, manager)
    terminal = await sessions.create_for_user(owner, now=NOW)
    await sessions.activate_for_user(owner, terminal.id, runtime_reference="opaque", now=NOW)

    report = await service.reap_due(now=NOW + timedelta(minutes=6))

    manager.stop.assert_awaited_once_with(terminal.id)
    refreshed = await sessions.get_for_user(owner, terminal.id)
    assert refreshed is not None
    assert refreshed.state is ClaudeTerminalState.EXPIRED
    assert report.expired == 1
    assert report.failed == 0


@pytest.mark.asyncio
async def test_reaper_leaves_session_retryable_when_manager_is_unavailable(
    sessions: ClaudeTerminalSessionService, owner: UserId,
) -> None:
    manager = AsyncMock()
    manager.stop.side_effect = TerminalManagerError("TERMINAL_UNAVAILABLE")
    service = ClaudeTerminalService(sessions, manager)
    terminal = await sessions.create_for_user(owner, now=NOW)
    await sessions.activate_for_user(owner, terminal.id, runtime_reference="opaque", now=NOW)

    report = await service.reap_due(now=NOW + timedelta(minutes=6))

    refreshed = await sessions.get_for_user(owner, terminal.id)
    assert refreshed is not None
    assert refreshed.state is ClaudeTerminalState.ACTIVE
    assert report.expired == 0
    assert report.failed == 1


def test_reaper_cli_accepts_a_non_destructive_dry_run() -> None:
    from backend.app.cli.reap_claude_terminals import _parse_args

    assert _parse_args([]).dry_run is False
    assert _parse_args(["--dry-run"]).dry_run is True
