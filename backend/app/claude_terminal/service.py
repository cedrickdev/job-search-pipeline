"""Owner-scoped terminal session lifecycle without Docker or credentials."""
from dataclasses import replace
from datetime import datetime, timedelta

from backend.app.claude_terminal.contracts import (
    ClaudeTerminalSession,
    ClaudeTerminalSessionRepository,
    ClaudeTerminalState,
)
from backend.app.domain.identifiers import (
    ClaudeTerminalSessionId,
    UserId,
    new_claude_terminal_session_id,
)


class ClaudeTerminalSessionService:
    """Creates and transitions safe session metadata under the owning user only."""

    def __init__(self, sessions: ClaudeTerminalSessionRepository, *, session_ttl: timedelta,
                 expiry_batch_limit: int = 100) -> None:
        if session_ttl <= timedelta():
            raise ValueError("session_ttl must be positive")
        if expiry_batch_limit < 1:
            raise ValueError("expiry_batch_limit must be positive")
        self._sessions = sessions
        self._session_ttl = session_ttl
        self._expiry_batch_limit = expiry_batch_limit

    async def create_for_user(self, user_id: UserId, *, now: datetime) -> ClaudeTerminalSession:
        session = ClaudeTerminalSession(
            id=new_claude_terminal_session_id(), owner_user_id=user_id,
            state=ClaudeTerminalState.CREATING, created_at=now,
            last_activity_at=now, expires_at=now + self._session_ttl,
        )
        return await self._sessions.create(session)

    async def get_for_user(self, user_id: UserId, session_id: ClaudeTerminalSessionId,
                           ) -> ClaudeTerminalSession | None:
        return await self._sessions.get_for_user(user_id, session_id)

    async def activate_for_user(self, user_id: UserId, session_id: ClaudeTerminalSessionId,
                                *, runtime_reference: str, now: datetime,
                                ) -> ClaudeTerminalSession | None:
        if not runtime_reference.strip():
            raise ValueError("runtime_reference must not be empty")
        session = await self._sessions.get_for_user(user_id, session_id)
        if session is None or session.state is not ClaudeTerminalState.CREATING:
            return None
        return await self._sessions.save(replace(
            session, state=ClaudeTerminalState.ACTIVE,
            runtime_reference=runtime_reference, last_activity_at=now,
        ))

    async def stop_for_user(self, user_id: UserId, session_id: ClaudeTerminalSessionId,
                            *, now: datetime,
                            ) -> ClaudeTerminalSession | None:
        session = await self._sessions.get_for_user(user_id, session_id)
        if session is None:
            return None
        if session.state is ClaudeTerminalState.TERMINATED:
            return session
        if session.state.is_final:
            return session
        return await self._sessions.save(replace(
            session, state=ClaudeTerminalState.TERMINATED,
            ended_at=now, last_activity_at=now,
        ))

    async def fail_for_user(self, user_id: UserId, session_id: ClaudeTerminalSessionId,
                            *, safe_error_code: str, now: datetime,
                            ) -> ClaudeTerminalSession | None:
        session = await self._sessions.get_for_user(user_id, session_id)
        if session is None or session.state.is_final:
            return session
        return await self._sessions.save(replace(
            session, state=ClaudeTerminalState.FAILED, safe_error_code=safe_error_code,
            ended_at=now, last_activity_at=now,
        ))

    async def due_for_expiry(self, *, now: datetime) -> tuple[ClaudeTerminalSession, ...]:
        """Return one bounded page of non-final sessions whose lease has elapsed."""
        return await self._sessions.list_expired_active(now, limit=self._expiry_batch_limit)

    async def expire_for_user(self, user_id: UserId, session_id: ClaudeTerminalSessionId,
                              *, now: datetime) -> ClaudeTerminalSession | None:
        """Persist an expiry after the private manager confirmed its runtime is stopped."""
        session = await self._sessions.get_for_user(user_id, session_id)
        if session is None or session.state.is_final:
            return session
        return await self._sessions.save(replace(
            session, state=ClaudeTerminalState.EXPIRED,
            ended_at=now, last_activity_at=now,
        ))

    async def expire_due(self, *, now: datetime) -> tuple[ClaudeTerminalSession, ...]:
        due = await self.due_for_expiry(now=now)
        expired: list[ClaudeTerminalSession] = []
        for session in due:
            terminal = await self.expire_for_user(session.owner_user_id, session.id, now=now)
            if terminal is not None and terminal.state is ClaudeTerminalState.EXPIRED:
                expired.append(terminal)
        return tuple(expired)
