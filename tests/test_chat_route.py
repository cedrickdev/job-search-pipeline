# tests/test_chat_route.py
"""SSE chat endpoint with a fake claude binary; session persistence + recovery."""
from pipeline.db import connect
from server import chat_store

FAKE_BIN = '''#!/usr/bin/env python3
import sys, json
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-aaa"}), flush=True)
# Emit a VALID action type (`set_status` -> an allowed status). `go` is an
# endpoint, NOT a copilot action type, so it would be stripped and yield no
# proposal — which would make the action_proposal assertion below fail.
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Approving is reasonable.\\n```action\\n{\\"type\\": \\"set_status\\", \\"job_id\\": 1, \\"args\\": {\\"status\\": \\"Approved\\"}, \\"label\\": \\"Approve\\"}\\n```"}]}}), flush=True)
print(json.dumps({"type": "result", "session_id": "sess-aaa"}), flush=True)
'''


def _conn(api_client):
    c = connect(api_client.db_path)
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def _install_fake(tmp_path, monkeypatch):
    binp = tmp_path / "fakeclaude"
    binp.write_text(FAKE_BIN)
    binp.chmod(0o755)
    monkeypatch.setenv("JOBSEARCH_CLAUDE_BIN", str(binp))


def test_chat_streams_and_persists_session(api_client, tmp_path, monkeypatch):
    _install_fake(tmp_path, monkeypatch)
    with api_client.stream("POST", "/api/chat",
                           json={"message": "should I apply?", "scope": "global", "scope_id": 0}) as r:
        body = "".join(r.iter_text())
    assert "action_proposal" in body
    assert "sess-aaa" in body
    # session recorded for recovery
    assert chat_store.get_session(_conn(api_client), "global", 0) == "sess-aaa"


def test_chat_unknown_job_returns_404(api_client):
    # A job-scoped request must name a real job (parity with the other
    # job-keyed routes). No fake binary needed: the check runs before streaming.
    r = api_client.post("/api/chat",
                        json={"message": "hi", "scope": "job", "scope_id": 10_000_000})
    assert r.status_code == 404


def test_chat_invalid_scope_returns_422(api_client):
    r = api_client.post("/api/chat",
                        json={"message": "hi", "scope": "../etc", "scope_id": 0})
    assert r.status_code == 422


def test_chat_session_store_roundtrip(api_client):
    from datetime import datetime
    conn = _conn(api_client)
    assert chat_store.get_session(conn, "job", 5) is None
    chat_store.set_session(conn, "job", 5, "sess-1", now=datetime(2026, 6, 11, 9, 0, 0))
    assert chat_store.get_session(conn, "job", 5) == "sess-1"
    chat_store.set_session(conn, "job", 5, "sess-2", now=datetime(2026, 6, 11, 9, 5, 0))
    assert chat_store.get_session(conn, "job", 5) == "sess-2"  # updated, not duplicated


def test_chat_message_store_roundtrip(api_client):
    from datetime import datetime
    conn = _conn(api_client)
    assert chat_store.get_messages(conn, "job", 7) == []
    chat_store.add_message(conn, "job", 7, "user", "hello there", now=datetime(2026, 6, 16, 9, 0, 0))
    chat_store.add_message(conn, "job", 7, "assistant", "hi back", now=datetime(2026, 6, 16, 9, 0, 1))
    msgs = chat_store.get_messages(conn, "job", 7)
    assert [(m["role"], m["text"]) for m in msgs] == [("user", "hello there"), ("assistant", "hi back")]
    assert all("created_at" in m for m in msgs)
    # Scoped by (scope, scope_id): another job's thread sees nothing.
    assert chat_store.get_messages(conn, "job", 8) == []


def test_chat_persists_user_and_assistant_messages(api_client, tmp_path, monkeypatch):
    # The user's complaint: the conversation disappears on leaving the chat.
    # Both turns must be persisted so the thread reloads intact (§6.1).
    _install_fake(tmp_path, monkeypatch)
    with api_client.stream("POST", "/api/chat",
                           json={"message": "should I apply?", "scope": "global", "scope_id": 0}) as r:
        "".join(r.iter_text())
    msgs = chat_store.get_messages(_conn(api_client), "global", 0)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["text"] == "should I apply?"
    # The assistant turn is the mandate-sanitized text the UI showed (the action
    # block is stripped). It is persisted regardless of the mandate verdict —
    # in this keyless setup the gate routinely fails closed, and dropping those
    # replies would leave the thread with gaps, the exact bug we are fixing.
    assert "Approving is reasonable." in msgs[1]["text"]


def test_chat_history_endpoint_returns_persisted_messages(api_client, tmp_path, monkeypatch):
    _install_fake(tmp_path, monkeypatch)
    with api_client.stream("POST", "/api/chat",
                           json={"message": "should I apply?", "scope": "global", "scope_id": 0}) as r:
        "".join(r.iter_text())
    body = api_client.get("/api/chat/history?scope=global&scope_id=0").json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["text"] == "should I apply?"


def test_chat_history_invalid_scope_422(api_client):
    assert api_client.get("/api/chat/history?scope=../etc&scope_id=0").status_code == 422


def test_chat_history_scoped_by_job(api_client, tmp_path, monkeypatch):
    # History is keyed by (scope, scope_id): a global turn must not leak into an
    # unrelated job thread.
    _install_fake(tmp_path, monkeypatch)
    with api_client.stream("POST", "/api/chat",
                           json={"message": "global q", "scope": "global", "scope_id": 0}) as r:
        "".join(r.iter_text())
    body = api_client.get("/api/chat/history?scope=job&scope_id=999").json()
    assert body["messages"] == []
