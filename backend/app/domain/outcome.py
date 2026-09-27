"""`ApplicationOutcome` — what actually happened to an application in the real world.

Phase 15's foundational distinction, and the one the whole phase is built to protect
(§2, §84): an **outcome** is a fact about the *hiring process* — a recruiter screen, an
interview round, an offer, a rejection — and it is emphatically **not** the Phase 12
`ApplicationState`, which is the platform's *execution* lifecycle (did we manage to
submit the form?). A recruiter saying "no" is a `REJECTED` outcome; it must never drive
`Application.state` to `FAILED`, because a rejection is not an execution failure and
conflating the two would poison both the retry logic and the analytics. The two
lifecycles live in different modules, different tables and different enums on purpose.

Four rules shape every model here:

- **Outcomes are user-owned, timestamped facts.** Each references an `application_id`,
  carries `occurred_at` (when the event happened in the world) kept strictly apart from
  `recorded_at` (when we learned of it), and states its `source` — how we know (§7-8).
- **Ghosting is not a fact.** "No response after N days" is an *absence*, derived
  analytically from the lack of any outcome, never stored as an outcome kind (§5). There
  is no `GHOSTED` member; a `NO_RESPONSE_AFTER_THRESHOLD` observation is computed by the
  analytics layer, not recorded here.
- **Corrections never delete.** A mistaken outcome is `RETRACTED` (status flip, the row
  stays) and a corrected one is `SUPERSEDED` by a new outcome that points back at it
  (§9, §64). History is append-only, so "what did we believe, and when" stays auditable.
- **Idempotent by construction.** The id is derived from `(application_id, outcome_key)`,
  so recording the same milestone twice — a double-clicked button, a retried request —
  collapses onto one row, while two genuinely distinct interview rounds (different
  `occurred_at`) stay two rows (§46, §81).

These are pure domain values: `backend.app.domain` imports the standard library and
Pydantic and nothing else (docs/ARCHITECTURE.md §1).
"""
from enum import StrEnum
from typing import Self

from pydantic import model_validator

from backend.app.domain.base import DomainModel, NonEmptyStr, UtcDatetime
from backend.app.domain.identifiers import (
    ApplicationId,
    ApplicationOutcomeId,
    UserId,
)


class OutcomeKind(StrEnum):
    """The closed vocabulary of real-world hiring-process milestones (§3-5).

    These are the events that happen *after* the platform has done its job of applying —
    the employer's and the candidate's moves through the funnel — and they are the axis
    every Phase 15 metric is measured along. The vocabulary is deliberately closed and
    country-neutral: a board that calls a screen an "entretien téléphonique" still records
    a `SCREEN`, and a member a provider invents fails to parse rather than smuggling a new
    stage into the funnel.

    - `ACKNOWLEDGED` — the employer confirmed the application was received;
    - `SCREEN` — a recruiter or phone screen took place (repeatable);
    - `ASSESSMENT` — a take-home, online test or exercise (repeatable);
    - `INTERVIEW` — an interview round took place (repeatable — a loop is several);
    - `OFFER_RECEIVED` — an offer was extended;
    - `OFFER_ACCEPTED` — the candidate accepted the offer (the one positive terminal);
    - `OFFER_DECLINED` — the candidate declined the offer;
    - `REJECTED` — the employer declined the candidate;
    - `WITHDRAWN` — the candidate withdrew from the process.

    There is no `GHOSTED`/`NO_RESPONSE` member, and that is a decision: silence is an
    absence the analytics layer derives (§5), never a fact recorded here. `WITHDRAWN` is a
    *process* outcome and is not the Phase 12 `ApplicationState.WITHDRAWN` execution state —
    the names rhyme, the two lifecycles do not touch (§84).
    """

    ACKNOWLEDGED = "ACKNOWLEDGED"
    SCREEN = "SCREEN"
    ASSESSMENT = "ASSESSMENT"
    INTERVIEW = "INTERVIEW"
    OFFER_RECEIVED = "OFFER_RECEIVED"
    OFFER_ACCEPTED = "OFFER_ACCEPTED"
    OFFER_DECLINED = "OFFER_DECLINED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


