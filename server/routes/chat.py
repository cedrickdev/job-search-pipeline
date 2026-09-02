"""SSE chat endpoint. Streams copilot tokens + action proposals; persists session id."""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from pipeline import settings as settings_store
from pipeline.db import connect
from server import chat, chat_store
from server.deps import get_conn

router = APIRouter(prefix="/api")


class ChatBody(BaseModel):
    message: str
    scope: str = "global"
    scope_id: int = 0
    # NOTE: no `context` field. Context is assembled SERVER-SIDE from trusted DB
    # state (chat.build_context). Accepting client-supplied context would let the
    # caller smuggle real client names / fabricated numbers past the mandate gate.


@router.post("/chat")
async def post_chat(body: ChatBody, request: Request):
    # Validate the request's scope before touching the DB: only "global" and
    # "job" are meaningful. Anything else would silently fall through to the
    # global digest and persist a junk session row.
    if body.scope not in ("global", "job"):
        raise HTTPException(422, "invalid scope")
    db_path = request.app.state.db_path

    def _open():
        c = connect(db_path)
        c.execute("PRAGMA busy_timeout = 5000")
        return c

    # Resume id AND context both come from trusted server state; the client
    # supplies only message + scope + scope_id (spec §6.1).
    conn = _open()
    try:
        resume = chat_store.get_session(conn, body.scope, body.scope_id)
        context = chat.build_context(conn, body.scope, body.scope_id)
    finally:
        conn.close()
    # A job-scoped request must name a real job (parity with the other
    # job-keyed routes, which all 404 on an unknown id). build_context returns
    # None when queries.job_detail finds nothing.
    if body.scope == "job" and context is None:
        raise HTTPException(404, "job not found")
    stdin_text = chat.build_prompt(context, body.message)
    # Which model serves the turn (local claude CLI by default, or a local
    # Ollama / LM Studio server) is a user setting, read fresh from trusted state.
    llm_settings = settings_store.load(request.app.state.settings_path)

    # Persist the user's turn up front so the thread reloads intact even if the
    # stream errors or the client disconnects mid-reply (the bug we are fixing:
    # conversations vanished on leaving the chat).
    conn = _open()
    try:
        chat_store.add_message(conn, body.scope, body.scope_id, "user", body.message, now=datetime.now())
    finally:
        conn.close()

    async def event_gen():
        async for ev in chat.stream_turn(
            stdin_text, settings=llm_settings, resume=resume):
            if await request.is_disconnected():
                break
            if ev["event"] == "session":
                conn = _open()
                try:
                    chat_store.set_session(conn, body.scope, body.scope_id, ev["data"], now=datetime.now())
                finally:
                    conn.close()
            elif ev["event"] == "done":
                # Persist the assistant turn — the mandate-sanitized `text` the UI
                # shows. Persist REGARDLESS of mandate_ok: in this keyless setup the
                # gate routinely fails closed, and dropping those replies would
                # leave gaps in the reloaded thread, recreating the original bug.
                conn = _open()
                try:
                    chat_store.add_message(
                        conn, body.scope, body.scope_id, "assistant",
                        ev["data"].get("text", ""), now=datetime.now())
                finally:
                    conn.close()
            yield {"event": ev["event"], "data": json.dumps(ev["data"])}

    return EventSourceResponse(event_gen())


@router.get("/chat/history")
def get_chat_history(scope: str = "global", scope_id: int = 0, conn=Depends(get_conn)):
    """Replay a scope's persisted conversation so the UI can hydrate on mount."""
    if scope not in ("global", "job"):
        raise HTTPException(422, "invalid scope")
    return {"messages": chat_store.get_messages(conn, scope, scope_id)}
