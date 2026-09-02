"""Persistence of claude session ids for conversation recovery (spec §6.1)."""
from datetime import datetime


def get_session(conn, scope: str, scope_id: int) -> str | None:
    row = conn.execute(
        "SELECT claude_session_id FROM chat_sessions WHERE scope=? AND scope_id=?",
        (scope, scope_id)).fetchone()
    return row["claude_session_id"] if row else None


def set_session(conn, scope: str, scope_id: int, session_id: str, *, now: datetime) -> None:
    conn.execute(
        "INSERT INTO chat_sessions (scope, scope_id, claude_session_id, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(scope, scope_id) DO UPDATE SET "
        "claude_session_id=excluded.claude_session_id, updated_at=excluded.updated_at",
        (scope, scope_id, session_id, now.isoformat()))
    conn.commit()


def add_message(conn, scope: str, scope_id: int, role: str, text: str, *, now: datetime) -> None:
    """Persist one copilot turn so the conversation survives leaving the chat (§6.1)."""
    conn.execute(
        "INSERT INTO chat_messages (scope, scope_id, role, text, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (scope, scope_id, role, text, now.isoformat()))
    conn.commit()


def get_messages(conn, scope: str, scope_id: int) -> list[dict]:
    """Return a scope's persisted turns in insertion order (oldest first)."""
    rows = conn.execute(
        "SELECT role, text, created_at FROM chat_messages "
        "WHERE scope=? AND scope_id=? ORDER BY id",
        (scope, scope_id)).fetchall()
    return [dict(r) for r in rows]
