"""`TaskRun` and `TaskSpec` — the durable, idempotent record of one background job (§32-40).

A `TaskRun` is the platform's own memory of a unit of deferred work: what it is, who it belongs
to, and exactly where it stands between "queued" and "finished for good". Phase 16's background
execution rests on this being a *persisted domain value* rather than a message that lives only in
a queue — the row is the source of truth a worker leases, retries or dead-letters, so a crash
loses no work and a redelivery repeats none.

Five rules shape it, and each protects a promise the worker makes:

- **The id is the idempotency key made physical (§37).** A `TaskRun`'s id is
  `task_run_id(idempotency_key)`, so enqueuing the same job twice — a retried request, a
  redelivered message — computes the same primary key and collapses onto one row rather than
  running the work twice. The validator recomputes the id from the key and refuses any row where
  they disagree, exactly as `UsageEvent` guards its own derived id.
- **The lane falls out of the kind, never the caller (§35).** `lane_for_kind` maps each
  `TaskKind` to `GENERAL` or `BROWSER`; the validator refuses a row whose lane does not match its
  kind. A browser job therefore cannot be mislabelled onto the general lane and slip past the
  isolation the separate browser worker depends on.
- **The state machine is closed and coherent (§40).** `TaskStatus` is the four states a job can
  be in, and a `@model_validator` refuses any combination of lease, timing and failure fields
  that does not match the state: a `RUNNING` task holds a lease and has started but not finished;
  a `SUCCEEDED` one holds no lease and no failure; a `DEAD_LETTERED` one records the typed failure
  that ended it. An incoherent task cannot be constructed by any path.
- **Retryable is a property of the failure, not a guess (§38).** `TaskFailureClass` splits every
  failure into `TRANSIENT` (worth another attempt) and `PERMANENT` (never), and `should_retry`
  additionally requires an attempt to remain. A permanent failure — a validation error, a CAPTCHA,
  a missing approval — is never retried; a transient one is, until the attempt budget is spent,
  after which it dead-letters.
- **A lease is how a crash is survived (§40).** A `RUNNING` task carries a `lease_owner` and a
  `lease_expires_at`; `is_lease_stale` reports when that lease has lapsed so recovery can return a
  dead worker's task to the queue. The spent attempt is not refunded, so a task that repeatedly
  kills its worker still exhausts its budget rather than looping forever.

Pure domain value: `backend.app.domain` imports the standard library and Pydantic only
(docs/ARCHITECTURE.md §1). The dispatcher enqueues a `TaskSpec` and the worker drives a `TaskRun`
through these transitions (Phase 16 §32-40); this module knows nothing of Redis, ARQ, subprocesses
or the services the handlers ultimately call.
"""
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import Field, model_validator

from backend.app.domain.base import DomainModel, NonEmptyStr, ReasonCode, UtcDatetime
from backend.app.domain.identifiers import TaskRunId, UserId, task_run_id


class TaskLane(StrEnum):
    """The worker pool a task runs on — the isolation boundary §35 makes physical.

    - `GENERAL` — ordinary background work (discovery, matching, document generation, analytics,
      exports, retention): the general worker pool.
    - `BROWSER` — work that drives a real browser to submit an application: the separate
      browser-worker pool, which must never share concurrency with the general lane so a slow or
      wedged submission cannot starve every other job.
    """

    GENERAL = "GENERAL"
    BROWSER = "BROWSER"


class TaskKind(StrEnum):
    """The closed set of background jobs the platform enqueues (§32, §62).

    Each kind maps to exactly one lane via `lane_for_kind` and to one handler the worker
    dispatches to. A kind a caller invents fails to parse rather than enqueuing untyped work,
    exactly as every closed vocabulary in the domain. Only `APPLICATION_SUBMISSION` drives a
    browser; everything else is ordinary general-lane work.
    """

    OPPORTUNITY_DISCOVERY = "OPPORTUNITY_DISCOVERY"
    COMPANY_DISCOVERY = "COMPANY_DISCOVERY"
    GEOCODING = "GEOCODING"
    MATCHING = "MATCHING"
    DOCUMENT_GENERATION = "DOCUMENT_GENERATION"
    CAREER_ANALYTICS = "CAREER_ANALYTICS"
    ACCOUNT_EXPORT = "ACCOUNT_EXPORT"
    RETENTION_SWEEP = "RETENTION_SWEEP"
    APPLICATION_SUBMISSION = "APPLICATION_SUBMISSION"


# The kinds that drive a real browser and therefore belong to the isolated browser lane (§35).
# A frozenset, not a scattered `if`, so "what needs the browser worker" is one auditable fact.
_BROWSER_KINDS: frozenset[TaskKind] = frozenset({TaskKind.APPLICATION_SUBMISSION})


