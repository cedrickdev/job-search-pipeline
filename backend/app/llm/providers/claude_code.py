"""The Claude Code CLI as a provider-neutral `LLMProvider`.

This wraps V1's `server/chat.py` mechanics — the argv, the stream-json parsing, the
stale-session retry-once — behind the generic contract, so a business service asks
for text and gets it without knowing a subprocess ran (docs/LLM_PROVIDER_ARCHITECTURE.md
§10). V1's own module is left untouched; this is a parallel adapter over the same
transport, and the parity tests (§74) pin V1's behaviour before anything migrates to
this path.

The security invariant is the reason this is not a thin wrapper (§1). The Claude CLI
authenticates itself; the platform must never inject `ANTHROPIC_API_KEY`, and the
subprocess environment strips the whole `ANTHROPIC_*` namespace. That is enforced in
`SafeCliRunner` through `child_env()`, which this provider uses and never bypasses —
there is no code path here that adds a variable to the child's environment.

`CodexProvider` is a *separate* adapter, not a subclass of this one (§11): the two
CLIs share only the `SafeCliRunner` boundary, and coupling them through inheritance
would make a change to Claude's stream-json parsing silently reshape Codex.
"""
import json
from collections.abc import AsyncIterator

from backend.app.llm.capabilities import Capability
from backend.app.llm.contracts import (
    FinishReason,
    LLMMessage,
    LLMProviderMetadata,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    MessageRole,
    ProviderHealth,
    ProviderHealthStatus,
    ProviderTransport,
    TokenUsage,
)
from backend.app.llm.failures import (
    LLMError,
    LLMFailureCode,
    classify_provider_failure,
)
from backend.app.llm.providers.cli_runner import (
    CliRun,
    SafeCliRunner,
    resolve_binary,
)

PROVIDER_KEY = "claude_code"

# The environment variable that overrides which `claude` binary is run, matching
# V1's `resolve_claude_bin`. A deployment pointing at a specific build sets it; the
# tests point it at a fake binary.
CLAUDE_BIN_VARIABLE = "JOBSEARCH_CLAUDE_BIN"


def build_argv(claude_bin: str, *, resume: str | None = None) -> list[str]:
    """The Claude CLI argv, identical to V1's `server/chat.py::build_argv`.

    Read-only tools only (`Read,Grep,Glob`): a copilot subprocess must not be able to
    write, and Phase 11 does not widen that — action execution stays in the
    application (CLAUDE.md). `--resume` is appended only when a session id is carried,
    so a first turn and a resumed one differ by exactly those two arguments.
    """
    argv = [claude_bin, "--print", "--output-format", "stream-json", "--verbose",
            "--allowedTools", "Read,Grep,Glob"]
    if resume:
        argv += ["--resume", resume]
    return argv


def render_prompt(request: LLMRequest) -> str:
    """Flatten the typed messages into the single stdin string `--print` expects.

    The Claude CLI takes one prompt on stdin, so a multi-message request is rendered
    as labelled sections. The request's `system` (and any SYSTEM message) leads, then
    the turns in order. This is a transport detail of *this* adapter — another
    provider maps the same messages to its own shape — which is exactly why the
    flattening lives here and not in the contract.
    """
    parts: list[str] = []
    if request.system:
        parts.append(request.system)
    for message in request.messages:
        parts.append(f"{_label(message)}:\n{message.content}")
    return "\n\n".join(parts)


def _label(message: LLMMessage) -> str:
    return {
        MessageRole.SYSTEM: "SYSTEM",
        MessageRole.USER: "USER",
        MessageRole.ASSISTANT: "ASSISTANT",
        MessageRole.TOOL: f"TOOL[{message.name or 'result'}]",
    }[message.role]


class _StreamParser:
    """Parses Claude stream-json lines into text and a session id.

    The V2 counterpart of V1's `StreamAccumulator`, kept private to this adapter
    because stream-json is Claude's wire format and nothing else's. It accumulates
    text and remembers the session id from the `system` and `result` lines. A line
    that is not JSON is ignored on the streaming path (a partial write), but a run
    that produced *only* unparseable output is a `PROVIDER_PROTOCOL_ERROR`, decided
    by the provider from `saw_json`.
    """

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.saw_json = False
        self.usage = TokenUsage()
        self._text: list[str] = []

    def feed(self, line: str) -> list[str]:
        """Consume one line; return any text deltas it produced."""
        stripped = line.strip()
        if not stripped:
            return []
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return []
        self.saw_json = True
        typ = obj.get("type")
        deltas: list[str] = []
        if typ == "system" and obj.get("session_id"):
            self.session_id = obj["session_id"]
        elif typ == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    self._text.append(block["text"])
                    deltas.append(block["text"])
        elif typ == "result":
            if obj.get("session_id"):
                self.session_id = obj["session_id"]
            self.usage = _usage_from_result(obj)
        return deltas

    @property
    def text(self) -> str:
        return "".join(self._text)


def _usage_from_result(obj: dict[str, object]) -> TokenUsage:
    """Lift token counts and cost off a `result` line, keeping the unknown null.

    The CLI's `result` line may carry a `usage` object and a `total_cost_usd`. When a
    field is absent it stays `None` — never 0 (§58) — so a telemetry sum is over what
    was actually reported.
    """
    usage = obj.get("usage")
    prompt = completion = None
    if isinstance(usage, dict):
        prompt = _int_or_none(usage.get("input_tokens"))
        completion = _int_or_none(usage.get("output_tokens"))
    cost = obj.get("total_cost_usd")
    total = None
    if prompt is not None and completion is not None:
        total = prompt + completion
    return TokenUsage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None)


