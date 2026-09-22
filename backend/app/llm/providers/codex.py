"""The Codex CLI as a provider-neutral `LLMProvider`.

A separate adapter from `ClaudeCodeProvider`, deliberately not a subclass of it
(docs/LLM_PROVIDER_ARCHITECTURE.md §11). The two CLIs share exactly one thing — the
`SafeCliRunner` subprocess boundary, with its deadline, output cap, sanitized
environment and kill-on-exit — and nothing else: Codex has its own argv and its own
output shape, and coupling the two through inheritance would make a change to
Claude's stream-json parsing silently reshape Codex, or vice versa.

Codex is invoked in a non-interactive, read-only mode and its output is read as
plain text. Its wire output is untrusted like any CLI's (§73): an exit that produced
nothing usable is a `PROVIDER_PROTOCOL_ERROR` or `PROVIDER_UNAVAILABLE`, never a
half-parsed answer. Codex has no session-resume contract here, so it does not claim
`SESSION_RESUME` — a task that needs resume is never routed to it.

The Codex binary and invocation are configurable through the environment because the
exact CLI name and flags vary by install; the defaults below name the common case and
a deployment overrides them. Nothing here injects a credential into the environment —
the CLI manages its own auth, the same rule the Claude adapter follows.
"""
import os
import shlex
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

PROVIDER_KEY = "codex"

# Where the Codex binary is, and the arguments that put it in non-interactive
# read-only mode. Both overridable: the CLI name and its flags differ by install, and
# a deployment that runs a different build sets these rather than editing code.
CODEX_BIN_VARIABLE = "JOBSEARCH_CODEX_BIN"
CODEX_ARGS_VARIABLE = "JOBSEARCH_CODEX_ARGS"

# The conservative default: run once, print the answer, exchange nothing interactive.
# `exec` is Codex's non-interactive subcommand; a deployment whose Codex differs
# overrides the whole arg string through `CODEX_ARGS_VARIABLE`.
_DEFAULT_ARGS = ("exec", "--skip-git-repo-check")


def build_argv(codex_bin: str, extra_args: tuple[str, ...]) -> list[str]:
    """The Codex argv: the binary, then its non-interactive flags.

    Distinct from `claude_code.build_argv` on purpose — a shared builder would be the
    coupling §11 forbids. The prompt is fed on stdin, not as an argument, so a long
    prompt never risks an argv length limit and no prompt text lands in a process
    listing.
    """
    return [codex_bin, *extra_args]


def render_prompt(request: LLMRequest) -> str:
    """Flatten the typed messages into Codex's stdin prompt.

    Its own renderer, not Claude's: the two adapters format independently so neither
    constrains the other. System instruction first, then the turns in order.
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


def _resolve_args() -> tuple[str, ...]:
    """The Codex invocation arguments, from the environment or the default."""
    override = shlex.split(os.environ.get(CODEX_ARGS_VARIABLE, ""))
    return tuple(override) if override else _DEFAULT_ARGS


class CodexProvider:
    """The Codex CLI, behind the `LLMProvider` contract — its own adapter.

    Text in, text out, over the shared `SafeCliRunner`. No session resume, no
    structured output, no tools claimed: what it does is generate text locally, and
    claiming more would route a task to a capability the adapter does not implement.
    """

    def __init__(self, *, runner: SafeCliRunner | None = None,
                 binary: str | None = None,
                 args: tuple[str, ...] | None = None) -> None:
        self._runner = runner or SafeCliRunner()
        self._binary = binary
        self._args = args

    @property
    def metadata(self) -> LLMProviderMetadata:
        return LLMProviderMetadata(
            provider_key=PROVIDER_KEY,
            display_name="Codex CLI",
            transport=ProviderTransport.CLI,
            capabilities=frozenset({
                Capability.TEXT_GENERATION,
                Capability.STREAMING,
                Capability.SYSTEM_INSTRUCTIONS,
                Capability.LOCAL_EXECUTION,
            }),
            priority=60)

    def _bin(self) -> str:
        return self._binary or resolve_binary(env_var=CODEX_BIN_VARIABLE,
                                               command="codex")

    def _argv(self) -> list[str]:
        return build_argv(self._bin(), self._args or _resolve_args())

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Run Codex once and return its whole answer.

        Drains `stream`, so the failure classification is one implementation. An empty
        answer from a clean exit is a valid (if unhelpful) completion; an empty answer
        from a non-zero exit is a failure raised as `LLMError`.
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
                           detail="Codex produced no completion")
        return response

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Stream Codex's output line by line as TEXT_DELTAs, then COMPLETED.

        Codex prints plain text, so each stdout line is a delta and the assembled
        text is the answer. A non-zero exit with no output is `PROVIDER_UNAVAILABLE`;
        a spawn failure is already normalized by the runner.
        """
        yield LLMStreamEvent.started()
        run = CliRun()
        chunks: list[str] = []
        try:
            async for line in self._runner.stream_lines(
                    self._argv(), stdin_text=render_prompt(request),
                    timeout_seconds=request.timeout_seconds, run=run):
                text = line.rstrip("\n")
                if text:
                    chunks.append(text)
                    yield LLMStreamEvent.text_delta(text + "\n")
        except LLMError as error:
            yield LLMStreamEvent.errored(error.code, error.detail)
            return
        except Exception as exc:
            normalized = classify_provider_failure(exc)
            yield LLMStreamEvent.errored(normalized.code, normalized.detail)
            return
        if not chunks and run.return_code not in (0, None):
            yield LLMStreamEvent.errored(
                LLMFailureCode.PROVIDER_UNAVAILABLE,
                "Codex exited without producing output")
            return
        yield LLMStreamEvent.completed(LLMResponse(
            text="".join(chunks).rstrip("\n"),
            finish_reason=FinishReason.STOP,
            model=request.model))

    async def healthcheck(self) -> ProviderHealth:
        """Probe Codex with a version query, without a real generation."""
        try:
            run = await self._runner.run_collected(
                [self._bin(), "--version"], timeout_seconds=10.0)
        except LLMError as error:
            return ProviderHealth(status=ProviderHealthStatus.UNAVAILABLE,
                                  detail=error.detail)
        if run.return_code == 0:
            return ProviderHealth(status=ProviderHealthStatus.HEALTHY)
        return ProviderHealth(status=ProviderHealthStatus.UNAVAILABLE,
                              detail="Codex did not report a version")