def lane_for_kind(kind: TaskKind) -> TaskLane:
    """The lane a kind must run on — the single mapping the isolation rule rests on (§35).

    Derived from the kind alone, so a caller can never place a browser job on the general lane:
    `TaskSpec.lane` and `TaskRun`'s validator both defer to this, making a mislabelled task
    impossible to construct rather than merely discouraged.
    """
    return TaskLane.BROWSER if kind in _BROWSER_KINDS else TaskLane.GENERAL


class TaskStatus(StrEnum):
    """The lifecycle of one task run — the closed set the state machine allows (§40).

    - `QUEUED` — waiting to be leased; holds no lease and has not finished. It may already carry a
      `started_at` and the failure of a prior attempt (a transient retry) or be pristine (a fresh
      enqueue).
    - `RUNNING` — leased by a worker: `lease_owner`/`lease_expires_at` and `started_at` are set,
      `finished_at` is unset, no failure is recorded, and at least one attempt has been spent.
    - `SUCCEEDED` — terminal success: no lease, `started_at`/`finished_at` set, no failure.
    - `DEAD_LETTERED` — terminal failure (permanent, or transient with no attempt left): no lease,
      `started_at`/`finished_at` set, and the typed failure that ended it recorded (§39).
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    DEAD_LETTERED = "DEAD_LETTERED"


class TaskFailureClass(StrEnum):
    """Whether a failure is worth another attempt — the §38 retry decision, made typed.

    - `TRANSIENT` — a fault that may clear on its own: a network or provider outage, a rate limit,
      a transient database or queue error. Retried until the attempt budget is spent.
    - `PERMANENT` — a fault re-running cannot fix: a validation or eligibility rejection, a
      CAPTCHA or MFA wall, a missing human approval, an unsupported ATS, a permanent auth failure.
      Never retried — it dead-letters immediately (§38).
    """

    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"

    @property
    def is_retryable(self) -> bool:
        """Whether a failure of this class may be retried at all (before counting attempts)."""
        return self is TaskFailureClass.TRANSIENT

class TaskRun(DomainModel):
    """One durable background task run and exactly where it stands (§32-40).

    User-owned when a job belongs to an account (read `WHERE user_id = ?`); `user_id` is `None`
    for an operator job like a retention sweep, which no account owns. The id is
    `task_run_id(idempotency_key)`, so a re-enqueue collides rather than duplicating. `payload`
    carries the handler's inputs (ids, never secrets); `max_attempts`/`attempts` bound retries;
    `available_at` is when it next becomes leasable (a backoff pushes it forward); the lease pair
    records which worker holds it and until when; the failure triple records the typed reason a
    retry or dead-letter carries. The transitions below produce new validated instances rather
    than mutating in place — a `DomainModel` is frozen — so an incoherent state is unreachable.
    """

    id: TaskRunId
    user_id: UserId | None = None
    kind: TaskKind
    lane: TaskLane
    status: TaskStatus
    idempotency_key: NonEmptyStr
    payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(ge=1)
    attempts: int = Field(ge=0)
    available_at: UtcDatetime
    lease_owner: NonEmptyStr | None = None
    lease_expires_at: UtcDatetime | None = None
    last_failure_class: TaskFailureClass | None = None
    failure_reason: ReasonCode | None = None
    failure_detail: NonEmptyStr | None = None
    enqueued_at: UtcDatetime
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _state_is_coherent(self) -> Self:
        if self.id != task_run_id(self.idempotency_key):
            raise ValueError(
                "a TaskRun id must be task_run_id(idempotency_key); the derived id is what makes "
                "a re-enqueue idempotent by construction")
        if self.lane is not lane_for_kind(self.kind):
            raise ValueError(
                "a TaskRun lane must be lane_for_kind(kind); a browser job cannot be relabelled "
                "onto the general lane")
        if self.attempts > self.max_attempts:
            raise ValueError("a TaskRun cannot have spent more attempts than max_attempts")

        has_lease = self.lease_owner is not None
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError(
                "a TaskRun lease is both-or-neither: lease_owner and lease_expires_at are set "
                "together or both unset")

        has_failure = self.last_failure_class is not None
        if (self.failure_reason is None) == has_failure:
            raise ValueError(
                "a TaskRun failure is both-or-neither: last_failure_class and failure_reason are "
                "set together or both unset")
        if self.failure_detail is not None and self.failure_reason is None:
            raise ValueError("a TaskRun failure_detail requires a failure_reason")

        if self.status is TaskStatus.QUEUED:
            if has_lease:
                raise ValueError("a QUEUED TaskRun holds no lease")
            if self.finished_at is not None:
                raise ValueError("a QUEUED TaskRun has not finished")
        elif self.status is TaskStatus.RUNNING:
            if not has_lease:
                raise ValueError("a RUNNING TaskRun holds a lease")
            if self.started_at is None or self.finished_at is not None:
                raise ValueError("a RUNNING TaskRun has started and has not finished")
            if has_failure:
                raise ValueError("a RUNNING TaskRun carries no failure")
            if self.attempts < 1:
                raise ValueError("a RUNNING TaskRun has spent at least one attempt")
        elif self.status is TaskStatus.SUCCEEDED:
            if has_lease:
                raise ValueError("a SUCCEEDED TaskRun holds no lease")
            if self.started_at is None or self.finished_at is None:
                raise ValueError("a SUCCEEDED TaskRun has started and finished")
            if has_failure:
                raise ValueError("a SUCCEEDED TaskRun carries no failure")
        else:  # TaskStatus.DEAD_LETTERED
            if has_lease:
                raise ValueError("a DEAD_LETTERED TaskRun holds no lease")
            if self.started_at is None or self.finished_at is None:
                raise ValueError("a DEAD_LETTERED TaskRun has started and finished")
            if not has_failure:
                raise ValueError("a DEAD_LETTERED TaskRun records the failure that ended it")
            if self.attempts < 1:
                raise ValueError("a DEAD_LETTERED TaskRun has spent at least one attempt")

        if self.available_at < self.enqueued_at:
            raise ValueError("a TaskRun available_at must not precede enqueued_at")
        if self.started_at is not None and self.started_at < self.enqueued_at:
            raise ValueError("a TaskRun started_at must not precede enqueued_at")
        if self.finished_at is not None and self.started_at is not None \
                and self.finished_at < self.started_at:
            raise ValueError("a TaskRun finished_at must not precede started_at")
        if self.updated_at < self.created_at:
            raise ValueError("a TaskRun updated_at must not precede created_at")
        return self

    def _evolve(self, **changes: Any) -> "TaskRun":
        """A new `TaskRun` with `changes` applied, re-run through the coherence validator.

        Pydantic's `model_copy(update=...)` skips validators, which would let a transition build an
        incoherent state; reconstructing through the constructor re-runs `_state_is_coherent`, so
        every transition below is checked exactly as construction is.
        """
        return TaskRun(**{**self.model_dump(), **changes})

    def should_retry(self, failure_class: TaskFailureClass) -> bool:
        """Whether a failure of this class leaves an attempt to spend (§38).

        Both conditions must hold: the class is retryable *and* a spent-attempt budget remains.
        A permanent failure is never retried; a transient one is, until `attempts` reaches
        `max_attempts`, after which the caller must dead-letter instead.
        """
        return failure_class.is_retryable and self.attempts < self.max_attempts

    def is_ready(self, as_of: datetime) -> bool:
        """Whether a QUEUED task is due to be leased at `as_of` (its backoff has elapsed)."""
        return self.status is TaskStatus.QUEUED and as_of >= self.available_at

    def is_lease_stale(self, as_of: datetime) -> bool:
        """Whether a RUNNING task's lease has lapsed at `as_of` — its worker presumed dead (§40)."""
        return (self.status is TaskStatus.RUNNING
                and self.lease_expires_at is not None
                and as_of >= self.lease_expires_at)

    def leased(self, *, worker: str, lease_expires_at: datetime,
               as_of: datetime) -> "TaskRun":
        """The RUNNING task this QUEUED one becomes when a worker claims it (§40).

        Spends an attempt, records the lease holder and its expiry, stamps `started_at` on the
        first lease (a re-lease keeps the original), and clears any failure a prior attempt left —
        a running task carries none. Only a QUEUED task may be leased.
        """
        if self.status is not TaskStatus.QUEUED:
            raise ValueError("only a QUEUED task can be leased")
        return self._evolve(
            status=TaskStatus.RUNNING,
            attempts=self.attempts + 1,
            lease_owner=worker,
            lease_expires_at=lease_expires_at,
            last_failure_class=None,
            failure_reason=None,
            failure_detail=None,
            started_at=self.started_at or as_of,
            updated_at=as_of,
        )

    def succeeded(self, *, as_of: datetime) -> "TaskRun":
        """The terminal SUCCEEDED task a RUNNING one becomes when its handler completes (§37).

        Releases the lease and stamps `finished_at`. Only a RUNNING task can succeed.
        """
        if self.status is not TaskStatus.RUNNING:
            raise ValueError("only a RUNNING task can succeed")
        return self._evolve(
            status=TaskStatus.SUCCEEDED,
            lease_owner=None,
            lease_expires_at=None,
            finished_at=as_of,
            updated_at=as_of,
        )

    def retry_scheduled(self, *, failure_class: TaskFailureClass, reason: str,
                        available_at: datetime, as_of: datetime,
                        detail: str | None = None) -> "TaskRun":
        """The QUEUED task a RUNNING one becomes when a retryable failure leaves an attempt (§38).

        Releases the lease, records the failure that caused the retry as secret-free provenance,
        and defers the next lease to `available_at` (the caller's backoff). Refuses to schedule a
        retry when the failure is permanent or no attempt remains — the caller must dead-letter
        instead — so a permanent failure can never masquerade as a pending retry.
        """
        if self.status is not TaskStatus.RUNNING:
            raise ValueError("only a RUNNING task can be rescheduled for retry")
        if not self.should_retry(failure_class):
            raise ValueError(
                "a permanent or attempt-exhausted failure must be dead-lettered, not retried")
        return self._evolve(
            status=TaskStatus.QUEUED,
            lease_owner=None,
            lease_expires_at=None,
            last_failure_class=failure_class,
            failure_reason=reason,
            failure_detail=detail,
            available_at=available_at,
            updated_at=as_of,
        )

    def dead_lettered(self, *, failure_class: TaskFailureClass, reason: str,
                      as_of: datetime, detail: str | None = None) -> "TaskRun":
        """The terminal DEAD_LETTERED task a RUNNING one becomes on a final failure (§39).

        Releases the lease, stamps `finished_at`, and records the typed terminal failure as a
        durable, secret-free trace. Both a PERMANENT failure and a TRANSIENT one with no attempt
        left land here — dead-lettering is the honest end of a job that will not be retried. Only
        a RUNNING task can be dead-lettered.
        """
        if self.status is not TaskStatus.RUNNING:
            raise ValueError("only a RUNNING task can be dead-lettered")
        return self._evolve(
            status=TaskStatus.DEAD_LETTERED,
            lease_owner=None,
            lease_expires_at=None,
            last_failure_class=failure_class,
            failure_reason=reason,
            failure_detail=detail,
            finished_at=as_of,
            updated_at=as_of,
        )

    def lease_recovered(self, *, as_of: datetime) -> "TaskRun":
        """The QUEUED task a stale-leased RUNNING one becomes when recovery reclaims it (§40).

        A worker that died mid-run left its task RUNNING with a lapsed lease. Recovery returns it
        to QUEUED, available immediately, so another worker can pick it up. The attempt it consumed
        is *not* refunded — a task that repeatedly kills its worker still exhausts its budget and
        dead-letters rather than looping forever. Only a task whose lease is actually stale at
        `as_of` may be recovered.
        """
        if not self.is_lease_stale(as_of):
            raise ValueError("only a task with a stale lease can be recovered")
        return self._evolve(
            status=TaskStatus.QUEUED,
            lease_owner=None,
            lease_expires_at=None,
            available_at=as_of,
            updated_at=as_of,
        )

