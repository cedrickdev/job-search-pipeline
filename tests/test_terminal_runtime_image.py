from pathlib import Path


def test_runtime_image_has_no_docker_socket_or_embedded_credential() -> None:
    source = Path("docker/claude-terminal.Dockerfile").read_text(encoding="utf-8")
    assert "/var/run/docker.sock" not in source
    assert "ANTHROPIC_API_KEY=" not in source
    assert "USER claude" in source
    assert "CLAUDE_CONFIG_DIR=/workspace/.claude-config" in source


def test_terminal_manager_image_installs_the_websocket_transport() -> None:
    source = Path("docker/terminal-manager.Dockerfile").read_text(encoding="utf-8")

    assert "websockets>=15,<16" in source


def test_managed_policy_contains_no_credentials() -> None:
    policy = Path("docker/claude-managed-settings.json").read_text(encoding="utf-8")
    assert "ANTHROPIC" not in policy
    assert "api_key" not in policy.lower()
