# tests/test_chat_core.py
"""Copilot subprocess core: argv, env stripping (no API key), action parsing,
stream accumulation, session-resume recovery, and subprocess lifecycle
(timeout, output cap, kill-on-exit/disconnect)."""
import asyncio
import json
import time

import httpx

from server import chat


def test_build_argv_read_only_no_resume():
    argv = chat.build_argv("/bin/claude")
    assert argv[:5] == ["/bin/claude", "--print", "--output-format", "stream-json", "--verbose"]
    assert "--allowedTools" in argv and "Read,Grep,Glob" in argv
    assert "--resume" not in argv
    # read-only: no write-capable tools requested
    joined = " ".join(argv)
    assert "Bash" not in joined and "Write" not in joined and "Edit" not in joined


def test_build_argv_resume():
    argv = chat.build_argv("/bin/claude", resume="sess-1")
    assert "--resume" in argv and "sess-1" in argv


def test_child_env_strips_anthropic_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-leak")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://leak")
    monkeypatch.setenv("HOME", "/Users/x")
    env = chat.child_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["HOME"] == "/Users/x"  # unrelated vars preserved


def test_parse_action_block_extracted():
    action = {"type": "set_status", "job_id": 5, "args": {"status": "Applied"},
              "label": "Mark as Applied"}
    text = f'Recommend applying.\n```action\n{json.dumps(action)}\n```\nDone.'
    cleaned, actions = chat.parse_action_blocks(text)
    assert actions == [action]
    assert "```action" not in cleaned and "Recommend applying." in cleaned


def test_parse_action_block_malformed_left_intact():
    cleaned, actions = chat.parse_action_blocks("```action\nnot json\n```")
    assert actions == []
    assert "not json" in cleaned


def test_parse_action_block_rejects_unknown_type():
    # "go" is a valid endpoint but NOT a copilot action type (§187) → rejected, stripped.
    cleaned, actions = chat.parse_action_blocks('```action\n{"type": "go"}\n```')
    assert actions == []
    assert "```action" not in cleaned


def test_parse_action_block_rejects_invalid_status():
    bad = '```action\n{"type": "set_status", "job_id": 5, "args": {"status": "Nope"}}\n```'
    cleaned, actions = chat.parse_action_blocks(bad)
    assert actions == []
    assert "```action" not in cleaned


def test_build_prompt_includes_context_and_message():
    p = chat.build_prompt({"job_id": 5, "company": "Alpha"}, "What's my fit?")
    assert "Alpha" in p and "What's my fit?" in p
    assert "Generative AI" in p  # defense-in-depth instruction present


def test_build_prompt_accepts_string_context():
    # Global scope passes the digest as a plain string — it must be embedded
    # verbatim (NOT JSON-quoted).
    p = chat.build_prompt("DIGEST: 3 jobs ready to apply.", "What should I do?")
    assert "DIGEST: 3 jobs ready to apply." in p
    assert '"DIGEST' not in p  # not json.dumps-wrapped


def test_preamble_guides_cv_regen_with_creativity():
    # The copilot must turn a plain "create/tailor a CV" request into a regen
    # action AND infer how bold to be from the wording. Lock that contract in the
    # preamble so the model is told to emit args.creativity for CV requests.
    p = chat.build_prompt(None, "create a bold CV for max JD match")
    assert "regen" in p
    # All three creativity levels are advertised so the model has the vocabulary.
    for level in ("conservative", "balanced", "bold"):
        assert level in p
    # The wording trigger that the user gave as the example is called out.
    assert "args.creativity" in p


def _mem_conn():
    from pipeline.db import connect, init_db
    conn = connect(":memory:")
    init_db(conn)
    return conn


def test_build_context_job_scope_is_server_assembled():
    from tests.helpers import seed_job
    conn = _mem_conn()
    job_id, _ = seed_job(conn, company="Alpha", title="Shift Lead",
                         description="Retail and service.", score=91)
    ctx = chat.build_context(conn, "job", job_id)
    assert ctx["job_id"] == job_id and ctx["company"] == "Alpha"
    assert ctx["score"]["score"] == 91
    assert "recent_events" in ctx and "fit" in ctx


