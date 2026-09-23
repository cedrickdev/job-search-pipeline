# tests/test_v2_llm_cli_adapters.py
"""The CLI adapters and their runner, against fake binaries — the V1 pattern (§74).

A CLI adapter's whole job is subprocess mechanics — the argv, the stream parsing, the
deadline and output cap, the stale-session retry-once — so a stub would paper over
exactly what needs testing. These tests run a real subprocess against a small Python
script standing in for `claude`/`codex`, the same way `test_chat_core.py` pins V1, and
assert the two security invariants Phase 11 must not weaken: the argv is explicit and
read-only, and the child environment never carries an `ANTHROPIC_*` credential (§1).
"""
import os

import pytest

from backend.app.llm.contracts import LLMMessage, LLMRequest, SessionContext
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.providers import claude_code
from backend.app.llm.providers.claude_code import ClaudeCodeProvider, build_argv
from backend.app.llm.providers.cli_runner import SafeCliRunner
from backend.app.llm.providers.codex import CodexProvider

pytestmark = pytest.mark.asyncio


def _request(**overrides) -> LLMRequest:
    fields = {"messages": (LLMMessage.user("hello"),)}
    fields.update(overrides)
    return LLMRequest(**fields)


def _write_bin(tmp_path, name: str, script: str) -> str:
    path = tmp_path / name
    path.write_text(script)
    path.chmod(0o755)
    return str(path)


# --- argv (no subprocess) --------------------------------------------------

async def test_claude_argv_is_read_only_and_stream_json():
    argv = build_argv("/bin/claude")
    assert argv[:5] == ["/bin/claude", "--print", "--output-format",
                        "stream-json", "--verbose"]
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,Glob"
    assert "--resume" not in argv


async def test_claude_argv_appends_resume_when_given():
    argv = build_argv("/bin/claude", resume="sess-1")
    assert argv[-2:] == ["--resume", "sess-1"]


# --- Claude Code adapter, fake binary --------------------------------------

_CLAUDE_OK = '''#!/usr/bin/env python3
import sys, json
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-xyz"}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Here you go."}]}}), flush=True)
print(json.dumps({"type": "result", "session_id": "sess-xyz",
                  "usage": {"input_tokens": 11, "output_tokens": 3},
                  "total_cost_usd": 0.002}), flush=True)
'''


async def test_claude_generate_returns_text_usage_and_session(tmp_path):
    binp = _write_bin(tmp_path, "fakeclaude", _CLAUDE_OK)
    provider = ClaudeCodeProvider(binary=binp)
    response = await provider.generate(_request())
    assert response.text == "Here you go."
    assert response.external_session_id == "sess-xyz"
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 3
    assert response.usage.total_tokens == 14
    assert response.usage.cost_usd == 0.002


# Fails fast & silent on --resume (stale), succeeds fresh with a new session.
_CLAUDE_STALE_RESUME = '''#!/usr/bin/env python3
import sys, json
sys.stdin.read()
if "--resume" in sys.argv:
    sys.stderr.write("No conversation found with that session ID\\n")
    sys.exit(1)
print(json.dumps({"type": "system", "session_id": "fresh-sess"}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Recovered."}]}}), flush=True)
print(json.dumps({"type": "result", "session_id": "fresh-sess"}), flush=True)
'''


async def test_claude_recovers_from_a_stale_resume(tmp_path):
    binp = _write_bin(tmp_path, "fakeclaude", _CLAUDE_STALE_RESUME)
    provider = ClaudeCodeProvider(binary=binp)
    response = await provider.generate(
        _request(session=SessionContext(external_session_id="stale-sess")))
    # Retried fresh: the new session id is what a caller persists, not the stale one.
    assert response.external_session_id == "fresh-sess"
    assert response.text == "Recovered."


_CLAUDE_FAIL_SILENT = '''#!/usr/bin/env python3
import sys
sys.stdin.read()
sys.stderr.write("boom\\n")
sys.exit(1)
'''


async def test_claude_a_nonzero_exit_with_no_output_is_unavailable(tmp_path):
    binp = _write_bin(tmp_path, "fakeclaude", _CLAUDE_FAIL_SILENT)
    provider = ClaudeCodeProvider(binary=binp)
    with pytest.raises(LLMError) as caught:
        await provider.generate(_request())
    assert caught.value.code is LLMFailureCode.PROVIDER_UNAVAILABLE


_CLAUDE_HANG = '''#!/usr/bin/env python3
import sys, time
sys.stdin.read()
time.sleep(30)
'''


