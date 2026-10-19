from datetime import UTC, datetime, timedelta
from uuid import UUID

from terminal_manager.runtime import TerminalRuntimeManager


SESSION_ID = UUID("11111111-1111-1111-1111-111111111111")
EXPIRY = datetime(2026, 10, 8, tzinfo=UTC) + timedelta(minutes=30)


class FakeDockerGateway:
    def __init__(self) -> None:
        self.created: dict[str, object] = {}
        self.volumes: list[str] = []
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.execs: list[dict[str, object]] = []
        self.resizes: list[tuple[str, int, int]] = []

    def create_volume(self, name: str, *, labels: dict[str, str]) -> None:
        self.volumes.append(name)

    def create_container(self, **kwargs: object) -> str:
        self.created = kwargs
        return "opaque-runtime-reference"

    def start(self, runtime_reference: str) -> None:
        assert runtime_reference == "opaque-runtime-reference"

    def stop_remove(self, runtime_reference: str) -> None:
        self.stopped.append(runtime_reference)

    def remove_volume(self, name: str) -> None:
        self.removed.append(name)

    def create_exec(self, runtime_reference: str, **kwargs: object) -> str:
        self.execs.append({"runtime_reference": runtime_reference, **kwargs})
        return "terminal-exec"

    def start_exec_socket(self, exec_id: str) -> object:
        assert exec_id == "terminal-exec"
        return object()

    def resize_exec(self, exec_id: str, *, cols: int, rows: int) -> None:
        self.resizes.append((exec_id, cols, rows))


def test_runtime_is_non_privileged_and_isolated() -> None:
    docker = FakeDockerGateway()
    manager = TerminalRuntimeManager(
        docker, image="jobsearch-claude-terminal:test", max_sessions=2,
        memory_limit="512m", nano_cpus=1_000_000_000, pids_limit=128,
    )

    runtime = manager.create(SESSION_ID, expires_at=EXPIRY)

    assert runtime.reference == "opaque-runtime-reference"
    assert runtime.container_name == f"jobsearch-terminal-{SESSION_ID}"
    assert docker.created["user"] == "claude"
    assert docker.created["privileged"] is False
    assert docker.created["network"] == "jobsearch_terminal_egress"
    assert docker.created["read_only"] is True
    assert docker.created["security_opt"] == ["no-new-privileges:true"]
    assert docker.created["cap_drop"] == ["ALL"]
    assert docker.created["pids_limit"] == 128
    assert docker.volumes == [
        f"jobsearch-terminal-workspace-{SESSION_ID}",
        f"jobsearch-terminal-config-{SESSION_ID}",
    ]


def test_runtime_passes_the_operator_gateway_and_model_to_claude_code() -> None:
    docker = FakeDockerGateway()
    manager = TerminalRuntimeManager(
        docker, image="jobsearch-claude-terminal:test", max_sessions=2,
        memory_limit="512m", nano_cpus=1_000_000_000, pids_limit=128,
        anthropic_api_key="test-gateway-key",
        anthropic_base_url="https://gateway.example.test",
        anthropic_model="claude-opus-5",
    )

    manager.create(SESSION_ID, expires_at=EXPIRY)

    assert docker.created["environment"] == {
        "ANTHROPIC_API_KEY": "test-gateway-key",
        "ANTHROPIC_BASE_URL": "https://gateway.example.test",
        "ANTHROPIC_MODEL": "claude-opus-5",
    }


def test_cleanup_removes_only_the_runtime_and_its_two_volumes() -> None:
    docker = FakeDockerGateway()
    manager = TerminalRuntimeManager(
        docker, image="jobsearch-claude-terminal:test", max_sessions=2,
        memory_limit="512m", nano_cpus=1_000_000_000, pids_limit=128,
    )
    runtime = manager.create(SESSION_ID, expires_at=EXPIRY)

    manager.stop(runtime)

    assert docker.stopped == ["opaque-runtime-reference"]
    assert docker.removed == [
        f"jobsearch-terminal-workspace-{SESSION_ID}",
        f"jobsearch-terminal-config-{SESSION_ID}",
    ]


def test_reap_stops_only_expired_managed_runtime() -> None:
    docker = FakeDockerGateway()
    manager = TerminalRuntimeManager(
        docker, image="jobsearch-claude-terminal:test", max_sessions=2,
        memory_limit="512m", nano_cpus=1_000_000_000, pids_limit=128,
    )
    runtime = manager.create(SESSION_ID, expires_at=EXPIRY)

    reaped = manager.reap(now=EXPIRY + timedelta(seconds=1))

    assert reaped == [SESSION_ID]
    assert docker.stopped == [runtime.reference]
    assert docker.removed == [runtime.workspace_volume, runtime.config_volume]
    assert manager.get(SESSION_ID) is None


def test_attach_uses_unprivileged_claude_tty_and_bounded_resize() -> None:
    docker = FakeDockerGateway()
    manager = TerminalRuntimeManager(
        docker, image="jobsearch-claude-terminal:test", max_sessions=2,
        memory_limit="512m", nano_cpus=1_000_000_000, pids_limit=128,
    )
    runtime = manager.create(SESSION_ID, expires_at=EXPIRY)

    attachment = manager.attach(runtime)
    manager.resize(attachment, cols=120, rows=42)

    assert attachment.exec_id == "terminal-exec"
    assert docker.execs == [{
        "runtime_reference": "opaque-runtime-reference", "cmd": ["claude"],
        "user": "claude", "tty": True, "stdin": True,
    }]
    assert docker.resizes == [("terminal-exec", 120, 42)]
