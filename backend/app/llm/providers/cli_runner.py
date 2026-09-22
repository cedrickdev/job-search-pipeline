"""The one place the LLM layer runs a subprocess, and the rules it runs one under.

Both CLI providers — Claude Code and Codex — reach their model by spawning a local
binary and streaming its stdout. That is genuine subprocess work with genuine ways
to go wrong (a hung process, an unbounded output, a leaked credential in the
environment), so it is done once, here, under explicit rules, rather than twice with
two chances to forget one (docs/LLM_PROVIDER_ARCHITECTURE.md §13).

The rules, each a property `server/chat.py` already established for V1 and this
generalizes:

- **Explicit argv, never a shell.** `create_subprocess_exec`, not `_shell`: the
  command is a list the caller built, so a value inside it can never be reinterpreted
  as a shell metacharacter (CLAUDE.md, docs/ENGINEERING_STANDARDS.md §Security).
- **A sanitized environment.** The child gets `child_env()` from `server/_env.py`,
  which strips the entire `ANTHROPIC_*` namespace — the V1 security invariant Phase
  11 must not weaken (§1). No adapter passes a credential through the environment;
  the Claude CLI's auth stays CLI-managed, off this process's variables.
- **A deadline, an output cap, and a kill on exit.** A run that outlasts its timeout
  or overflows its cap is stopped and the process killed, so a wedged binary cannot
  hold a request open or exhaust memory. The `finally` kills any process still alive
  when the caller stops consuming — a client disconnect must not leave a subprocess.

This module knows nothing about *what* the bytes mean. It yields raw stdout lines;
the provider that owns the binary parses them (stream-json for Claude, whatever
Codex speaks). It raises `LLMError` for the failures it can see — the process would
not start, it timed out, it overflowed — and leaves protocol errors to the parser
above, which also reads the exit code and stderr this runner exposes.
"""
import asyncio
import os
import shutil
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field

from backend.app.llm.contracts import (
    DEFAULT_OUTPUT_CAP_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
)
from backend.app.llm.failures import LLMError, LLMFailureCode
from server._env import child_env


def resolve_binary(*, env_var: str, command: str) -> str:
    """Where a CLI binary is: an explicit override, then `PATH`, then the bare name.

    The same resolution `server/chat.py::resolve_claude_bin` uses, lifted so both CLI
    providers share it. An explicit env var wins so a deployment can point at a
    specific build; `shutil.which` finds it on `PATH`; the bare name is the last
    resort, and if it is not installed the spawn fails with `PROVIDER_UNAVAILABLE`,
    which is the honest answer.
    """
    return os.environ.get(env_var) or shutil.which(command) or command


@dataclass
class CliRun:
    """The evolving state of one CLI invocation.

    Mutable and passed by the runner to the caller of `stream_lines` through the
    generator's own return path is awkward, so instead a caller that needs the exit
    code and stderr uses `run_collected`, which fills this in. `return_code` is `None`
    until the process is reaped; `stderr` accumulates the child's error stream, read
    only to classify a failure and never forwarded verbatim into a detail.
    """

    return_code: int | None = None
    stderr: str = ""
    lines: list[str] = field(default_factory=list)


