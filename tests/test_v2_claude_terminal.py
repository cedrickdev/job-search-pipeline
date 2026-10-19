from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.requests import HTTPConnection

from backend.app.api.dependencies import current_session, reject_cross_site_writes
from backend.app.claude_terminal.service import ClaudeTerminalSessionService
from backend.app.domain.identifiers import new_user_id
from backend.app.services.claude_terminal import ClaudeTerminalService, TerminalManagerError
from tests.test_claude_terminal_service import FakeTerminalSessions


class Manager:
    async def create(self, session_id, *, expires_at):
        del session_id, expires_at
        return "opaque-runtime"

    async def stop(self, session_id):
        del session_id


def test_invalid_terminal_config_returns_safe_503(monkeypatch):
    from fastapi import HTTPException
    from backend.app.api.dependencies import claude_terminal_settings
    from backend.app.core.settings import ClaudeTerminalSettings

    monkeypatch.setattr(ClaudeTerminalSettings, "from_env", classmethod(
        lambda cls: cls(enabled=True)))
    request = HTTPConnection({"type": "http", "app": SimpleNamespace(state=SimpleNamespace())})
    with pytest.raises(HTTPException) as failed:
        claude_terminal_settings(request)
    assert failed.value.status_code == 503
    assert failed.value.detail == "TERMINAL_MISCONFIGURED"


class UnavailableManager(Manager):
    async def create(self, session_id, *, expires_at):
        del session_id, expires_at
        raise TerminalManagerError("TERMINAL_UNAVAILABLE")


@pytest.mark.asyncio
async def test_terminal_start_exposes_safe_state_without_runtime_reference() -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    service = ClaudeTerminalService(
        ClaudeTerminalSessionService(FakeTerminalSessions(), session_ttl=timedelta(minutes=30)),
        Manager(),
    )

    terminal = await service.create_for_user(new_user_id(), now=now)

    assert terminal.state == "ACTIVE"
    assert terminal.runtime_reference == "opaque-runtime"


@pytest.mark.asyncio
async def test_terminal_start_failure_is_a_safe_failed_session() -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    service = ClaudeTerminalService(
        ClaudeTerminalSessionService(FakeTerminalSessions(), session_ttl=timedelta(minutes=30)),
        UnavailableManager(),
    )

    terminal = await service.create_for_user(new_user_id(), now=now)

    assert terminal.state == "FAILED"
    assert terminal.safe_error_code == "TERMINAL_UNAVAILABLE"
    assert terminal.runtime_reference is None


def test_cross_site_write_guard_ignores_websocket_handshakes() -> None:
    """The HTTP CSRF guard is router-wide, including the terminal WebSocket route."""
    websocket = HTTPConnection({
        "type": "websocket",
        "headers": [(b"origin", b"http://localhost:3000")],
    })

    assert reject_cross_site_writes(websocket) is None


@pytest.mark.asyncio
async def test_authenticated_websocket_skips_http_only_csrf_check() -> None:
    authenticated = SimpleNamespace(session=object())

    class Authentication:
        async def authenticate(self, *, token, now):
            del token, now
            return authenticated

    websocket = HTTPConnection({
        "type": "websocket",
        "headers": [(b"cookie", b"jobsearch_session=opaque-session")],
    })

    resolved = await current_session(
        websocket, Authentication(), SimpleNamespace(session_cookie_name="jobsearch_session"),
        datetime(2026, 10, 8, tzinfo=UTC))

    assert resolved is authenticated
