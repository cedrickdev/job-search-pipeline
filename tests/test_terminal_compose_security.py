from pathlib import Path

import yaml


def test_only_terminal_manager_mounts_docker_socket_and_anthropic_key() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    api = compose["services"]["api"]
    manager = compose["services"]["terminal-manager"]

    assert "/var/run/docker.sock:/var/run/docker.sock" not in api.get("volumes", [])
    assert "/var/run/docker.sock:/var/run/docker.sock" in manager["volumes"]
    assert "ANTHROPIC_API_KEY" not in api["environment"]
    # The terminal manager reads a dedicated file rather than Compose
    # interpolation, so an unrelated exported shell credential cannot silently
    # replace the project's gateway, and the Docker-socket-owning service does
    # not inherit the other application secrets in `.env`.
    assert manager["env_file"] == [{"path": "terminal.env", "required": False}]
    assert "ANTHROPIC_API_KEY" not in manager["environment"]
    assert "ports" not in manager
