"""`TaskFailure` — how a handler tells the worker whether a fault is worth another attempt.

A handler either returns (the work succeeded) or raises. Phase 16 §38 forbids blindly retrying
a validation, eligibility, CAPTCHA, MFA, approval or unsupported-flow failure, and requires
retrying a transient network/provider/database fault until the attempt budget is spent. The
handler is the only code that knows which of those a given exception is, so it says so by raising
`TaskFailure` with a `TaskFailureClass` and a stable, secret-free `reason` code.

Anything a handler does *not* wrap — an unexpected `KeyError`, a driver error, a bug — reaches the
worker as an ordinary exception. `classify_unexpected` maps those to a bounded `TRANSIENT` retry
under `UNEXPECTED_ERROR`: worth trying again in case it was a blip, but capped by `max_attempts`
so a real defect dead-letters with a durable trace rather than looping forever (§39). A handler
that means "never retry this" must say so explicitly by raising a `PERMANENT` `TaskFailure`.
"""
from __future__ import annotations

from backend.app.domain.task import TaskFailureClass

# The reason an exception no handler classified dead-letters or retries under. A stable
# SCREAMING_SNAKE_CASE code (docs/ENGINEERING_STANDARDS.md §Observability) so a dashboard can
# group "the worker hit something it did not expect" without parsing prose.
UNEXPECTED_ERROR: str = "UNEXPECTED_ERROR"

# The cap on how much of an unexpected exception's text is carried into `failure_detail`. The
# detail is a human breadcrumb, never a payload dump; truncating keeps a runaway message (or a
# leaked value) from bloating the row or the logs.
_MAX_DETAIL_LENGTH: int = 500


class TaskFailure(Exception):
    """A handler's typed verdict on a fault: its class, a stable reason, an optional detail.

    Raising this is how a handler drives the §38 retry decision without the worker having to guess
    from an exception type. `failure_class` says whether the fault may be retried at all;
    `reason` is the SCREAMING_SNAKE_CASE code the `TaskRun` records as provenance; `detail` is an
    optional secret-free human breadcrumb. The worker turns a `TRANSIENT` failure with an attempt
    left into a scheduled retry and everything else into a dead-letter, exactly as the domain's
    `TaskRun.should_retry` decides.
    """

    def __init__(self, *, failure_class: TaskFailureClass, reason: str,
                 detail: str | None = None) -> None:
        self.failure_class = failure_class
        self.reason = reason
        self.detail = detail
        super().__init__(f"{failure_class.value}:{reason}")

    @classmethod
    def transient(cls, reason: str, *, detail: str | None = None) -> TaskFailure:
        """A fault worth another attempt (a network/provider/database blip, a rate limit)."""
        return cls(failure_class=TaskFailureClass.TRANSIENT, reason=reason, detail=detail)

    @classmethod
    def permanent(cls, reason: str, *, detail: str | None = None) -> TaskFailure:
        """A fault re-running cannot fix (validation, eligibility, CAPTCHA, MFA, approval)."""
        return cls(failure_class=TaskFailureClass.PERMANENT, reason=reason, detail=detail)


def classify_unexpected(exc: BaseException) -> TaskFailure:
    """The verdict for an exception no handler wrapped: a bounded `TRANSIENT` retry (§38).

    An unclassified fault is treated as possibly transient — worth another attempt in case it was
    a transient blip — but it carries the generic `UNEXPECTED_ERROR` reason and a truncated,
    type-prefixed detail so an operator can see what surfaced without the message ever growing
    without bound. `max_attempts` still caps it, so a genuine bug dead-letters rather than spins.
    """
    text = f"{type(exc).__name__}: {exc}".strip()
    detail = text[:_MAX_DETAIL_LENGTH] if text else None
    return TaskFailure.transient(UNEXPECTED_ERROR, detail=detail)
