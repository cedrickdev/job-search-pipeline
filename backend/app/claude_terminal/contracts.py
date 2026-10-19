"""Pure domain objects for isolated Claude terminal sessions."""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from backend.app.domain.identifiers import ClaudeTerminalSessionId, UserId


class ClaudeTerminalState(StrEnum):
    """The finite lifecycle persisted for a terminal runtime."""

    CREATING = "CREATING"
    ACTIVE = "ACTIVE"
    STOPPING = "STOPPING"
    TERMINATED = "TERMINATED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"

    @property
    def is_final(self) -> bool:
        return self in {
            ClaudeTerminalState.TERMINATED,
            ClaudeTerminalState.FAILED,
            ClaudeTerminalState.EXPIRED,
        }


@dataclass(frozen=True, slots=True)
class ClaudeTerminalSession:
    """Safe metadata for one user-owned terminal; never terminal contents or secrets."""

    id: ClaudeTerminalSessionId
    owner_user_id: UserId
    state: ClaudeTerminalState
    created_at: datetime
    last_activity_at: datetime
    expires_at: datetime
    runtime_reference: str | None = None
    ended_at: datetime | None = None
    safe_error_code: str | None = None


class ClaudeTerminalSessionRepository(Protocol):
    """Persistence boundary; every retrieval is explicitly owner-scoped."""

    async def create(self, session: ClaudeTerminalSession) -> ClaudeTerminalSession: ...

    async def get_for_user(
        self, user_id: UserId, session_id: ClaudeTerminalSessionId,
    ) -> ClaudeTerminalSession | None: ...

    async def save(self, session: ClaudeTerminalSession) -> ClaudeTerminalSession: ...

    async def list_expired_active(
        self, now: datetime, *, limit: int,
    ) -> tuple[ClaudeTerminalSession, ...]: ...
