"""Owner-scoped lifecycle for the integrated Claude Code terminal."""
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import SecretStr
from starlette.websockets import WebSocketState

from backend.app.api.dependencies import (
    ClaudeTerminals,
    CurrentSession,
    Now,
    auth_settings,
    claude_terminal_settings,
    now,
    session_factory,
)
from backend.app.api.schemas import ClaudeTerminalSessionResponse
from backend.app.claude_terminal.service import ClaudeTerminalSessionService
from backend.app.core.settings import ClaudeTerminalSettings
from backend.app.domain.identifiers import ClaudeTerminalSessionId
from backend.app.infrastructure.database.engine import session_scope
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyClaudeTerminalSessionRepository,
    SqlAlchemySessionRepository,
    SqlAlchemyUserRepository,
)
from backend.app.services.authentication import AuthenticationService
from backend.app.services.claude_terminal import InternalTerminalManagerClient, TerminalManagerError

router = APIRouter(prefix="/settings/claude-terminal", tags=["v2-claude-terminal"])


def _stream_url(session_id: ClaudeTerminalSessionId) -> str:
    return f"/api/v2/settings/claude-terminal/sessions/{session_id}/stream"


@router.post("/sessions", response_model=ClaudeTerminalSessionResponse,
             status_code=status.HTTP_201_CREATED)
async def create_session(current: CurrentSession, service: ClaudeTerminals,
                         instant: Now) -> ClaudeTerminalSessionResponse:
    terminal = await service.create_for_user(current.user.id, now=instant)
    return ClaudeTerminalSessionResponse.of(terminal, stream_url=_stream_url(terminal.id))


@router.get("/sessions/{session_id}", response_model=ClaudeTerminalSessionResponse)
async def read_session(session_id: ClaudeTerminalSessionId, current: CurrentSession,
                       service: ClaudeTerminals) -> ClaudeTerminalSessionResponse:
    terminal = await service.get_for_user(current.user.id, session_id)
    if terminal is None:
        raise HTTPException(status_code=404, detail="TERMINAL_SESSION_NOT_FOUND")
    return ClaudeTerminalSessionResponse.of(terminal, stream_url=_stream_url(terminal.id))


@router.delete("/sessions/{session_id}", response_model=ClaudeTerminalSessionResponse)
async def stop_session(session_id: ClaudeTerminalSessionId, current: CurrentSession,
                       service: ClaudeTerminals, instant: Now) -> ClaudeTerminalSessionResponse:
    terminal = await service.stop_for_user(current.user.id, session_id, now=instant)
    if terminal is None:
        raise HTTPException(status_code=404, detail="TERMINAL_SESSION_NOT_FOUND")
    return ClaudeTerminalSessionResponse.of(terminal, stream_url=_stream_url(terminal.id))


@router.websocket("/sessions/{session_id}/stream")
async def stream_session(
        websocket: WebSocket, session_id: ClaudeTerminalSessionId,
        settings: Annotated[ClaudeTerminalSettings, Depends(claude_terminal_settings)],
) -> None:
    origin = websocket.headers.get("origin")
    if origin not in settings.allowed_origins:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # WebSocket routes can outlive an ordinary request by hours. Resolve identity and
    # ownership in a short transaction before accepting, so an open terminal never
    # keeps a PostgreSQL transaction/row lock while its browser is idle.
    factory = session_factory(websocket)
    auth = auth_settings(websocket)
    async with session_scope(factory) as session:
        raw = websocket.cookies.get(auth.session_cookie_name)
        if not raw:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        authenticated = await AuthenticationService(
            SqlAlchemyUserRepository(session), SqlAlchemySessionRepository(session), auth,
        ).authenticate(token=SecretStr(raw), now=now())
        if authenticated is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        terminal = await ClaudeTerminalSessionService(
            SqlAlchemyClaudeTerminalSessionRepository(session),
            session_ttl=timedelta(minutes=settings.session_ttl_minutes),
        ).get_for_user(authenticated.user.id, session_id)
    if terminal is None or terminal.state != "ACTIVE":
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    try:
        await InternalTerminalManagerClient(
            base_url=settings.manager_url or "", token=settings.manager_token or "",
        ).relay(session_id, websocket)
    except (TerminalManagerError, WebSocketDisconnect):
        if websocket.client_state is WebSocketState.CONNECTED:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
    else:
        # The manager may reject a malformed terminal frame and finish its relay.
        # Complete the browser handshake explicitly instead of dropping its TCP socket.
        if websocket.client_state is WebSocketState.CONNECTED:
            await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
