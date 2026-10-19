"""Public API orchestration for the private Claude terminal manager."""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx
from fastapi import WebSocketDisconnect
from starlette.websockets import WebSocket

from backend.app.claude_terminal.contracts import ClaudeTerminalSession, ClaudeTerminalState
from backend.app.claude_terminal.service import ClaudeTerminalSessionService
from backend.app.domain.identifiers import ClaudeTerminalSessionId, UserId


class TerminalManagerError(RuntimeError):
    """A finite, public-safe manager failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TerminalManagerClient(Protocol):
    async def create(self, session_id: ClaudeTerminalSessionId, *, expires_at: datetime) -> str: ...
    async def stop(self, session_id: ClaudeTerminalSessionId) -> None: ...
    async def relay(self, session_id: ClaudeTerminalSessionId, websocket: WebSocket) -> None: ...


@dataclass(frozen=True, slots=True)
class ClaudeTerminalReapReport:
    """Secret-free tally of one bounded expiry pass."""

    expired: int = 0
    failed: int = 0


class InternalTerminalManagerClient:
    """Authenticated client; the Anthropic credential is never part of this contract."""

    def __init__(self, *, base_url: str, token: str, timeout_seconds: float = 10) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Terminal-Manager-Token": token}
        self._timeout = timeout_seconds

    async def create(self, session_id: ClaudeTerminalSessionId, *, expires_at: datetime) -> str:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/internal/sessions", headers=self._headers,
                    json={"session_id": str(session_id), "expires_at": expires_at.isoformat()},
                )
        except httpx.HTTPError as exc:
            raise TerminalManagerError("TERMINAL_UNAVAILABLE") from exc
        if response.status_code == 429:
            raise TerminalManagerError("TERMINAL_CAPACITY_REACHED")
        if response.status_code >= 400:
            raise TerminalManagerError("TERMINAL_START_FAILED")
        reference = response.json().get("runtime_reference")
        if not isinstance(reference, str) or not reference:
            raise TerminalManagerError("TERMINAL_START_FAILED")
        return reference

    async def stop(self, session_id: ClaudeTerminalSessionId) -> None:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.delete(
                    f"{self._base_url}/internal/sessions/{session_id}", headers=self._headers)
        except httpx.HTTPError as exc:
            raise TerminalManagerError("TERMINAL_UNAVAILABLE") from exc
        if response.status_code >= 500:
            raise TerminalManagerError("TERMINAL_UNAVAILABLE")

    async def relay(self, session_id: ClaudeTerminalSessionId, websocket: WebSocket) -> None:
        """Bridge validated browser terminal frames to the private manager only."""
        import asyncio

        import websockets

        scheme = "wss" if self._base_url.startswith("https://") else "ws"
        host = self._base_url.split("://", 1)[1]
        uri = f"{scheme}://{host}/internal/sessions/{session_id}/stream"
        try:
            async with websockets.connect(
                uri, additional_headers=self._headers, max_size=32_768,
                open_timeout=self._timeout,
            ) as manager:
                async def browser_to_manager() -> None:
                    try:
                        while True:
                            message = await websocket.receive_text()
                            if len(message.encode()) > 32_768:
                                await websocket.close(code=1009)
                                return
                            await manager.send(message)
                    except WebSocketDisconnect:
                        return

                async def manager_to_browser() -> None:
                    try:
                        async for message in manager:
                            data = message.encode() if isinstance(message, str) else message
                            await websocket.send_bytes(data)
                    except websockets.ConnectionClosed:
                        return

                tasks = {asyncio.create_task(browser_to_manager()),
                         asyncio.create_task(manager_to_browser())}
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            raise TerminalManagerError("TERMINAL_CONNECTION_CLOSED") from exc


class ClaudeTerminalService:
    def __init__(
            self, sessions: ClaudeTerminalSessionService, manager: TerminalManagerClient,
    ) -> None:
        self._sessions = sessions
        self._manager = manager

    async def create_for_user(self, user_id: UserId, *, now: datetime) -> ClaudeTerminalSession:
        session = await self._sessions.create_for_user(user_id, now=now)
        try:
            reference = await self._manager.create(session.id, expires_at=session.expires_at)
        except TerminalManagerError as exc:
            return await self._fail(session, exc.code, now=now)
        active = await self._sessions.activate_for_user(
            user_id, session.id, runtime_reference=reference, now=now)
        return active or await self._fail(session, "TERMINAL_START_FAILED", now=now)

    async def get_for_user(
            self, user_id: UserId, session_id: ClaudeTerminalSessionId,
    ) -> ClaudeTerminalSession | None:
        return await self._sessions.get_for_user(user_id, session_id)

    async def stop_for_user(
            self, user_id: UserId, session_id: ClaudeTerminalSessionId, *, now: datetime,
    ) -> ClaudeTerminalSession | None:
        session = await self._sessions.get_for_user(user_id, session_id)
        if session is None or session.state.is_final:
            return session
        try:
            await self._manager.stop(session_id)
        except TerminalManagerError:
            # The persisted state still becomes terminated: a later reaper retries
            # only manager-labelled runtimes, while the caller cannot reconnect.
            pass
        return await self._sessions.stop_for_user(user_id, session_id, now=now)

    async def relay_for_user(
            self, user_id: UserId, session_id: ClaudeTerminalSessionId, websocket: WebSocket,
    ) -> bool:
        session = await self._sessions.get_for_user(user_id, session_id)
        if session is None or session.state is not ClaudeTerminalState.ACTIVE:
            return False
        await self._manager.relay(session_id, websocket)
        return True

    async def reap_due(self, *, now: datetime) -> ClaudeTerminalReapReport:
        """Stop one bounded page of expired runtimes, then persist their terminal state.

        The stop is intentionally first.  A manager outage leaves the database row non-final,
        so the next scheduled pass retries it instead of claiming cleanup that did not happen.
        """
        expired = 0
        failed = 0
        for session in await self._sessions.due_for_expiry(now=now):
            try:
                await self._manager.stop(session.id)
            except TerminalManagerError:
                failed += 1
                continue
            terminal = await self._sessions.expire_for_user(
                session.owner_user_id, session.id, now=now)
            if terminal is not None and terminal.state is ClaudeTerminalState.EXPIRED:
                expired += 1
        return ClaudeTerminalReapReport(expired=expired, failed=failed)

    async def count_due(self, *, now: datetime) -> int:
        """Return the current bounded expiry page size without contacting Docker."""
        return len(await self._sessions.due_for_expiry(now=now))

    async def _fail(
            self, session: ClaudeTerminalSession, code: str, *, now: datetime,
    ) -> ClaudeTerminalSession:
        # The service intentionally stores a finite code, never a manager response.
        failed = await self._sessions.fail_for_user(
            session.owner_user_id, session.id, safe_error_code=code, now=now)
        return failed or session
