"""`UsageEvent` and `UsagePeriod` — the append-only, idempotent record of what was consumed (§5-9).

This is the *authoritative usage event* end of the spine `commercial entitlement → server-side
quota check → existing domain service → authoritative usage event`. Every metered capability,
after it runs, writes one `UsageEvent`: a fact that this account consumed this much of this
entitlement at this instant, in this billing period. The ledger is the ground truth a per-period
quota check sums against — never the frontend's tally, never a provider's — so an account's
remaining allowance is always derived from what the server actually recorded.

Four rules make the ledger trustworthy, and the models enforce every one:

- **Append-only and idempotent (§7).** An event is never mutated or deleted; a correction is a
  new compensating event, not an edit. The id is derived from a deterministic
  `idempotency_key`, so metering the same source twice — a retried handler, a redelivered task —
  collapses onto one row rather than charging the account twice for one action (§9).
- **The unknown is never fabricated (§5).** When a capability's consumption cannot be measured —
  a provider CLI that reports no token usage — the metering layer writes *no* event rather than
  an event of `0`. A `UsageEvent` therefore always represents a real, positive, measured
  consumption; absence of an event is honest ignorance, not a counted zero. Reusing the Phase 11
  `LLMRun` telemetry (whose token fields are `None` when unknown) is exactly how LLM_TOKENS
  metering honours this — there is one token counter, not two.
- **Every event names its source and its period.** `source_type`/`source_id` point back at the
  domain entity that produced the usage (the `LLMRun`, the document version, the application),
  so a charge is always auditable to the act that caused it; `billing_period` labels the window
  it counts in, so summing a period is a keyed read rather than a date-range guess.
- **A period is a self-describing window.** `UsagePeriod` carries its own bounds and a stable
  `label`, so a usage sum always says which span it measured — a subscription's paid window, or
  the calendar month a free account falls back to.

Pure domain values: `backend.app.domain` imports the standard library and Pydantic only
(docs/ARCHITECTURE.md §1). This module depends on `entitlement` for the capability vocabulary it
meters and on nothing heavier.
"""
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.base import DomainModel, NonEmptyStr, UtcDatetime
from backend.app.domain.entitlement import EntitlementKey
from backend.app.domain.identifiers import UsageEventId, UserId


class UsageSourceType(StrEnum):
    """The kind of domain entity that produced a usage event — its provenance (§7).

    Closed to the acts the platform actually meters, so a charge is always attributable to a
    real entity a reader can go and inspect. A member a caller invents fails to parse rather
    than recording usage against an untraceable source, exactly as every closed vocabulary in
    the domain.

    - `LLM_RUN` — an `LLMRun` telemetry row (meters `LLM_TOKENS` from its measured token count);
    - `DOCUMENT_VERSION` — a generated document version (meters `DOCUMENT_GENERATIONS`);
    - `APPLICATION_SUBMISSION` — a real application submission (meters `APPLICATION_SUBMISSIONS`);
    - `INTERVIEW_SESSION` — an interview-practice session (meters `INTERVIEW_SESSIONS`);
    - `CAREER_RECOMMENDATION` — a recommendation-generation run (meters the generations key).
    """

    LLM_RUN = "LLM_RUN"
    DOCUMENT_VERSION = "DOCUMENT_VERSION"
    APPLICATION_SUBMISSION = "APPLICATION_SUBMISSION"
    INTERVIEW_SESSION = "INTERVIEW_SESSION"
    CAREER_RECOMMENDATION = "CAREER_RECOMMENDATION"


