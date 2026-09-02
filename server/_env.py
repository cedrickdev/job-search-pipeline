"""Environment for child processes spawned by the server.

SECURITY MANDATE: the copilot IS Claude Code (local `claude` CLI) and the
transcriber is a local whisper binary — neither may ever see an Anthropic API
key. `child_env()` is the ONLY sanctioned way to build a subprocess env; every
`subprocess.run`/`create_subprocess_exec` in the server MUST pass `env=child_env()`.
"""
import os

# Strip the whole ANTHROPIC_* namespace rather than a fixed list of key names:
# a new credential variable (ANTHROPIC_ADMIN_KEY, ANTHROPIC_AUTH_*, ...) must
# not leak just because this list was never updated. The child `claude` CLI
# authenticates from its own stored credentials and picks its own model, so it
# needs none of these.
_STRIPPED_PREFIX = "ANTHROPIC_"


def child_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if not key.startswith(_STRIPPED_PREFIX)}
