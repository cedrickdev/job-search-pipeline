from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from terminal_manager.app import create_app
from terminal_manager.runtime import TerminalRuntimeManager

TEST_MANAGER_TOKEN = "synthetic-terminal-test-token"  # noqa: S105 - fake local test credential


class FakeDockerGateway:
    def __init__(self, *, output: list[bytes] | None = None) -> None:
        self.socket = FakeRawSocket(output=output)
        self.resizes: list[tuple[int, int]] = []

    def create_volume(self, name: str, *, labels: dict[str, str]) -> None:
        del name, labels

    def create_container(self, **kwargs: object) -> str:
        del kwargs
        return "opaque-runtime"

    def start(self, runtime_reference: str) -> None:
        assert runtime_reference == "opaque-runtime"

    def stop_remove(self, runtime_reference: str) -> None:
        assert runtime_reference == "opaque-runtime"

    def remove_volume(self, name: str) -> None:
        del name

    def create_exec(self, runtime_reference: str, **kwargs: object) -> str:
        assert runtime_reference == "opaque-runtime"
        assert kwargs == {"cmd": ["claude"], "user": "claude", "tty": True, "stdin": True}
        return "opaque-exec"

    def start_exec_socket(self, exec_id: str) -> object:
        assert exec_id == "opaque-exec"
        return self.socket

    def resize_exec(self, exec_id: str, *, cols: int, rows: int) -> None:
        assert exec_id == "opaque-exec"
        self.resizes.append((cols, rows))


class FakeRawSocket:
    def __init__(self, *, output: list[bytes] | None = None) -> None:
        self._output = list(output or [])
        self._closed = Event()
        self.input = b""
        self.input_received = Event()

    def sendall(self, data: bytes) -> None:
        self.input += data
        self.input_received.set()

    def recv(self, _size: int) -> bytes:
        if self._output:
            return self._output.pop(0)
        self._closed.wait(timeout=2)
        return b""

    def close(self) -> None:
        self._closed.set()


def test_private_create_and_stop_require_manager_token() -> None:
    manager = TerminalRuntimeManager(
        FakeDockerGateway(), image="test", max_sessions=1, memory_limit="512m",
        nano_cpus=1, pids_limit=1,
    )
    client = TestClient(create_app(manager=manager, manager_token=TEST_MANAGER_TOKEN))
    session_id = UUID("11111111-1111-1111-1111-111111111111")
    payload = {"session_id": str(session_id), "expires_at": (datetime(2026, 10, 8, tzinfo=UTC) + timedelta(minutes=30)).isoformat()}

    assert client.post("/internal/sessions", json=payload).status_code == 401
    created = client.post(
        "/internal/sessions", json=payload,
        headers={"X-Terminal-Manager-Token": TEST_MANAGER_TOKEN},
    )
    assert created.status_code == 201
    assert created.json() == {"runtime_reference": "opaque-runtime"}

    stopped = client.delete(
        f"/internal/sessions/{session_id}",
        headers={"X-Terminal-Manager-Token": TEST_MANAGER_TOKEN},
    )
    assert stopped.status_code == 204


def _manager_with_runtime(*, output: list[bytes] | None = None) -> tuple[FakeDockerGateway, TerminalRuntimeManager, UUID]:
    docker = FakeDockerGateway(output=output)
    manager = TerminalRuntimeManager(
        docker, image="test", max_sessions=1, memory_limit="512m", nano_cpus=1, pids_limit=1,
    )
    session_id = UUID("11111111-1111-1111-1111-111111111111")
    manager.create(session_id, expires_at=datetime(2026, 10, 8, tzinfo=UTC) + timedelta(minutes=30))
    return docker, manager, session_id


def test_private_stream_rejects_missing_manager_token() -> None:
    _, manager, session_id = _manager_with_runtime()
    client = TestClient(create_app(manager=manager, manager_token=TEST_MANAGER_TOKEN))

    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(f"/internal/sessions/{session_id}/stream"):
            pass
    assert closed.value.code == 1008


def test_private_stream_forwards_input_and_valid_resize() -> None:
    docker, manager, session_id = _manager_with_runtime()
    client = TestClient(create_app(manager=manager, manager_token=TEST_MANAGER_TOKEN))

    with client.websocket_connect(
        f"/internal/sessions/{session_id}/stream",
        headers={"X-Terminal-Manager-Token": TEST_MANAGER_TOKEN},
    ) as socket:
        socket.send_json({"type": "input", "data": "hello"})
        assert docker.socket.input_received.wait(timeout=1)
        socket.send_json({"type": "resize", "cols": 120, "rows": 42})
        # The test client processes inbound frames asynchronously; the input event
        # confirms the bridge is live before checking the next frame's effect.
        for _ in range(50):
            if docker.resizes:
                break
            Event().wait(0.01)

    assert docker.socket.input == b"hello"
    assert docker.resizes == [(120, 42)]


def test_private_stream_forwards_raw_output_as_binary() -> None:
    _, manager, session_id = _manager_with_runtime(output=[b"Claude ready"])
    client = TestClient(create_app(manager=manager, manager_token=TEST_MANAGER_TOKEN))

    with client.websocket_connect(
        f"/internal/sessions/{session_id}/stream",
        headers={"X-Terminal-Manager-Token": TEST_MANAGER_TOKEN},
    ) as socket:
        assert socket.receive_bytes() == b"Claude ready"


def test_private_stream_survives_an_idle_docker_socket_timeout() -> None:
    docker, manager, session_id = _manager_with_runtime()

    class IdleThenOutputSocket(FakeRawSocket):
        def __init__(self):
            super().__init__(output=[b"still connected"])
            self.idle = True

        def recv(self, size):
            if self.idle:
                self.idle = False
                raise TimeoutError("idle Docker socket")
            return super().recv(size)

    docker.socket = IdleThenOutputSocket()
    with TestClient(create_app(manager=manager, manager_token=TEST_MANAGER_TOKEN)) as client:
        with client.websocket_connect(
            f"/internal/sessions/{session_id}/stream",
            headers={"X-Terminal-Manager-Token": TEST_MANAGER_TOKEN},
        ) as socket:
            assert socket.receive_bytes() == b"still connected"