# The milestones a single application can legitimately reach more than once (§46). A screen,
# an assessment and an interview recur — a full loop is three interviews on three days — so
# their identity must fold in *when* they happened, or the second round would collapse onto
# the first. Every other milestone is once-only: an application is acknowledged, offered,
# rejected or accepted at most once, so recording it twice is a duplicate to be collapsed.
_REPEATABLE_OUTCOME_KINDS: frozenset[OutcomeKind] = frozenset({
    OutcomeKind.SCREEN,
    OutcomeKind.ASSESSMENT,
    OutcomeKind.INTERVIEW,
})

# The milestones that close the process: nothing meaningful follows them in the funnel. An
# application that reached one of these is *mature* — it has a final answer — which is what
# the censoring logic (§15-16) needs to tell "still in flight" from "concluded". Kept here,
# beside the kinds, so the one definition of "concluded" is read by both the analytics layer
# and any caller that asks whether an application is still open.
TERMINAL_OUTCOME_KINDS: frozenset[OutcomeKind] = frozenset({
    OutcomeKind.OFFER_ACCEPTED,
    OutcomeKind.OFFER_DECLINED,
    OutcomeKind.REJECTED,
    OutcomeKind.WITHDRAWN,
})


def is_repeatable_outcome_kind(kind: OutcomeKind) -> bool:
    """Whether one application may legitimately record `kind` more than once (§46)."""
    return kind in _REPEATABLE_OUTCOME_KINDS


def build_outcome_key(*, kind: OutcomeKind, occurred_at: UtcDatetime,
                      supersedes_id: ApplicationOutcomeId | None = None) -> str:
    """The stable natural key that identifies "this fact about this application" (§46, §81).

    The whole idempotency story rests on this being a pure function of what the outcome
    *is*. Three cases, one rule — the key names the fact precisely enough that recording it
    twice collapses and recording two genuinely different facts does not:

    - a **correction** (`supersedes_id` set) keys on the outcome it replaces, so re-issuing
      the same correction after a failed flush lands on the same row, and a chain of
      corrections each supersedes a distinct predecessor and so gets a distinct id;
    - a **repeatable** milestone folds in `occurred_at`, so a second interview round on
      another day is a second row while the same round recorded twice is one;
    - a **once-only** milestone keys on the kind alone, so a re-recorded `OFFER_RECEIVED`
      collapses; genuinely changing its date is a *correction*, which takes the first path.

    `application_outcome_id` turns `(application_id, this key)` into the primary key, exactly
    as `application_id` turns an idempotency key into an application's; the value is passed to
    the id factory rather than recomputed there, keeping the key's vocabulary out of the
    UUID module.
    """
    if supersedes_id is not None:
        return f"correction:{supersedes_id}"
    if kind in _REPEATABLE_OUTCOME_KINDS:
        return f"{kind.value}:{occurred_at.isoformat()}"
    return kind.value


class OutcomeSource(StrEnum):
    """How the platform came to know an outcome — its provenance (§7-8).

    Provenance is not decoration: an outcome the user typed in and one synced from an ATS
    carry different trust, and a metric that could not say which is which could not be
    audited. The set is closed and honest — every member is a way we *actually* learned a
    fact, so there is no `INFERRED` member, because an inferred outcome is not a fact and
    does not belong in this table at all (that is what the derived `NO_RESPONSE_AFTER_
    THRESHOLD` observation is for, §5).

    - `MANUAL_USER` — the candidate recorded it themselves (the default and the common case);
    - `EMAIL` — parsed or forwarded from an email the candidate received;
    - `ATS` — synced from an applicant-tracking system or employer portal;
    - `IMPORTED` — migrated from a prior record (a V1 export, a spreadsheet).
    """

    MANUAL_USER = "MANUAL_USER"
    EMAIL = "EMAIL"
    ATS = "ATS"
    IMPORTED = "IMPORTED"