async def test_claude_a_hang_hits_the_deadline(tmp_path):
    binp = _write_bin(tmp_path, "fakeclaude", _CLAUDE_HANG)
    provider = ClaudeCodeProvider(binary=binp)
    with pytest.raises(LLMError) as caught:
        await provider.generate(_request(timeout_seconds=0.3))
    assert caught.value.code is LLMFailureCode.PROVIDER_TIMEOUT


async def test_claude_healthcheck_is_healthy_when_the_binary_answers(tmp_path):
    binp = _write_bin(tmp_path, "fakeclaude",
                      "#!/usr/bin/env python3\nprint('1.0.0')\n")
    provider = ClaudeCodeProvider(binary=binp)
    health = await provider.healthcheck()
    assert health.status.value == "HEALTHY"


async def test_claude_healthcheck_is_unavailable_when_the_binary_is_missing():
    provider = ClaudeCodeProvider(binary="/nonexistent/claude-binary")
    health = await provider.healthcheck()
    assert health.status.value == "UNAVAILABLE"


# --- Codex adapter, fake binary --------------------------------------------

_CODEX_OK = '''#!/usr/bin/env python3
import sys
sys.stdin.read()
print("Codex says hello.")
'''


async def test_codex_generate_returns_its_text(tmp_path):
    binp = _write_bin(tmp_path, "fakecodex", _CODEX_OK)
    provider = CodexProvider(binary=binp, args=())
    response = await provider.generate(_request())
    assert response.text == "Codex says hello."


_CODEX_FAIL = '''#!/usr/bin/env python3
import sys
sys.stdin.read()
sys.exit(2)
'''


async def test_codex_a_nonzero_exit_with_no_output_is_unavailable(tmp_path):
    binp = _write_bin(tmp_path, "fakecodex", _CODEX_FAIL)
    provider = CodexProvider(binary=binp, args=())
    with pytest.raises(LLMError) as caught:
        await provider.generate(_request())
    assert caught.value.code is LLMFailureCode.PROVIDER_UNAVAILABLE


async def test_codex_does_not_claim_session_resume():
    from backend.app.llm.capabilities import Capability
    provider = CodexProvider(binary="/bin/true")
    assert Capability.SESSION_RESUME not in provider.metadata.capabilities


# --- the shared runner: security invariants (§1) ---------------------------

_ENV_DUMP = '''#!/usr/bin/env python3
import os, sys
sys.stdin.read()
for k in sorted(os.environ):
    if k.startswith("ANTHROPIC_"):
        print("LEAKED:" + k)
print("done")
'''


async def test_the_child_environment_strips_anthropic_credentials(tmp_path,
                                                                  monkeypatch):
    """The §1 invariant: no `ANTHROPIC_*` variable reaches a CLI subprocess."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-must-not-leak")
    binp = _write_bin(tmp_path, "envdump", _ENV_DUMP)
    runner = SafeCliRunner()
    run = await runner.run_collected([binp])
    assert run.return_code == 0
    assert not any("LEAKED:" in line for line in run.lines)


_FLOOD = '''#!/usr/bin/env python3
import sys
sys.stdin.read()
for _ in range(100000):
    print("x" * 100, flush=True)
'''


async def test_the_output_cap_stops_a_flood(tmp_path):
    binp = _write_bin(tmp_path, "flood", _FLOOD)
    runner = SafeCliRunner(output_cap_bytes=2000)
    with pytest.raises(LLMError) as caught:
        async for _ in runner.stream_lines([binp]):
            pass
    assert caught.value.code is LLMFailureCode.OUTPUT_LIMIT_EXCEEDED


async def test_a_missing_binary_is_unavailable():
    runner = SafeCliRunner()
    with pytest.raises(LLMError) as caught:
        await runner.run_collected(["/nonexistent/binary-xyz"])
    assert caught.value.code is LLMFailureCode.PROVIDER_UNAVAILABLE


async def test_the_binary_override_env_var_is_honoured(tmp_path, monkeypatch):
    binp = _write_bin(tmp_path, "fakeclaude",
                      "#!/usr/bin/env python3\nprint('9.9.9')\n")
    monkeypatch.setenv(claude_code.CLAUDE_BIN_VARIABLE, binp)
    provider = ClaudeCodeProvider()  # no explicit binary → resolves from the env var
    health = await provider.healthcheck()
    assert health.status.value == "HEALTHY"
    monkeypatch.delenv(claude_code.CLAUDE_BIN_VARIABLE, raising=False)
    assert os.environ.get(claude_code.CLAUDE_BIN_VARIABLE) is None