def build_usage_idempotency_key(
        *, entitlement_key: EntitlementKey, source_type: UsageSourceType,
        source_id: str) -> str:
    """The stable natural key that identifies "this consumption of this entitlement" (§9).

    The whole idempotent-metering story rests on this being a pure function of *what the usage
    is*: the entitlement it charges, the kind of source, and that source's own id. Metering the
    same `LLMRun` twice — a retried task, a redelivered handler — composes the same key, and
    `usage_event_id` turns it into the same primary key, so the second write collapses onto the
    first row rather than double-charging (§7, §9). Two genuinely different sources (two
    `LLMRun`s, two document versions) compose different keys and so are two events.

    The entitlement key is folded in so that a single source metering two distinct entitlements
    (were that ever to arise) stays two events; in today's mapping each source type feeds exactly
    one entitlement, so the source id alone would suffice — including the key is cheap insurance
    against a future many-to-one that would otherwise silently collide.
    """
    return f"{entitlement_key.value}:{source_type.value}:{source_id}"


def calendar_month_period(as_of: datetime) -> "UsagePeriod":
    """The calendar-month usage window an unsubscribed account meters against (§6).

    The free tier has no provider billing window, so its per-period meters reset on the calendar
    month: this returns the `[first of the month, first of next month)` span containing `as_of`,
    labelled `YYYY-MM`. A paid subscription instead meters against its own
    `current_period_start`/`current_period_end`; this is only the fallback, kept here so the one
    definition of "the free tier's month" is shared by the metering service and its tests.
    """
    anchor = as_of.astimezone(UTC)
    start = anchor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return UsagePeriod(start=start, end=end, label=f"{start.year:04d}-{start.month:02d}")


class UsagePeriod(DomainModel):
    """A self-describing billing window a usage sum is measured over (§6, §14).

    Carries its own `[start, end)` bounds and a stable `label`, so a usage total always says
    which span it counted — a subscription's paid period, or the calendar month a free account
    falls back to. The label is what a `UsageEvent.billing_period` stores and what a period sum
    keys on, so folding usage into a window is a keyed read rather than a fragile date-range
    scan. Half-open by convention: an event at exactly `end` belongs to the *next* period, which
    `contains` encodes so a boundary instant is counted once, never twice.
    """

    start: UtcDatetime
    end: UtcDatetime
    label: NonEmptyStr

    @model_validator(mode="after")
    def _window_runs_forward(self) -> Self:
        if self.end <= self.start:
            raise ValueError("a UsagePeriod end must follow its start")
        return self

    def contains(self, moment: datetime) -> bool:
        """Whether `moment` falls in this half-open window `[start, end)` (the boundary is next)."""
        return self.start <= moment.astimezone(UTC) < self.end


class UsageEvent(DomainModel):
    """One append-only, idempotent fact of measured consumption (§5-9).

    User-owned like every entity, read `WHERE user_id = ?`. It records which `entitlement_key`
    was consumed, the positive `quantity` (a token count, or `1` for a discrete act), the
    `source_type`/`source_id` that produced it, when it `occurred_at`, the `billing_period` label
    it counts in, and the `idempotency_key` whose derivation makes re-metering a no-op. The id is
    derived from that key (`usage_event_id`), so the primary key itself enforces "one event per
    consumption" — the second attempt collides rather than duplicating.

    `quantity` is strictly positive on purpose: an event exists only for consumption that was
    actually measured, so an unmeasurable call (unknown provider tokens) writes no event at all
    rather than a fabricated `0` (§5). The validator recomputes the idempotency key from the
    event's own fields and refuses one that does not match, exactly as `ApplicationOutcome` guards
    its outcome key — so the key the primary key derives from can never drift from the fact it
    claims to identify.
    """

    id: UsageEventId
    user_id: UserId
    entitlement_key: EntitlementKey
    quantity: Annotated[int, Field(ge=1)]
    source_type: UsageSourceType
    source_id: NonEmptyStr
    occurred_at: UtcDatetime
    billing_period: NonEmptyStr
    idempotency_key: NonEmptyStr
    detail: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _key_matches_the_fact(self) -> Self:
        expected = build_usage_idempotency_key(
            entitlement_key=self.entitlement_key,
            source_type=self.source_type,
            source_id=self.source_id)
        if self.idempotency_key != expected:
            raise ValueError(
                "idempotency_key does not match the event's entitlement_key, source_type and "
                "source_id; it must be build_usage_idempotency_key(...) for this consumption")
        return self
