"""`RetryPolicy` — how long a transient failure waits before it may be leased again (§38).

Phase 16 §38 asks for *bounded retries with backoff*: a transient fault is retried, but not
immediately and not forever. The attempt budget (`TaskRun.max_attempts`) is the bound; this policy
supplies the backoff — the delay before a re-queued task's `available_at`, growing with each spent
attempt so a struggling provider is not hammered. It is deliberately deterministic (no random
jitter) so a test can assert the exact `available_at` a retry lands on; a deployment that wants
jitter adds it at the queue-delivery layer, not in this pure schedule.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field


class RetryPolicy(BaseModel):
    """A capped exponential backoff, computed purely from the number of attempts already spent.

    The delay before attempt *n* becomes available is `base_delay * multiplier ** (n - 1)`, capped
    at `max_delay`. So with a one-second base and a doubling multiplier the first retry waits one
    second, the second two, the third four, and so on until the cap holds it flat. Frozen, so a
    worker's schedule cannot drift mid-run.
    """

    model_config = ConfigDict(frozen=True)

    base_delay_seconds: float = Field(default=2.0, gt=0.0)
    max_delay_seconds: float = Field(default=300.0, gt=0.0)
    multiplier: float = Field(default=2.0, ge=1.0)

    def backoff_for(self, attempts: int) -> timedelta:
        """The delay a task carrying `attempts` spent attempts waits before its next lease.

        `attempts` is the number already consumed (at least one, since a task is only rescheduled
        after it has run and failed), so the first retry uses the base delay. The growth is capped
        at `max_delay_seconds`, and computed with a guard so a large exponent cannot overflow.
        """
        exponent = max(attempts - 1, 0)
        try:
            raw = self.base_delay_seconds * (self.multiplier ** exponent)
        except OverflowError:
            raw = self.max_delay_seconds
        return timedelta(seconds=min(raw, self.max_delay_seconds))

    def next_available_at(self, *, attempts: int, as_of: datetime) -> datetime:
        """When a task re-queued at `as_of` after `attempts` spent attempts becomes leasable."""
        return as_of + self.backoff_for(attempts)