class SafeCliRunner:
    """Runs one CLI invocation under a deadline, an output cap and a sanitized env.

    Stateless and reusable: a provider holds one and calls it per request. The
    environment defaults to `child_env()`; a caller that overrides it is responsible
    for having built it the same sanitized way — this runner never re-adds a stripped
    variable.
    """

    def __init__(self, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                 output_cap_bytes: int = DEFAULT_OUTPUT_CAP_BYTES) -> None:
        self._timeout = timeout_seconds
        self._output_cap = output_cap_bytes

    async def _spawn(self, argv: Sequence[str],
                     env: Mapping[str, str] | None) -> asyncio.subprocess.Process:
        """Start the process with pipes and a sanitized environment, or fail typed."""
        environment = dict(env) if env is not None else child_env()
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise LLMError(LLMFailureCode.PROVIDER_UNAVAILABLE,
                           detail=f"the CLI binary could not be started: {argv[0]}"
                           ) from exc

    async def _pump(self, proc: asyncio.subprocess.Process, *,
                    stdin_text: str | None, timeout_seconds: float, cap: int
                    ) -> AsyncIterator[str]:
        """Feed stdin, then yield stdout lines under the deadline and the cap.

        The deadline parameter is `timeout_seconds`, not `timeout`: this owns an
        explicit `readline` deadline loop rather than an `asyncio.timeout` block (a
        line-by-line cap needs to re-arm per read), and the name states that so the
        async-correctness lint reads the intent rather than flagging the shorthand.
        """
        assert proc.stdout is not None
        if proc.stdin is not None:
            if stdin_text:
                proc.stdin.write(stdin_text.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        produced = 0
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise LLMError(LLMFailureCode.PROVIDER_TIMEOUT)
            try:
                line = await asyncio.wait_for(proc.stdout.readline(),
                                              timeout=remaining)
            except TimeoutError as exc:
                raise LLMError(LLMFailureCode.PROVIDER_TIMEOUT) from exc
            if not line:
                break
            produced += len(line)
            if produced > cap:
                raise LLMError(LLMFailureCode.OUTPUT_LIMIT_EXCEEDED)
            yield line.decode("utf-8", errors="replace")
        await proc.wait()

    async def stream_lines(
        self,
        argv: Sequence[str],
        *,
        stdin_text: str | None = None,
        timeout_seconds: float | None = None,
        output_cap_bytes: int | None = None,
        env: Mapping[str, str] | None = None,
        run: "CliRun | None" = None,
    ) -> AsyncIterator[str]:
        """Spawn `argv`, feed `stdin_text`, and yield decoded stdout lines.

        Raises `LLMError` before the first line if the process cannot start
        (`PROVIDER_UNAVAILABLE`), and after the last if it timed out
        (`PROVIDER_TIMEOUT`) or overflowed (`OUTPUT_LIMIT_EXCEEDED`).

        `run`, when given, is filled in as the stream progresses — `lines` counts
        what was yielded and `return_code` is set once the process is reaped. It is
        what lets the Claude adapter make its stale-session retry decision ("did this
        attempt produce anything, and did it exit non-zero?") without a second run.

        The `finally` kills a process still running when consumption stops — a broken
        pipe, a client disconnect, an exception upstream — so no subprocess outlives
        the request that started it.
        """
        timeout = timeout_seconds if timeout_seconds is not None else self._timeout
        cap = output_cap_bytes if output_cap_bytes is not None else self._output_cap
        proc = await self._spawn(argv, env)
        try:
            async for line in self._pump(proc, stdin_text=stdin_text,
                                         timeout_seconds=timeout, cap=cap):
                if run is not None:
                    run.lines.append(line)
                yield line
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            if run is not None:
                run.return_code = proc.returncode

    async def run_collected(
        self,
        argv: Sequence[str],
        *,
        stdin_text: str | None = None,
        timeout_seconds: float | None = None,
        output_cap_bytes: int | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CliRun:
        """Run to completion, returning every stdout line, the exit code and stderr.

        The one-shot counterpart of `stream_lines`, for a provider that parses the
        whole output at once and needs to tell an empty-but-nonzero exit (an auth
        failure) from empty content. It owns the process directly rather than reaching
        into the streaming generator, but the deadline, cap, env and kill-on-exit
        rules are the same code through `_spawn`/`_pump`. `stderr` is read after
        stdout drains and used only to classify — never forwarded into a detail.
        """
        timeout = timeout_seconds if timeout_seconds is not None else self._timeout
        cap = output_cap_bytes if output_cap_bytes is not None else self._output_cap
        run = CliRun()
        proc = await self._spawn(argv, env)
        try:
            async for line in self._pump(proc, stdin_text=stdin_text,
                                         timeout_seconds=timeout, cap=cap):
                run.lines.append(line)
            if proc.stderr is not None:
                stderr_bytes = await proc.stderr.read()
                run.stderr = stderr_bytes.decode("utf-8", errors="replace")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        run.return_code = proc.returncode
        return run