def test_build_context_job_scope_unknown_job_is_none():
    conn = _mem_conn()
    assert chat.build_context(conn, "job", 424242) is None


def test_build_context_global_scope_returns_digest_string():
    from tests.helpers import seed_job
    conn = _mem_conn()
    seed_job(conn, company="Alpha")
    ctx = chat.build_context(conn, "global", 0)
    assert isinstance(ctx, str)  # daily digest text, not a dict


FAKE_BIN = '''#!/usr/bin/env python3
import sys, json
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-xyz"}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Here you go.\\n```action\\n{\\"type\\": \\"skip\\", \\"job_id\\": 1}\\n```"}]}}), flush=True)
print(json.dumps({"type": "result", "session_id": "sess-xyz"}), flush=True)
'''


def test_stream_chat_with_fake_binary(tmp_path):
    binp = tmp_path / "fakeclaude"
    binp.write_text(FAKE_BIN)
    binp.chmod(0o755)

    async def run():
        return [ev async for ev in chat.stream_chat("hello", claude_bin=str(binp))]

    events = asyncio.run(run())
    kinds = [e["event"] for e in events]
    assert "session" in kinds
    assert "token" in kinds
    assert "action_proposal" in kinds
    proposals = [e["data"] for e in events if e["event"] == "action_proposal"]
    assert proposals[0] == {"type": "skip", "job_id": 1}
    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["session_id"] == "sess-xyz"
    assert "```action" not in done["data"]["text"]
    # Final prose passes the mandate gate; `done` carries its verdict (value is
    # config-dependent, so assert only the contract — keys present, flags a list).
    assert "mandate_ok" in done["data"]
    assert isinstance(done["data"]["flags"], list)


# --- Session-resume recovery (spec §6.1) ----------------------------------

# Fails fast & silent when --resume is present (stale session); a fresh launch
# (no --resume) succeeds with a NEW session id. Branches on argv.
FAKE_BIN_STALE_RESUME = '''#!/usr/bin/env python3
import sys, json
sys.stdin.read()
if "--resume" in sys.argv:
    sys.stderr.write("No conversation found with that session ID\\n")
    sys.exit(1)
print(json.dumps({"type": "system", "subtype": "init", "session_id": "fresh-sess"}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Recovered."}]}}), flush=True)
print(json.dumps({"type": "result", "session_id": "fresh-sess"}), flush=True)
'''


def test_stream_chat_recovers_from_stale_resume(tmp_path):
    binp = tmp_path / "fakeclaude"
    binp.write_text(FAKE_BIN_STALE_RESUME)
    binp.chmod(0o755)

    async def run():
        return [ev async for ev in chat.stream_chat(
            "hi", claude_bin=str(binp), resume="stale-sess")]

    events = asyncio.run(run())
    kinds = [e["event"] for e in events]
    # stale --resume died with no output → a session_reset marker, then a fresh turn
    assert any(e["event"] == "info" and e["data"] == "session_reset" for e in events)
    # only ONE successful session announced (the fresh one), not the stale id
    sessions = [e["data"] for e in events if e["event"] == "session"]
    assert sessions == ["fresh-sess"]
    done = next(e for e in events if e["event"] == "done")
    # the NEW id is what the route persists → overwrites the chat_sessions row
    assert done["data"]["session_id"] == "fresh-sess"
    assert "Recovered." in done["data"]["text"]


# Streams output first, THEN crashes non-zero: the session was already accepted,
# so recovery must NOT retry (retrying would duplicate streamed tokens).
FAKE_BIN_PARTIAL_THEN_FAIL = '''#!/usr/bin/env python3
import sys, json
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-partial"}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "partial answer"}]}}), flush=True)
sys.exit(1)
'''


def test_stream_chat_no_retry_after_partial_output(tmp_path):
    binp = tmp_path / "fakeclaude"
    binp.write_text(FAKE_BIN_PARTIAL_THEN_FAIL)
    binp.chmod(0o755)

    async def run():
        return [ev async for ev in chat.stream_chat(
            "hi", claude_bin=str(binp), resume="sess-partial")]

    events = asyncio.run(run())
    assert [e["event"] for e in events].count("session") == 1  # exactly one attempt
    assert not any(e["event"] == "info" for e in events)       # no session_reset


