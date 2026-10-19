"""Create and clean isolated Claude Code runtimes through a narrow Docker boundary."""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


class TerminalCapacityReached(RuntimeError):
    """The configured manager-wide runtime limit has been reached."""


class TerminalRuntimeUnavailable(RuntimeError):
    """Docker could not create, start or remove the requested runtime."""


class DockerGateway(Protocol):
    def create_volume(self, name: str, *, labels: dict[str, str]) -> None: ...
    def create_container(self, **kwargs: object) -> str: ...
    def start(self, runtime_reference: str) -> None: ...
    def stop_remove(self, runtime_reference: str) -> None: ...
    def remove_volume(self, name: str) -> None: ...
    def create_exec(self, runtime_reference: str, **kwargs: object) -> str: ...
    def start_exec_socket(self, exec_id: str) -> object: ...
    def resize_exec(self, exec_id: str, *, cols: int, rows: int) -> None: ...


@dataclass(frozen=True, slots=True)
class TerminalRuntime:
    """Opaque runtime metadata held only by the private manager."""

    session_id: UUID
    reference: str
    container_name: str
    workspace_volume: str
    config_volume: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TerminalAttachment:
    """One manager-private TTY attachment; never persisted or sent to the browser."""

    runtime: TerminalRuntime
    exec_id: str
    socket: object


class TerminalRuntimeManager:
    """Own Docker operations so public application services never need the socket."""

    def __init__(
            self, docker: DockerGateway, *, image: str, max_sessions: int,
            memory_limit: str, nano_cpus: int, pids_limit: int,
            network: str = "jobsearch_terminal_egress",
            anthropic_api_key: str = "",
            anthropic_base_url: str = "https://api.anthropic.com",
            anthropic_model: str | None = None,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        if nano_cpus < 1 or pids_limit < 1:
            raise ValueError("runtime limits must be positive")
        self._docker = docker
        self._image = image
        self._max_sessions = max_sessions
        self._memory_limit = memory_limit
        self._nano_cpus = nano_cpus
        self._pids_limit = pids_limit
        self._network = network
        self._anthropic_api_key = anthropic_api_key
        self._anthropic_base_url = anthropic_base_url
        self._anthropic_model = anthropic_model
        self._runtimes: dict[UUID, TerminalRuntime] = {}

    def create(self, session_id: UUID, *, expires_at: datetime) -> TerminalRuntime:
        existing = self._runtimes.get(session_id)
        if existing is not None:
            return existing
        if len(self._runtimes) >= self._max_sessions:
            raise TerminalCapacityReached("terminal capacity reached")

        token = str(session_id)
        labels = {
            "com.jobsearch.terminal": "true",
            "com.jobsearch.terminal.session": token,
            "com.jobsearch.terminal.expires-at": expires_at.isoformat(),
        }
        container_name = f"jobsearch-terminal-{token}"
        workspace_volume = f"jobsearch-terminal-workspace-{token}"
        config_volume = f"jobsearch-terminal-config-{token}"
        try:
            self._docker.create_volume(workspace_volume, labels=labels)
            self._docker.create_volume(config_volume, labels=labels)
            reference = self._docker.create_container(
                image=self._image,
                name=container_name,
                user="claude",
                privileged=False,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                pids_limit=self._pids_limit,
                mem_limit=self._memory_limit,
                nano_cpus=self._nano_cpus,
                network=self._network,
                labels=labels,
                environment=self._runtime_environment(),
                volumes={
                    workspace_volume: {"bind": "/workspace", "mode": "rw"},
                    config_volume: {"bind": "/workspace/.claude-config", "mode": "rw"},
                },
            )
            self._docker.start(reference)
        except Exception as exc:
            self._cleanup_volumes(workspace_volume, config_volume)
            raise TerminalRuntimeUnavailable("terminal runtime could not start") from exc

        runtime = TerminalRuntime(
            session_id=session_id, reference=reference, container_name=container_name,
            workspace_volume=workspace_volume, config_volume=config_volume,
            expires_at=expires_at,
        )
        self._runtimes[session_id] = runtime
        return runtime

    def stop(self, runtime: TerminalRuntime) -> None:
        self._runtimes.pop(runtime.session_id, None)
        try:
            self._docker.stop_remove(runtime.reference)
        finally:
            self._cleanup_volumes(runtime.workspace_volume, runtime.config_volume)

    def stop_session(self, session_id: UUID) -> bool:
        """Stop one known runtime; absence is already the desired postcondition."""
        runtime = self._runtimes.get(session_id)
        if runtime is None:
            return False
        self.stop(runtime)
        return True

    def reap(self, *, now: datetime) -> list[UUID]:
        """Stop only this manager's expired, in-memory runtimes.

        Orphan discovery is intentionally left to the scheduled application
        reaper, which can validate its database session ownership first.
        """
        expired = [runtime for runtime in self._runtimes.values() if runtime.expires_at <= now]
        for runtime in expired:
            self.stop(runtime)
        return [runtime.session_id for runtime in expired]

    def get(self, session_id: UUID) -> TerminalRuntime | None:
        return self._runtimes.get(session_id)

    def attach(self, runtime: TerminalRuntime) -> TerminalAttachment:
        """Start Claude as the non-root runtime user in a terminal TTY."""
        exec_id = self._docker.create_exec(
            runtime.reference, cmd=["claude"], user="claude", tty=True, stdin=True)
        return TerminalAttachment(
            runtime=runtime, exec_id=exec_id,
            socket=self._docker.start_exec_socket(exec_id),
        )

    def resize(self, attachment: TerminalAttachment, *, cols: int, rows: int) -> None:
        if not 1 <= cols <= 500 or not 1 <= rows <= 500:
            raise ValueError("terminal dimensions must be between 1 and 500")
        self._docker.resize_exec(attachment.exec_id, cols=cols, rows=rows)

    def _cleanup_volumes(self, workspace_volume: str, config_volume: str) -> None:
        for volume in (workspace_volume, config_volume):
            try:
                self._docker.remove_volume(volume)
            except Exception:
                # Cleanup is idempotent: an absent volume is already the target state.
                continue

    def _runtime_environment(self) -> dict[str, str]:
        """Pass only the operator-owned gateway settings into Claude Code."""
        environment = {
            "ANTHROPIC_API_KEY": self._anthropic_api_key,
            "ANTHROPIC_BASE_URL": self._anthropic_base_url,
        }
        if self._anthropic_model:
            environment["ANTHROPIC_MODEL"] = self._anthropic_model
        return environment
