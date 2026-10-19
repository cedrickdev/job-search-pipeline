"""The only adapter that imports Docker's Python SDK."""
from typing import Any


class DockerSdkGateway:
    """Translate the manager's narrow gateway protocol to docker-py calls."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_env(cls) -> "DockerSdkGateway":
        import docker
        return cls(docker.from_env())

    def create_volume(self, name: str, *, labels: dict[str, str]) -> None:
        self._client.volumes.create(name, labels=labels)

    def create_container(self, **kwargs: object) -> str:
        container = self._client.containers.create(**kwargs)
        return str(container.id)

    def start(self, runtime_reference: str) -> None:
        self._client.containers.get(runtime_reference).start()

    def stop_remove(self, runtime_reference: str) -> None:
        container = self._client.containers.get(runtime_reference)
        try:
            # The terminal runtime is disposable: prompt shutdown keeps the UI responsive.
            container.stop(timeout=1)
        finally:
            container.remove(force=True)

    def remove_volume(self, name: str) -> None:
        self._client.volumes.get(name).remove(force=True)

    def create_exec(self, runtime_reference: str, **kwargs: object) -> str:
        result = self._client.api.exec_create(runtime_reference, **kwargs)
        return str(result["Id"])

    def start_exec_socket(self, exec_id: str) -> object:
        socket = self._client.api.exec_start(exec_id, socket=True, tty=True)
        # docker-py wraps the hijacked socket in SocketIO on Unix transports.
        # The terminal bridge needs the underlying object exposing recv/sendall.
        return getattr(socket, "_sock", socket)

    def resize_exec(self, exec_id: str, *, cols: int, rows: int) -> None:
        self._client.api.exec_resize(exec_id, width=cols, height=rows)
