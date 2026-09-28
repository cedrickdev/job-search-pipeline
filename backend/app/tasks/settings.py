"""`TaskQueueSettings` — the deployment knobs for background execution (§33-35, §40).

Where Redis is, how many tasks each lane runs at once, how long a lease lives, how often a worker
polls for work and sweeps for dead workers' tasks, and the backoff a transient retry waits. Read
from the environment, frozen once built, and defaulting to safe, modest values so a deployment
that sets nothing still runs correctly (just conservatively). The Redis URL is kept out of the
`repr` because it can carry a password (§41: never log a credential).

The two lane concurrencies are separate on purpose: §35 requires the browser lane never share
execution with the general lane, so a wedged application submission cannot starve discovery,
matching or exports. The general default is a handful of workers; the browser default is one, both
because a browser is heavy and because serialising submissions is the safe posture.
"""
from __future__ import annotations

from collections.abc import Mapping
from os import environ
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from backend.app.tasks.retry import RetryPolicy

# The development Redis, matching docker-compose.yml. Not a secret: bound to loopback with no
# password, and a production deployment overrides it. Kept here so a dev run needs no env at all.
LOCAL_DEV_REDIS_URL: str = "redis://127.0.0.1:56379/0"

_REDIS_URL_VARIABLES: tuple[str, ...] = ("JOBSEARCH_REDIS_URL", "REDIS_URL")


def _read_int(source: Mapping[str, str], name: str, default: int) -> int:
    """Parse an integer environment variable, or refuse to guess (the settings-module rule)."""
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from error


def _read_float(source: Mapping[str, str], name: str, default: float) -> float:
    """Parse a float environment variable, or refuse to guess."""
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number; got {raw!r}") from error


class TaskQueueSettings(BaseModel):
    """Deployment configuration for the task queue and its workers (§33-35, §38, §40)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    redis_url: str = Field(default=LOCAL_DEV_REDIS_URL, repr=False)
    general_concurrency: int = Field(default=4, ge=1, le=256)
    browser_concurrency: int = Field(default=1, ge=1, le=64)
    lease_seconds: int = Field(default=300, ge=1)
    poll_seconds: float = Field(default=1.0, gt=0.0)
    stale_recovery_seconds: float = Field(default=30.0, gt=0.0)
    max_attempts: int = Field(default=5, ge=1)
    retry_base_delay_seconds: float = Field(default=2.0, gt=0.0)
    retry_max_delay_seconds: float = Field(default=300.0, gt=0.0)
    retry_multiplier: float = Field(default=2.0, ge=1.0)

    def retry_policy(self) -> RetryPolicy:
        """The backoff schedule these settings describe (§38)."""
        return RetryPolicy(
            base_delay_seconds=self.retry_base_delay_seconds,
            max_delay_seconds=self.retry_max_delay_seconds,
            multiplier=self.retry_multiplier,
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Self:
        """Read the queue settings from the environment, defaulting to safe, modest values.

        `env` defaults to `os.environ`; a test passes an explicit mapping so resolution is checked
        without mutating the process environment. The Redis URL resolves most-specific-first, the
        same discipline `DatabaseSettings` uses, so an unrelated `REDIS_URL` cannot silently
        redirect a project that sets `JOBSEARCH_REDIS_URL`.
        """
        source = environ if env is None else env
        redis_url = next(
            (source[name] for name in _REDIS_URL_VARIABLES if source.get(name, "").strip()),
            LOCAL_DEV_REDIS_URL,
        )
        return cls(
            redis_url=redis_url,
            general_concurrency=_read_int(source, "JOBSEARCH_WORKER_GENERAL_CONCURRENCY", 4),
            browser_concurrency=_read_int(source, "JOBSEARCH_WORKER_BROWSER_CONCURRENCY", 1),
            lease_seconds=_read_int(source, "JOBSEARCH_WORKER_LEASE_SECONDS", 300),
            poll_seconds=_read_float(source, "JOBSEARCH_WORKER_POLL_SECONDS", 1.0),
            stale_recovery_seconds=_read_float(
                source, "JOBSEARCH_WORKER_STALE_RECOVERY_SECONDS", 30.0),
            max_attempts=_read_int(source, "JOBSEARCH_WORKER_MAX_ATTEMPTS", 5),
            retry_base_delay_seconds=_read_float(
                source, "JOBSEARCH_WORKER_RETRY_BASE_SECONDS", 2.0),
            retry_max_delay_seconds=_read_float(
                source, "JOBSEARCH_WORKER_RETRY_MAX_SECONDS", 300.0),
            retry_multiplier=_read_float(source, "JOBSEARCH_WORKER_RETRY_MULTIPLIER", 2.0),
        )