def _int_or_none(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


class ClaudeCodeProvider:
    """The Claude Code CLI, behind the `LLMProvider` contract.

    Holds a `SafeCliRunner` and resolves the binary per call, mirroring V1's
    process-per-request model. Streaming honours the stale-session retry-once rule
    (§74): a resume that dies non-zero *before producing any output* is retried once
    with a fresh session, and a resume that produced output is never retried, because
    retrying would replay tokens the caller already saw.
    """

    def __init__(self, *, runner: SafeCliRunner | None = None,
                 binary: str | None = None) -> None:
        self._runner = runner or SafeCliRunner()
        self._binary = binary

    @property
    def metadata(self) -> LLMProviderMetadata:
        # Reasoning control, structured output and tools are deliberately *not*
        # claimed: the CLI here is run read-only with stream-json text, and claiming
        # a capability the adapter does not implement would route a task to a dead
        # end. Session resume, streaming, system instructions and local execution are
        # what this transport genuinely offers; token/cost usage is reported when the
        # `result` line carries it.
        return LLMProviderMetadata(
            provider_key=PROVIDER_KEY,
            display_name="Claude Code CLI",
            transport=ProviderTransport.CLI,
            capabilities=frozenset({
                Capability.TEXT_GENERATION,
                Capability.STREAMING,
                Capability.SESSION_RESUME,
                Capability.SYSTEM_INSTRUCTIONS,
                Capability.LOCAL_EXECUTION,
                Capability.TOKEN_USAGE,
                Capability.COST_USAGE,
            }),
            priority=50,
        )

    def _bin(self) -> str:
        return self._binary or resolve_binary(env_var=CLAUDE_BIN_VARIABLE,
                                               command="claude")

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Run one turn to completion and return the whole answer.

        Drains `stream`, so the retry-once recovery and the failure classification
        are one implementation. A stream that ends in ERROR raises the corresponding
        `LLMError`, so a one-shot caller sees a failure as an exception, never as an
        empty `LLMResponse`.
        """
        response: LLMResponse | None = None
        async for event in self.stream(request):
            if event.response is not None:
                response = event.response
            if event.error_code is not None:
                raise LLMError(LLMFailureCode(event.error_code),
                               detail=event.error_detail)
        if response is None:
            raise LLMError(LLMFailureCode.PROVIDER_PROTOCOL_ERROR,
                           detail="the CLI produced no completion")
        return response

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Stream one turn, with the stale-session retry-once recovery (§74).

        Yields STARTED, then a TEXT_DELTA per text block, then COMPLETED with the
        assembled `LLMResponse`. On a stale resume it silently retries fresh; on a
        classified failure it yields a single ERROR event and stops. `finalize` is
        applied by the caller, not here — this adapter returns raw model text, and the
        Evidence Guard and the mandate gate live above the provider (§1, §46).
        """
        resume = (request.session.external_session_id
                  if request.session is not None else None)
        attempts = [resume, None] if resume else [None]
        yield LLMStreamEvent.started()
        for attempt_resume in attempts:
            parser = _StreamParser()
            run = CliRun()
            argv = build_argv(self._bin(), resume=attempt_resume)
            try:
                async for line in self._runner.stream_lines(
                        argv, stdin_text=render_prompt(request),
                        timeout_seconds=request.timeout_seconds, run=run):
                    for delta in parser.feed(line):
                        yield LLMStreamEvent.text_delta(delta)
            except LLMError as error:
                yield LLMStreamEvent.errored(error.code, error.detail)
                return
            except Exception as exc:
                normalized = classify_provider_failure(exc)
                yield LLMStreamEvent.errored(normalized.code, normalized.detail)
                return
            produced = bool(parser.text) or parser.saw_json
            # Stale resume: died non-zero before producing anything. Retry fresh once.
            if (attempt_resume is not None and not produced
                    and run.return_code not in (0, None)):
                continue
            if not parser.saw_json and run.return_code not in (0, None):
                # A non-zero exit with no parseable output on a fresh attempt: the CLI
                # failed and said nothing we can read. Treat as unavailable.
                yield LLMStreamEvent.errored(
                    LLMFailureCode.PROVIDER_UNAVAILABLE,
                    "the CLI exited without producing output")
                return
            yield LLMStreamEvent.completed(LLMResponse(
                text=parser.text,
                usage=parser.usage,
                finish_reason=FinishReason.STOP,
                model=request.model,
                external_session_id=parser.session_id))
            return

    async def healthcheck(self) -> ProviderHealth:
        """Probe the CLI without a real generation, by asking for its version.

        A cheap `--version` run: it exercises the same binary resolution and spawn
        path a real call would, so a missing binary shows as `UNAVAILABLE` here
        rather than at the first generation. It never sends a prompt, so it cannot
        cost a token. Auth state is not probed — the CLI manages its own auth and a
        version check does not touch it.
        """
        try:
            run = await self._runner.run_collected(
                [self._bin(), "--version"], timeout_seconds=10.0)
        except LLMError as error:
            return ProviderHealth(status=ProviderHealthStatus.UNAVAILABLE,
                                  detail=error.detail)
        if run.return_code == 0:
            return ProviderHealth(status=ProviderHealthStatus.HEALTHY)
        return ProviderHealth(status=ProviderHealthStatus.UNAVAILABLE,
                              detail="the CLI did not report a version")