class TaskSpec(DomainModel):
    """The immutable description of a job to enqueue — what the dispatcher accepts (§32).

    A caller hands the dispatcher *what* to run (`kind`), *for whom* (`user_id`, or `None` for an
    operator job like a retention sweep), the `payload` the handler needs (ids, never secrets),
    and the `idempotency_key` that makes enqueuing the same job twice a no-op. The lane and the
    run id are derived, never supplied: the lane falls out of the kind (`lane_for_kind`) so a
    browser job can never be mislabelled onto the general lane, and the run id is
    `task_run_id(idempotency_key)` so two enqueues of the same job compute the same primary key
    and collapse onto one row (§37).
    """

    kind: TaskKind
    idempotency_key: NonEmptyStr
    user_id: UserId | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(default=5, ge=1)

    @property
    def lane(self) -> TaskLane:
        """The lane this spec runs on, derived from its kind (§35)."""
        return lane_for_kind(self.kind)

    @property
    def run_id(self) -> TaskRunId:
        """The id the enqueued run will carry, derived from the idempotency key (§37)."""
        return task_run_id(self.idempotency_key)

    def to_queued_run(self, *, as_of: datetime) -> TaskRun:
        """The fresh QUEUED `TaskRun` this spec enqueues as (§32, §37).

        Available immediately (`available_at = as_of`), zero attempts spent, no lease and no
        failure — the pristine QUEUED state the validator describes. The derived id and lane make
        a re-enqueue idempotent and correctly-laned by construction.
        """
        return TaskRun(
            id=self.run_id,
            user_id=self.user_id,
            kind=self.kind,
            lane=self.lane,
            status=TaskStatus.QUEUED,
            idempotency_key=self.idempotency_key,
            payload=dict(self.payload),
            max_attempts=self.max_attempts,
            attempts=0,
            available_at=as_of,
            lease_owner=None,
            lease_expires_at=None,
            last_failure_class=None,
            failure_reason=None,
            failure_detail=None,
            enqueued_at=as_of,
            started_at=None,
            finished_at=None,
            created_at=as_of,
            updated_at=as_of,
        )