class OutcomeStatus(StrEnum):
    """Whether an outcome is currently believed, corrected, or taken back (§9, §64).

    History is append-only — nothing here is ever hard-deleted — so an outcome that turns
    out wrong changes *status* rather than vanishing, and the analytics layer counts only
    `EFFECTIVE` rows. `SUPERSEDED` marks an outcome a later, corrected one replaced (the
    replacement points back with `supersedes_id`); `RETRACTED` marks one the user took back
    without a replacement ("that rejection was for a different application"). The distinction
    matters to an audit: a superseded outcome was *wrong about the detail*, a retracted one
    *should not have been recorded at all*.
    """

    EFFECTIVE = "EFFECTIVE"
    SUPERSEDED = "SUPERSEDED"
    RETRACTED = "RETRACTED"


class ApplicationOutcome(DomainModel):
    """One recorded real-world milestone in one application's hiring process (§3-9).

    User-owned and scoped to one `application_id` — read `WHERE user_id = ?`, so one account
    can neither see nor correct another's outcomes. It records *what* happened (`kind`),
    *when it happened in the world* (`occurred_at`), *when we learned of it* (`recorded_at`,
    kept strictly apart because the gap between them is itself signal, §8), *how we know*
    (`source`) and *whether we still believe it* (`status`).

    `outcome_key` is the natural key `build_outcome_key` composes; the validator recomputes
    it and refuses a hand-built outcome whose key does not match its fields, exactly as
    `Application` guards its idempotency key — so the key `application_outcome_id` derives the
    primary key from can never drift from the fact it claims to identify. `supersedes_id`
    links a correction to the outcome it replaces; a correction must carry the `SUPERSEDED`
    intent through its key, so its id cannot collide with the original's.

    Nothing here mutates an `Application`, a `MatchProfile`, `CandidateEvidence` or any
    eligibility fact (§40-42): an outcome observes, it never rewrites the record it observes.
    """

    id: ApplicationOutcomeId
    user_id: UserId
    application_id: ApplicationId
    kind: OutcomeKind
    source: OutcomeSource = OutcomeSource.MANUAL_USER
    status: OutcomeStatus = OutcomeStatus.EFFECTIVE
    outcome_key: NonEmptyStr
    occurred_at: UtcDatetime
    recorded_at: UtcDatetime
    supersedes_id: ApplicationOutcomeId | None = None
    detail: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _key_matches_the_fact(self) -> Self:
        expected = build_outcome_key(
            kind=self.kind,
            occurred_at=self.occurred_at,
            supersedes_id=self.supersedes_id)
        if self.outcome_key != expected:
            raise ValueError(
                "outcome_key does not match the outcome's kind, occurred_at and "
                "supersedes_id; it must be build_outcome_key(...) for this fact")
        if self.supersedes_id is not None and self.supersedes_id == self.id:
            raise ValueError("an outcome cannot supersede itself")
        return self

    @property
    def is_effective(self) -> bool:
        """Whether the analytics layer should count this outcome — only `EFFECTIVE` rows."""
        return self.status is OutcomeStatus.EFFECTIVE

    @property
    def is_terminal(self) -> bool:
        """Whether this milestone concludes the process (§15-16 maturity)."""
        return self.kind in TERMINAL_OUTCOME_KINDS

    @property
    def is_correction(self) -> bool:
        """Whether this outcome was recorded to correct an earlier one (§9)."""
        return self.supersedes_id is not None

    def retracted(self, *, at: UtcDatetime) -> "ApplicationOutcome":
        """Return a copy marked `RETRACTED` — taken back without a replacement (§64).

        A status flip, never a delete: the row stays so "we once believed this, then took
        it back at `at`" remains auditable. `recorded_at` advances to the retraction instant,
        because the retraction is itself the latest thing we know about this fact.
        """
        return self.model_copy(
            update={"status": OutcomeStatus.RETRACTED, "recorded_at": at})

    def superseded(self, *, at: UtcDatetime) -> "ApplicationOutcome":
        """Return a copy marked `SUPERSEDED` — replaced by a later correction (§9).

        Called on the *old* outcome when a corrected one takes its place; the correction
        carries `supersedes_id=self.id`. As with retraction the row survives, so the
        correction history is a chain a reader can walk rather than a value overwritten.
        """
        return self.model_copy(
            update={"status": OutcomeStatus.SUPERSEDED, "recorded_at": at})
