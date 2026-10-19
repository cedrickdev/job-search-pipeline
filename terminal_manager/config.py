"""Private manager settings. This module is the only local owner of Anthropic auth."""
from os import environ
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TerminalManagerSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manager_token: str = Field(min_length=16, repr=False)
    anthropic_api_key: str = Field(min_length=1, repr=False)
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str | None = None
    runtime_image: str = "jobsearch-claude-terminal:local"
    max_sessions: int = Field(default=4, ge=1, le=100)
    memory_limit: str = "512m"
    nano_cpus: int = Field(default=1_000_000_000, ge=1)
    pids_limit: int = Field(default=128, ge=1)
    terminal_network: str = "jobsearch_terminal_egress"

    @field_validator("anthropic_base_url")
    @classmethod
    def validate_anthropic_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme != "https" or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
        ):
            raise ValueError("ANTHROPIC_BASE_URL must be an HTTPS API origin or base path")
        return normalized

    @field_validator("anthropic_model")
    @classmethod
    def normalize_anthropic_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @classmethod
    def from_env(cls) -> "TerminalManagerSettings":
        return cls(
            manager_token=environ.get("JOBSEARCH_TERMINAL_MANAGER_TOKEN", ""),
            anthropic_api_key=environ.get("ANTHROPIC_API_KEY", ""),
            anthropic_base_url=environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            anthropic_model=environ.get("ANTHROPIC_MODEL"),
            runtime_image=environ.get("JOBSEARCH_CLAUDE_TERMINAL_IMAGE", "jobsearch-claude-terminal:local"),
            max_sessions=int(environ.get("JOBSEARCH_TERMINAL_MAX_SESSIONS", "4")),
            memory_limit=environ.get("JOBSEARCH_TERMINAL_MEMORY_LIMIT", "512m"),
            nano_cpus=int(environ.get("JOBSEARCH_TERMINAL_NANO_CPUS", "1000000000")),
            pids_limit=int(environ.get("JOBSEARCH_TERMINAL_PIDS_LIMIT", "128")),
            terminal_network=environ.get("JOBSEARCH_TERMINAL_NETWORK", "jobsearch_terminal_egress"),
        )