# --- Subprocess lifecycle (spec §6.1: timeout, output cap, kill) ----------

FAKE_BIN_HANG = '''#!/usr/bin/env python3
import sys, time
sys.stdin.read()
time.sleep(30)
'''


def test_stream_chat_timeout_kills_process(tmp_path):
    binp = tmp_path / "fakeclaude"
    binp.write_text(FAKE_BIN_HANG)
    binp.chmod(0o755)

    async def run():
        return [ev async for ev in chat.stream_chat(
            "hi", claude_bin=str(binp), timeout=0.3)]

    start = time.monotonic()
    events = asyncio.run(run())
    elapsed = time.monotonic() - start
    assert any(e["event"] == "error" and e["data"] == "timeout" for e in events)
    assert elapsed < 5  # killed promptly, did NOT wait out the 30s sleep


FAKE_BIN_FLOOD = '''#!/usr/bin/env python3
import sys, json
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s"}), flush=True)
for _ in range(200):
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "x" * 1000}]}}), flush=True)
'''


def test_stream_chat_output_cap(tmp_path):
    binp = tmp_path / "fakeclaude"
    binp.write_text(FAKE_BIN_FLOOD)
    binp.chmod(0o755)

    async def run():
        return [ev async for ev in chat.stream_chat(
            "hi", claude_bin=str(binp), output_cap=2000)]

    events = asyncio.run(run())
    assert any(e["event"] == "error" and e["data"] == "output_cap" for e in events)


FAKE_BIN_FAIL_IMMEDIATELY = '''#!/usr/bin/env python3
import sys
sys.stdin.read()
sys.exit(1)
'''


def test_stream_chat_fresh_failure_terminates(tmp_path):
    # No resume + non-zero exit + no output: no recovery is possible (already
    # fresh) → ends cleanly with an empty, fail-closed `done`, never a retry.
    binp = tmp_path / "fakeclaude"
    binp.write_text(FAKE_BIN_FAIL_IMMEDIATELY)
    binp.chmod(0o755)

    async def run():
        return [ev async for ev in chat.stream_chat("hi", claude_bin=str(binp))]

    events = asyncio.run(run())
    assert not any(e["event"] == "info" for e in events)  # nothing to recover
    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["text"] == ""


FAKE_BIN_SLOW_STREAM = '''#!/usr/bin/env python3
import sys, json, time
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s"}), flush=True)
time.sleep(30)
print(json.dumps({"type": "result", "session_id": "s"}), flush=True)
'''


def test_stream_chat_kills_process_on_consumer_break(tmp_path):
    # Models kill-on-disconnect: the route breaks its `async for` when the client
    # disconnects, which closes this async generator. The `finally` must kill the
    # (otherwise 30s-hanging) subprocess rather than leak it.
    binp = tmp_path / "fakeclaude"
    binp.write_text(FAKE_BIN_SLOW_STREAM)
    binp.chmod(0o755)

    async def run():
        gen = chat.stream_chat("hi", claude_bin=str(binp))
        first = await gen.__anext__()  # consume the first (session) event
        await gen.aclose()             # consumer "disconnects"
        return first

    start = time.monotonic()
    first = asyncio.run(run())
    elapsed = time.monotonic() - start
    assert elapsed < 5  # process killed on aclose(), not waited out
    assert first["event"] in ("session", "token")


# --- Local model backend selection (Ollama / LM Studio) -------------------

def test_resolve_backend_defaults_to_local_claude():
    assert chat.resolve_backend(None)["backend"] == "claude_cli"
    assert chat.resolve_backend({})["backend"] == "claude_cli"
    assert chat.resolve_backend({"llm_backend": "claude_cli"})["backend"] == "claude_cli"


def test_resolve_backend_fills_ollama_defaults():
    cfg = chat.resolve_backend({"llm_backend": "ollama", "llm_base_url": "", "llm_model": ""})
    assert cfg["backend"] == "ollama"
    assert cfg["base_url"].startswith("http://localhost:11434")
    assert cfg["model"]  # a non-empty default


