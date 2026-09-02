"""Local-CLI copilot subprocess (spec §6.1). No Anthropic API key, read-only tools,
message+context on stdin, process-per-request, timeout + output cap + kill-on-exit.
"""
import asyncio
import json
import os
import re
import shutil

import httpx

from pipeline import digest
from pipeline.statuses import STATUSES
from server import mandate
from server._env import child_env  # shared credential-stripping env (re-exported)

_ACTION_RE = re.compile(r"```action\s*\n(.*?)```", re.DOTALL)
_TIMEOUT = 120
_OUTPUT_CAP = 256 * 1024

# Conventional localhost endpoints/models for the local model servers. Used when
# the user picks the backend but leaves base_url/model blank in Settings.
_BACKEND_DEFAULTS = {
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "llama3.2"},
    "lmstudio": {"base_url": "http://localhost:1234/v1", "model": "local-model"},
}

# Spec §6.1/§187: the only action types the copilot may propose. Each maps to a
# typed endpoint (§5.2); `set_status` targets are re-validated against STATUSES.
_ALLOWED_ACTION_TYPES = {"regen", "set_status", "mark_applied", "draft_followup", "skip"}

_PREAMBLE = (
    "You are a copilot for a personal, local-only job-search tool. Be concise. "
    "When you recommend a state change, emit ONE fenced ```action block containing JSON "
    'shaped {"type": ..., "job_id": N, "args": {...}, "label": "human text"}. '
    'Valid types: "set_status" (args.status must be an allowed status), '
    '"mark_applied" (args.channel?), "regen" (args.notes?, args.creativity?), '
    '"draft_followup" (args.tone?), "skip". '
    "CV requests: when the user asks you to create, generate, tailor, rewrite, or improve a CV "
    'for a job, propose a "regen" action for that job. Put the emphasis and JD keywords to '
    "highlight into args.notes. Set args.creativity from how the user wants it to read: "
    '"bold" when they say things like "be bold", "stand out", "aggressive", or "max compatibility '
    'with the JD"; "conservative" when they say "play it safe" or "keep it conservative"; '
    'otherwise "balanced". '
    "Never reveal real client names: say 'a large retail client' or 'a nightlife venue'. "
    "Always write 'Generative AI', never 'GenAI'. Never invent numbers or technologies "
    "that are not in the provided context."
)


def resolve_claude_bin() -> str:
    return os.environ.get("JOBSEARCH_CLAUDE_BIN") or shutil.which("claude") or "claude"


def build_argv(claude_bin: str, *, resume: str | None = None) -> list[str]:
    argv = [claude_bin, "--print", "--output-format", "stream-json", "--verbose",
            "--allowedTools", "Read,Grep,Glob"]
    if resume:
        argv += ["--resume", resume]
    return argv


def build_prompt(context: str | dict | None, message: str) -> str:
    """Render the stdin prompt. `context` is assembled SERVER-SIDE by
    build_context (never supplied by the client): a digest string for global
    scope, or a compact dict for job scope."""
    parts = [_PREAMBLE]
    if context:
        body = context if isinstance(context, str) else json.dumps(context, indent=2, default=str)
        parts.append("CONTEXT:\n" + body)
    parts.append("USER:\n" + message)
    return "\n\n".join(parts)


def build_context(conn, scope: str, scope_id: int) -> str | dict | None:
    """Assemble copilot context from trusted server state (spec §6.1). The client
    NEVER supplies context — it only names a scope. Global scope → daily digest
    text; job scope → compact dict (score omitted when the job is unscored)."""
    if scope == "job":
        from server import queries  # lazy: queries imports gap_analysis/paths
        detail = queries.job_detail(conn, scope_id)
        if detail is None:
            return None
        job = detail["job"]
        ctx: dict = {
            "job_id": job["id"],
            "company": job["company"],
            "title": job["title"],
            "description": job.get("description"),
            "status": (detail.get("application") or {}).get("status"),
            "fit": detail.get("fit"),
            "recent_events": detail.get("events", [])[:10],
        }
        if detail.get("score"):
            ctx["score"] = detail["score"]
        return ctx
    return digest.build_digest(conn)


def _action_is_valid(action: object) -> bool:
    """Spec §197: known type, and for set_status an args.status within STATUSES."""
    if not isinstance(action, dict):
        return False
    if action.get("type") not in _ALLOWED_ACTION_TYPES:
        return False
    if action.get("type") == "set_status":
        args = action.get("args") or {}
        if args.get("status") not in STATUSES:
            return False
    return True


