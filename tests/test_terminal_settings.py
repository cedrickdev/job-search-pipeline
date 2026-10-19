from backend.app.core.settings import ClaudeTerminalSettings


def test_api_terminal_settings_never_read_anthropic_key() -> None:
    settings = ClaudeTerminalSettings.from_env({
        "JOBSEARCH_TERMINAL_ENABLED": "true",
        "JOBSEARCH_TERMINAL_MANAGER_URL": "http://terminal-manager:8788",
        "JOBSEARCH_TERMINAL_MANAGER_TOKEN": "manager-token-123456",
        "JOBSEARCH_TERMINAL_ALLOWED_ORIGINS": "http://localhost:3000,https://app.example.test",
        "ANTHROPIC_API_KEY": "must-not-be-consumed-by-api",
    })

    assert settings.enabled is True
    assert settings.manager_url == "http://terminal-manager:8788"
    assert settings.allowed_origins == ("http://localhost:3000", "https://app.example.test")
    assert "ANTHROPIC_API_KEY" not in settings.model_dump()


def test_enabled_terminal_requires_private_manager_token() -> None:
    try:
        ClaudeTerminalSettings.from_env({"JOBSEARCH_TERMINAL_ENABLED": "true"})
    except ValueError as error:
        assert "manager_token" in str(error)
    else:
        raise AssertionError("enabled terminal must require manager token")


def test_enabled_terminal_requires_valid_allowed_origins() -> None:
    base = {
        "JOBSEARCH_TERMINAL_ENABLED": "true",
        "JOBSEARCH_TERMINAL_MANAGER_URL": "http://terminal-manager:8788",
        "JOBSEARCH_TERMINAL_MANAGER_TOKEN": "manager-token-123456",
    }

    try:
        ClaudeTerminalSettings.from_env({**base, "JOBSEARCH_TERMINAL_ALLOWED_ORIGINS": "not a URL"})
    except ValueError as error:
        assert "allowed_origins" in str(error)
    else:
        raise AssertionError("enabled terminal must require valid browser origins")