def test_resolve_backend_honors_explicit_endpoint():
    cfg = chat.resolve_backend({"llm_backend": "lmstudio",
                                "llm_base_url": "http://127.0.0.1:1234/v1",
                                "llm_model": "my-model"})
    assert cfg == {"backend": "lmstudio",
                   "base_url": "http://127.0.0.1:1234/v1", "model": "my-model"}


def test_parse_sse_delta_extracts_content():
    line = 'data: {"choices":[{"delta":{"content":"hi"}}]}'
    assert chat.parse_sse_delta(line) == "hi"


def test_parse_sse_delta_ignores_done_and_noise():
    assert chat.parse_sse_delta("data: [DONE]") is None
    assert chat.parse_sse_delta(": keep-alive") is None
    assert chat.parse_sse_delta('data: {"choices":[{"delta":{"role":"assistant"}}]}') is None
    assert chat.parse_sse_delta("data: not json") is None


def _sse(*chunks: str) -> str:
    lines = [f'data: {json.dumps({"choices": [{"delta": {"content": c}}]})}' for c in chunks]
    return "\n\n".join(lines) + "\n\ndata: [DONE]\n\n"


def test_stream_local_model_streams_tokens_and_parses_action():
    body = _sse("Approving ", "is fine.",
                '\n```action\n{"type": "skip", "job_id": 1}\n```')

    def handler(request):
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def run():
        return [ev async for ev in chat.stream_local_model(
            "hi", base_url="http://localhost:11434/v1", model="m",
            transport=httpx.MockTransport(handler))]

    events = asyncio.run(run())
    tokens = "".join(e["data"] for e in events if e["event"] == "token")
    assert "Approving is fine." in tokens
    proposals = [e["data"] for e in events if e["event"] == "action_proposal"]
    assert proposals == [{"type": "skip", "job_id": 1}]
    done = next(e for e in events if e["event"] == "done")
    assert "```action" not in done["data"]["text"]
    assert done["data"]["session_id"] is None  # stateless backend, no resume id
    assert isinstance(done["data"]["flags"], list)


def test_stream_local_model_unreachable_emits_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    async def run():
        return [ev async for ev in chat.stream_local_model(
            "hi", base_url="http://localhost:11434/v1", model="m",
            transport=httpx.MockTransport(handler))]

    events = asyncio.run(run())
    assert any(e["event"] == "error" and e["data"] == "backend_unreachable" for e in events)


def test_stream_local_model_http_error_status():
    def handler(request):
        return httpx.Response(404, text="model not found")

    async def run():
        return [ev async for ev in chat.stream_local_model(
            "hi", base_url="http://localhost:11434/v1", model="missing",
            transport=httpx.MockTransport(handler))]

    events = asyncio.run(run())
    assert any(e["event"] == "error" and e["data"].startswith("backend_http_") for e in events)


# --- collect_turn surfaces the mandate verdict (for prep generate, §6.2) ---

def test_collect_turn_surfaces_mandate_verdict(monkeypatch):
    async def fake_stream(stdin_text, *, settings=None, resume=None):
        yield {"event": "token", "data": "draft"}
        yield {"event": "done", "data": {
            "text": "draft", "session_id": None,
            "mandate_ok": False, "flags": ["anonymization_config_missing"]}}

    monkeypatch.setattr(chat, "stream_turn", fake_stream)
    result = asyncio.run(chat.collect_turn("hi"))
    assert result["text"] == "draft"
    assert result["mandate_ok"] is False
    assert result["flags"] == ["anonymization_config_missing"]


def test_collect_turn_fails_closed_when_done_missing(monkeypatch):
    # A backend that errors out before `done` (e.g. unreachable local server)
    # must leave collect_turn reporting unverified prose, not a silent ok.
    async def fake_stream(stdin_text, *, settings=None, resume=None):
        yield {"event": "error", "data": "backend_unreachable"}

    monkeypatch.setattr(chat, "stream_turn", fake_stream)
    result = asyncio.run(chat.collect_turn("hi"))
    assert result["text"] == ""
    assert result["mandate_ok"] is False
    assert result["flags"] == []