def parse_action_blocks(text: str) -> tuple[str, list[dict]]:
    actions: list[dict] = []

    def _collect(match: re.Match) -> str:
        try:
            action = json.loads(match.group(1))
        except json.JSONDecodeError:
            return match.group(0)  # malformed JSON: emit nothing, leave block in text (§197)
        if not _action_is_valid(action):
            return ""  # well-formed but rejected (unknown type / bad status): no card, strip block (§197)
        actions.append(action)
        return ""

    cleaned = _ACTION_RE.sub(_collect, text)
    return cleaned.strip(), actions


class StreamAccumulator:
    """Parses claude stream-json lines into UI events; defers action extraction to finish()."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self._text: list[str] = []

    def feed(self, line: bytes) -> list[dict]:
        events: list[dict] = []
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return events
        typ = obj.get("type")
        if typ == "system" and obj.get("session_id"):
            self.session_id = obj["session_id"]
            events.append({"event": "session", "data": self.session_id})
        elif typ == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    self._text.append(block["text"])
                    events.append({"event": "token", "data": block["text"]})
        elif typ == "result" and obj.get("session_id"):
            self.session_id = obj["session_id"]
        return events

    def finish(self) -> list[dict]:
        return finalize_turn("".join(self._text), self.session_id)


def finalize_turn(text: str, session_id: str | None) -> list[dict]:
    """Turn the full accumulated reply into the closing UI events, regardless of
    which backend produced it. Final-prose mandate gate (spec §6.2): the streamed
    `token` events are an ephemeral preview; this sanitized `text` is the
    AUTHORITATIVE message and the UI MUST replace the streamed preview with it on
    `done`. We do NOT pass an explicit forbidden list so check_prose fails CLOSED
    when the redaction config is absent (mandate_ok=False)."""
    cleaned, actions = parse_action_blocks(text)
    safe = mandate.apply_fixes(cleaned)
    verdict = mandate.check_prose(safe)
    out = [{"event": "action_proposal", "data": a} for a in actions]
    out.append({"event": "done", "data": {
        "text": safe,
        "session_id": session_id,
        "mandate_ok": verdict.ok,
        "flags": verdict.violations,
    }})
    return out


async def stream_chat(stdin_text, *, claude_bin, resume=None, timeout=_TIMEOUT, output_cap=_OUTPUT_CAP):
    """Stream one copilot turn.

    Session-resume recovery (spec §6.1): a stored `claude_session_id` can go
    stale (the CLI's local session store is rotated/cleared), in which case
    `claude --resume <id>` exits non-zero *before emitting anything* on stdout.
    Without recovery, every future turn for that scope would fail forever. So a
    resume attempt that dies non-zero before producing a single event is retried
    ONCE with a fresh session; the fresh attempt's `session` event then flows to
    the caller, which overwrites the chat_sessions row (INSERT…ON CONFLICT).
    A resume attempt that produced ANY output (already accepted the session) is
    never retried — retrying would duplicate streamed tokens. Timeout/output_cap
    are terminal and never trigger recovery (the session id is not the problem)."""
    attempts = [resume, None] if resume else [None]
    for attempt_resume in attempts:
        proc = await asyncio.create_subprocess_exec(
            *build_argv(claude_bin, resume=attempt_resume),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
        )
        assert proc.stdin and proc.stdout
        proc.stdin.write(stdin_text.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        acc = StreamAccumulator()
        produced = False  # True once claude emitted any stream-json line (session accepted)
        total = 0
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    yield {"event": "error", "data": "timeout"}
                    return
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    yield {"event": "error", "data": "timeout"}
                    return
                if not line:
                    break
                total += len(line)
                if total > output_cap:
                    yield {"event": "error", "data": "output_cap"}
                    return
                for ev in acc.feed(line):
                    produced = True
                    yield ev
            await proc.wait()
            # Recovery decision BEFORE flushing finish(): a stale --resume dies
            # non-zero with no output → drop the synthetic empty `done`, retry fresh.
            if attempt_resume is not None and not produced and proc.returncode not in (0, None):
                yield {"event": "info", "data": "session_reset"}
                continue
            for ev in acc.finish():
                yield ev
            return
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


def _drained_result(done: dict, actions: list[dict]) -> dict:
    """Shape a non-streaming turn result. Surfaces the §6.2 mandate verdict so
    callers (prep generate, draft_followup) can refuse to persist unverified
    prose. Fails closed: a turn that errored out before `done` has mandate_ok
    False and no flags (treated as unverified, never silently ok)."""
    return {"text": done["text"], "session_id": done["session_id"], "actions": actions,
            "mandate_ok": done.get("mandate_ok", False), "flags": done.get("flags", [])}


async def collect_chat(stdin_text, *, claude_bin, resume=None) -> dict:
    """Drain stream_chat to a single result (for non-streaming callers like prep generate)."""
    actions, done = [], {"text": "", "session_id": None}
    async for ev in stream_chat(stdin_text, claude_bin=claude_bin, resume=resume):
        if ev["event"] == "action_proposal":
            actions.append(ev["data"])
        elif ev["event"] == "done":
            done = ev["data"]
    return _drained_result(done, actions)


# --- Local model backends (Ollama / LM Studio) -----------------------------

def resolve_backend(settings: dict | None) -> dict:
    """Pick the copilot's model backend from saved settings (pipeline.settings).
    Defaults to the local `claude` CLI. For ollama/lmstudio, fill base_url/model
    from settings, falling back to each server's conventional localhost default."""
    backend = (settings or {}).get("llm_backend") or "claude_cli"
    if backend in _BACKEND_DEFAULTS:
        d = _BACKEND_DEFAULTS[backend]
        return {
            "backend": backend,
            "base_url": (settings.get("llm_base_url") or d["base_url"]),
            "model": (settings.get("llm_model") or d["model"]),
        }
    return {"backend": "claude_cli"}


def parse_sse_delta(line: str) -> str | None:
    """Extract assistant text from one OpenAI-compatible streaming line. Returns
    None for non-data lines, the [DONE] sentinel, and empty/role-only deltas."""
    if not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    try:
        return obj["choices"][0]["delta"].get("content")
    except (KeyError, IndexError, TypeError):
        return None


async def stream_local_model(stdin_text, *, base_url, model, timeout=_TIMEOUT,
                             output_cap=_OUTPUT_CAP, transport=None):
    """Stream one copilot turn from a local model server (Ollama / LM Studio) over
    its OpenAI-compatible endpoint. Stateless: the full context is re-sent every
    turn (build_context), so no session id is tracked. No credentials are sent —
    the server is local and keyless, which keeps the no-Anthropic-key mandate
    intact. `transport` is a test seam for httpx.MockTransport."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model,
               "messages": [{"role": "user", "content": stdin_text}],
               "stream": True}
    chunks: list[str] = []
    total = 0
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    yield {"event": "error", "data": f"backend_http_{resp.status_code}"}
                    return
                async for line in resp.aiter_lines():
                    total += len(line)
                    if total > output_cap:
                        yield {"event": "error", "data": "output_cap"}
                        return
                    delta = parse_sse_delta(line)
                    if delta:
                        chunks.append(delta)
                        yield {"event": "token", "data": delta}
    except httpx.HTTPError:
        # Server down / refused / read timeout: the local model isn't reachable.
        yield {"event": "error", "data": "backend_unreachable"}
        return
    for ev in finalize_turn("".join(chunks), None):
        yield ev


async def stream_turn(stdin_text, *, settings=None, resume=None):
    """Dispatch one copilot turn to the backend chosen in Settings. Default is the
    local `claude` CLI (session-resume capable); ollama/lmstudio go over HTTP."""
    cfg = resolve_backend(settings)
    if cfg["backend"] in _BACKEND_DEFAULTS:
        async for ev in stream_local_model(
                stdin_text, base_url=cfg["base_url"], model=cfg["model"]):
            yield ev
    else:
        async for ev in stream_chat(
                stdin_text, claude_bin=resolve_claude_bin(), resume=resume):
            yield ev


async def collect_turn(stdin_text, *, settings=None, resume=None) -> dict:
    """Backend-agnostic single-result drain (for non-streaming callers like prep
    generate). Mirrors collect_chat but honors the configured backend."""
    actions, done = [], {"text": "", "session_id": None}
    async for ev in stream_turn(stdin_text, settings=settings, resume=resume):
        if ev["event"] == "action_proposal":
            actions.append(ev["data"])
        elif ev["event"] == "done":
            done = ev["data"]
    return _drained_result(done, actions)
