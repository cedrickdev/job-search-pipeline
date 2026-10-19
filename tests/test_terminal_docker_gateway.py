from terminal_manager.docker_gateway import DockerSdkGateway


class FakeVolumeCollection:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, str]]] = []
        self.removed: list[str] = []

    def create(self, name: str, *, labels: dict[str, str]):
        self.created.append((name, labels))
        return object()

    def get(self, name: str):
        return type("Volume", (), {"remove": lambda self, force: None})()


class FakeContainerCollection:
    def __init__(self) -> None:
        self.run_kwargs: dict[str, object] | None = None

    def create(self, **kwargs: object):
        self.run_kwargs = kwargs
        return type("Container", (), {"id": "docker-id", "start": lambda self: None})()

    def get(self, reference: str):
        assert reference == "docker-id"
        return type("Container", (), {"stop": lambda self, timeout: None, "remove": lambda self, force: None})()


class FakeDockerClient:
    def __init__(self) -> None:
        self.volumes = FakeVolumeCollection()
        self.containers = FakeContainerCollection()


def test_gateway_stops_ephemeral_runtime_without_a_long_grace_period() -> None:
    calls: list[tuple[str, object]] = []

    class Container:
        def stop(self, *, timeout: int) -> None:
            calls.append(("stop", timeout))

        def remove(self, *, force: bool) -> None:
            calls.append(("remove", force))

    class Containers:
        def get(self, reference: str) -> Container:
            assert reference == "docker-id"
            return Container()

    client = type("Client", (), {"containers": Containers()})()

    DockerSdkGateway(client).stop_remove("docker-id")

    assert calls == [("stop", 1), ("remove", True)]


def test_gateway_unwraps_docker_socket_io_for_terminal_reads() -> None:
    raw_socket = object()

    class Api:
        def exec_start(self, exec_id: str, *, socket: bool, tty: bool):
            assert exec_id == "exec-id"
            assert socket is True
            assert tty is True
            return type("SocketIO", (), {"_sock": raw_socket})()

    client = FakeDockerClient()
    client.api = Api()

    assert DockerSdkGateway(client).start_exec_socket("exec-id") is raw_socket


def test_gateway_passes_hardening_options_to_docker_sdk() -> None:
    client = FakeDockerClient()
    gateway = DockerSdkGateway(client)

    reference = gateway.create_container(
        image="runtime", name="terminal", user="claude", privileged=False,
        read_only=True, cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
        pids_limit=128, mem_limit="512m", nano_cpus=1, network="terminal-egress",
        labels={"com.jobsearch.terminal": "true"}, environment={"ANTHROPIC_API_KEY": "secret"},
        volumes={"workspace": {"bind": "/workspace", "mode": "rw"}},
    )

    assert reference == "docker-id"
    assert client.containers.run_kwargs == {
        "image": "runtime", "name": "terminal", "user": "claude", "privileged": False,
        "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
        "pids_limit": 128, "mem_limit": "512m", "nano_cpus": 1,
        "network": "terminal-egress", "labels": {"com.jobsearch.terminal": "true"},
        "environment": {"ANTHROPIC_API_KEY": "secret"},
        "volumes": {"workspace": {"bind": "/workspace", "mode": "rw"}},
    }
