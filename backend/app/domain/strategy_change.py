"""`StrategyChangeProposal` — the one link in the spine that may actually change strategy (§34-45).

Phase 15's spine is `observe → measure → recommend → user approves → existing service
executes`, and every earlier link is deliberately powerless: a `CareerAnalytics` report only
counts, a `CareerRecommendation` only suggests with evidence. This module is where the chain
is finally allowed to *touch* the platform — and it is built so that even here nothing changes
without an explicit human act. A proposal is a typed, bounded description of an edit to one
of exactly two targets; it carries the *before* and the proposed *after* so a reader sees the
whole diff; and it changes nothing until the user approves it, at which point the change is
re-validated against the live target and handed to the **existing** service that already owns
that edit. The proposal never writes a `SearchProfile` or an `ApplicationPolicy` itself.

Five rules make that safe, and the models enforce every one:

- **Two targets, nothing else (§35).** `StrategyChangeTarget` is closed to `SEARCH_PROFILE`
  and `APPLICATION_POLICY`. A proposal cannot reach an application, an outcome, an eligibility
  fact or a score; the career loop tunes *what the user looks for* and *how selectively they
  apply*, never the record of what happened.
- **A complete before/after, never a blind write (§37).** Each change carries both the current
  value it expects and the value it proposes, so `diff()` yields exactly the fields that move.
  A change that moves nothing fails to construct — a proposal must propose something.
- **A version precondition (§39, §64).** `target_version` is the target's `updated_at` when the
  proposal was drafted. Approval reloads the target and refuses if it has moved on
  (`STALE_STRATEGY_PROPOSAL`), so a stale proposal drafted against numbers the user has since
  changed can never silently overwrite the newer state.
- **Expiry (§38).** A proposal carries `expires_at`; an old suggestion the user never acted on
  lapses rather than lingering as a live edit against a funnel that has since changed.
- **Sensitive expansions are visible (§41-43).** `is_sensitive` is a pure function of the
  change: widening which opportunity types the platform may *apply* to, raising a submission
  cap, or lowering the score floor all read as `True`, so a surface can demand an explicit
  second confirmation for exactly the edits that loosen a safety brake. Editing a *search*
  (radius, keywords, sources, discovery types) is never sensitive — discovery scope is safe;
  application authority is what is guarded.

Pure domain values: `backend.app.domain` imports the standard library and Pydantic only
(docs/ARCHITECTURE.md §1). The service layer maps an approved proposal onto `OnboardingService`
(search edits) and a minimal `ApplicationPolicyService` (policy edits); this module knows
neither.
"""
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, TypeAdapter, model_validator

from backend.app.domain.base import DomainModel, NonEmptyStr, Score, UtcDatetime
from backend.app.domain.identifiers import (
    ApplicationPolicyId,
    CareerRecommendationId,
    LLMRunId,
    SearchProfileId,
    StrategyChangeExecutionId,
    StrategyChangeProposalId,
    UserId,
)
from backend.app.domain.opportunity import OpportunityType


class StrategyChangeTarget(StrEnum):
    """The two — and only two — kinds of thing a strategy change may edit (§35).

    A `SEARCH_PROFILE` change tunes *what the user looks for and where* and reaches the funnel
    only by changing what discovery surfaces; an `APPLICATION_POLICY` change tunes *how
    selectively and autonomously the platform applies*, which is the safety-critical axis. The
    set is closed for the same reason every vocabulary in the domain is: a target the platform
    does not offer is not a free string a generator can invent, and there is no member for an
    application, an outcome or a score — the career loop never rewrites the record it observes.
    """

    SEARCH_PROFILE = "SEARCH_PROFILE"
    APPLICATION_POLICY = "APPLICATION_POLICY"


class StrategyChangeKind(StrEnum):
    """Every typed edit a proposal may carry — the closed grammar of strategy change (§36).

    One member per operation, each mapping to exactly one method of an existing service the
    executor calls after approval, so "what can a strategy change do?" is an answerable
    question and adding a capability is adding a member plus its executor branch, never a free
    field at a call site. The search family (`SET_SEARCH_*`) is bounded discovery tuning routed
    to `OnboardingService`; the policy family (`SET_POLICY_*`, `SET_APPLICATION_VOLUME`,
    `SET_MINIMUM_SCORE`) is the selectivity/volume levers a career loop legitimately touches,
    routed to `ApplicationPolicyService`. There is deliberately no member that flips the
    automation mode or the approval brake: the career loop never proposes weakening the
    submission guard — that stays a human's manual, out-of-band choice.
    """

    SET_SEARCH_RADIUS = "SET_SEARCH_RADIUS"
    SET_SEARCH_KEYWORDS = "SET_SEARCH_KEYWORDS"
    SET_SEARCH_SOURCES = "SET_SEARCH_SOURCES"
    SET_SEARCH_OPPORTUNITY_TYPES = "SET_SEARCH_OPPORTUNITY_TYPES"
    SET_POLICY_OPPORTUNITY_TYPES = "SET_POLICY_OPPORTUNITY_TYPES"
    SET_APPLICATION_VOLUME = "SET_APPLICATION_VOLUME"
    SET_MINIMUM_SCORE = "SET_MINIMUM_SCORE"


class StrategyChangeProposalStatus(StrEnum):
    """Where one proposal is in its life from "offered" to "done" (§44).

    Born `PROPOSED`. A human confirming it that the executor applied moves it to `EXECUTED`;
    one the executor refused at re-validation — stale precondition, lost ownership — moves it
    to `REJECTED` (it was never permitted); one the executor permitted but whose service call
    raised moves it to `FAILED` (permitted, did not complete). `DISMISSED` is the user
    declining it; `EXPIRED` is the clock passing `expires_at` before anyone acted. Only a
    `PROPOSED` proposal may leave that state — every other status is terminal, so a change is
    applied at most once.
    """

    PROPOSED = "PROPOSED"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    DISMISSED = "DISMISSED"
    EXPIRED = "EXPIRED"


class StrategyChangeExecutionOutcome(StrEnum):
    """How one attempt to apply a confirmed proposal ended (§45).

    Told apart from the proposal's status because an execution is the *event* and the status is
    the proposal's resulting *state*: `REJECTED` is a proposal the executor would not permit (a
    stale version, a target that vanished), `FAILED` is one it permitted but whose service call
    raised, and `SUCCEEDED` is the change having been written to the live target.
    """

    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class FieldChange(DomainModel):
    """One field a change moves, rendered before → after for a human to read (§37).

    A read-time view, not a stored fact: the typed value the executor applies lives on the
    change member, and `diff()` composes these purely for a surface to show "this, and only
    this, will change". `before` and `after` are already-formatted strings so a tuple, an
    `int | None` cap ("unlimited") and a `Score | None` floor ("no floor") all display
    honestly; they are never equal, because a field that does not move is not a `FieldChange`.
    """

    field: NonEmptyStr
    before: NonEmptyStr
    after: NonEmptyStr

    @model_validator(mode="after")
    def _actually_moves(self) -> Self:
        if self.before == self.after:
            raise ValueError("a FieldChange must describe a field that actually moves")
        return self


def _fmt_keywords(values: tuple[str, ...]) -> str:
    """A stable, readable rendering of a keyword/source/type list ("(none)" when empty)."""
    return ", ".join(values) if values else "(none)"


def _fmt_cap(value: int | None) -> str:
    """A submission cap for display — `None` is "unlimited", a real and dangerous value."""
    return "unlimited" if value is None else str(value)


def _fmt_score(value: Score | None) -> str:
    """A score floor for display — `None` is "no floor", the least selective setting."""
    return "no floor" if value is None else f"{value:.2f}"


def _field_change(field: str, *, before: str, after: str) -> FieldChange | None:
    """A `FieldChange` for `field`, or `None` when the two renderings are identical."""
    if before == after:
        return None
    return FieldChange(field=field, before=before, after=after)


def _opportunity_types_broaden(
        before: tuple[OpportunityType, ...], after: tuple[OpportunityType, ...]) -> bool:
    """Whether an *application* allow-list got more permissive — a sensitive expansion (§42).

    Empty means "no restriction" (every type allowed), so the widest possible list is the empty
    one. Going from a restricted list to empty, or adding any type the old list did not allow,
    lets the platform apply where it previously would not and is therefore sensitive; narrowing
    or reordering is not.
    """
    before_set, after_set = frozenset(before), frozenset(after)
    if not after_set:  # after allows every type
        return bool(before_set)  # sensitive only if before actually restricted
    if not before_set:  # before allowed every type, after restricts — a narrowing
        return False
    return bool(after_set - before_set)  # a type is now allowed that was not before


def _cap_raised(before: int | None, after: int | None) -> bool:
    """Whether a submission cap loosened — raised, or lifted to unlimited (§42).

    `None` is unlimited, the most permissive setting. Moving to unlimited from any finite cap,
    or raising a finite cap, lets the platform submit more and is sensitive; tightening is not.
    """
    if after is None:
        return before is not None
    if before is None:
        return False
    return after > before


def _score_lowered(before: Score | None, after: Score | None) -> bool:
    """Whether the score floor loosened — lowered, or removed entirely (§42).

    `None` is "no floor", the least selective setting. Removing the floor, or lowering it, lets
    the platform apply to weaker matches and is sensitive; raising it is not.
    """
    if after is None:
        return before is not None
    if before is None:
        return False
    return after < before


# --- the typed change union ------------------------------------------------------------
#
# Every member carries the target's id, the value it *expects* (the `before_*` fields captured
# when the proposal was drafted) and the value it *proposes*. `diff()` renders only the fields
# that move and `is_sensitive` classifies the change; a member that moves nothing fails its
# validator, because a proposal must propose something. `target`/`target_ref` give the wrapping
# proposal a uniform way to read which resource a change acts on without unpacking the union.


class SetSearchRadiusChange(DomainModel):
    """Set the radius of a saved search's radius area(s) (routes to `OnboardingService`)."""

    kind: Literal[StrategyChangeKind.SET_SEARCH_RADIUS] = StrategyChangeKind.SET_SEARCH_RADIUS
    search_profile_id: SearchProfileId
    radius_km: Annotated[float, Field(gt=0.0, le=500.0)]
    before_radius_km: Annotated[float, Field(gt=0.0, le=500.0)] | None = None

    @property
    def target(self) -> StrategyChangeTarget:
        return StrategyChangeTarget.SEARCH_PROFILE

    @property
    def target_ref(self) -> UUID:
        return self.search_profile_id

    @property
    def is_sensitive(self) -> bool:
        """A search radius is discovery scope, never application authority — never sensitive."""
        return False

    @model_validator(mode="after")
    def _moves(self) -> Self:
        if self.before_radius_km is not None and self.before_radius_km == self.radius_km:
            raise ValueError("a SET_SEARCH_RADIUS change must alter the radius")
        return self

    def diff(self) -> tuple[FieldChange, ...]:
        before = "(unset)" if self.before_radius_km is None else f"{self.before_radius_km:g} km"
        change = _field_change("radius_km", before=before, after=f"{self.radius_km:g} km")
        return (change,) if change is not None else ()


class SetSearchKeywordsChange(DomainModel):
    """Replace a saved search's title and excluded keyword lists (routes to `OnboardingService`).

    A complete before/after of both lists: `diff()` shows only the list that moved, so a
    proposal that touches the title keywords does not read as if it also rewrote the exclusions.
    An empty tuple is a real value that clears a list — the domain's "no restriction" — never a
    missing one.
    """

    kind: Literal[StrategyChangeKind.SET_SEARCH_KEYWORDS] = (
        StrategyChangeKind.SET_SEARCH_KEYWORDS)
    search_profile_id: SearchProfileId
    title_keywords: tuple[NonEmptyStr, ...]
    before_title_keywords: tuple[NonEmptyStr, ...]
    excluded_keywords: tuple[NonEmptyStr, ...]
    before_excluded_keywords: tuple[NonEmptyStr, ...]

    @property
    def target(self) -> StrategyChangeTarget:
        return StrategyChangeTarget.SEARCH_PROFILE

    @property
    def target_ref(self) -> UUID:
        return self.search_profile_id

    @property
    def is_sensitive(self) -> bool:
        """Editing what a search matches is discovery scope — never a sensitive expansion."""
        return False

    @model_validator(mode="after")
    def _moves(self) -> Self:
        if self.title_keywords == self.before_title_keywords \
                and self.excluded_keywords == self.before_excluded_keywords:
            raise ValueError("a SET_SEARCH_KEYWORDS change must alter at least one list")
        return self

    def diff(self) -> tuple[FieldChange, ...]:
        changes = (
            _field_change("title_keywords",
                          before=_fmt_keywords(self.before_title_keywords),
                          after=_fmt_keywords(self.title_keywords)),
            _field_change("excluded_keywords",
                          before=_fmt_keywords(self.before_excluded_keywords),
                          after=_fmt_keywords(self.excluded_keywords)),
        )
        return tuple(change for change in changes if change is not None)


class SetSearchSourcesChange(DomainModel):
    """Replace a saved search's source allow-list (routes to `OnboardingService`).

    The concrete edit behind a `PRIORITIZE_SOURCE`/`DEPRIORITIZE_SOURCE` recommendation: an
    empty tuple restricts nothing (every source), a non-empty one limits discovery to the
    listed plugin keys.
    """

    kind: Literal[StrategyChangeKind.SET_SEARCH_SOURCES] = (
        StrategyChangeKind.SET_SEARCH_SOURCES)
    search_profile_id: SearchProfileId
    source_keys: tuple[NonEmptyStr, ...]
    before_source_keys: tuple[NonEmptyStr, ...]

    @property
    def target(self) -> StrategyChangeTarget:
        return StrategyChangeTarget.SEARCH_PROFILE

    @property
    def target_ref(self) -> UUID:
        return self.search_profile_id

    @property
    def is_sensitive(self) -> bool:
        """Which boards a search reads is discovery scope — never a sensitive expansion."""
        return False

    @model_validator(mode="after")
    def _moves(self) -> Self:
        if self.source_keys == self.before_source_keys:
            raise ValueError("a SET_SEARCH_SOURCES change must alter the source list")
        return self

    def diff(self) -> tuple[FieldChange, ...]:
        change = _field_change("source_keys",
                               before=_fmt_keywords(self.before_source_keys),
                               after=_fmt_keywords(self.source_keys))
        return (change,) if change is not None else ()


class SetSearchOpportunityTypesChange(DomainModel):
    """Replace a saved search's opportunity-type allow-list (routes to `OnboardingService`).

    Discovery filtering, not application authority: this bounds which types a search *surfaces*.
    Widening it broadens what the user sees, which is safe — the `APPLICATION_POLICY` allow-list
    (`SetPolicyOpportunityTypesChange`) is the one that governs what may be applied to.
    """

    kind: Literal[StrategyChangeKind.SET_SEARCH_OPPORTUNITY_TYPES] = (
        StrategyChangeKind.SET_SEARCH_OPPORTUNITY_TYPES)
    search_profile_id: SearchProfileId
    opportunity_types: tuple[OpportunityType, ...]
    before_opportunity_types: tuple[OpportunityType, ...]

    @property
    def target(self) -> StrategyChangeTarget:
        return StrategyChangeTarget.SEARCH_PROFILE

    @property
    def target_ref(self) -> UUID:
        return self.search_profile_id

    @property
    def is_sensitive(self) -> bool:
        """A discovery-type filter is search scope — never a sensitive expansion."""
        return False

    @model_validator(mode="after")
    def _moves(self) -> Self:
        if self.opportunity_types == self.before_opportunity_types:
            raise ValueError(
                "a SET_SEARCH_OPPORTUNITY_TYPES change must alter the type list")
        return self

    def diff(self) -> tuple[FieldChange, ...]:
        change = _field_change(
            "opportunity_types",
            before=_fmt_keywords(tuple(t.value for t in self.before_opportunity_types)),
            after=_fmt_keywords(tuple(t.value for t in self.opportunity_types)))
        return (change,) if change is not None else ()


class SetPolicyOpportunityTypesChange(DomainModel):
    """Replace an application policy's allowed-type list (routes to `ApplicationPolicyService`).

    This is application authority, not discovery scope: it governs which opportunity types the
    platform may actually *apply* to. Widening it — adding a type, or clearing the list to "any
    type" — lets the platform apply where it previously would not, so it reads as sensitive and
    a surface should demand an explicit second confirmation (§42).
    """

    kind: Literal[StrategyChangeKind.SET_POLICY_OPPORTUNITY_TYPES] = (
        StrategyChangeKind.SET_POLICY_OPPORTUNITY_TYPES)
    application_policy_id: ApplicationPolicyId
    allowed_opportunity_types: tuple[OpportunityType, ...]
    before_allowed_opportunity_types: tuple[OpportunityType, ...]

    @property
    def target(self) -> StrategyChangeTarget:
        return StrategyChangeTarget.APPLICATION_POLICY

    @property
    def target_ref(self) -> UUID:
        return self.application_policy_id

    @property
    def is_sensitive(self) -> bool:
        """Widening what the platform may *apply* to loosens a brake — sensitive (§42)."""
        return _opportunity_types_broaden(
            self.before_allowed_opportunity_types, self.allowed_opportunity_types)

    @model_validator(mode="after")
    def _moves(self) -> Self:
        if self.allowed_opportunity_types == self.before_allowed_opportunity_types:
            raise ValueError(
                "a SET_POLICY_OPPORTUNITY_TYPES change must alter the allowed-type list")
        return self

    def diff(self) -> tuple[FieldChange, ...]:
        change = _field_change(
            "allowed_opportunity_types",
            before=_fmt_keywords(
                tuple(t.value for t in self.before_allowed_opportunity_types)),
            after=_fmt_keywords(tuple(t.value for t in self.allowed_opportunity_types)))
        return (change,) if change is not None else ()


class SetApplicationVolumeChange(DomainModel):
    """Set an application policy's daily/weekly submission caps (`ApplicationPolicyService`).

    A complete before/after of both caps, because they carry a coherence rule the policy
    enforces — the daily cap may not exceed the weekly one — so the two move together and are
    validated together here rather than drifting apart across two proposals. `None` is a real,
    dangerous value: it means *unlimited*, which is why raising a cap or lifting one to
    unlimited reads as sensitive (§42). Tightening either cap is safe.
    """

    kind: Literal[StrategyChangeKind.SET_APPLICATION_VOLUME] = (
        StrategyChangeKind.SET_APPLICATION_VOLUME)
    application_policy_id: ApplicationPolicyId
    max_applications_per_day: Annotated[int, Field(ge=0)] | None = None
    max_applications_per_week: Annotated[int, Field(ge=0)] | None = None
    before_max_applications_per_day: Annotated[int, Field(ge=0)] | None = None
    before_max_applications_per_week: Annotated[int, Field(ge=0)] | None = None

    @property
    def target(self) -> StrategyChangeTarget:
        return StrategyChangeTarget.APPLICATION_POLICY

    @property
    def target_ref(self) -> UUID:
        return self.application_policy_id

    @property
    def is_sensitive(self) -> bool:
        """Raising or lifting a submission cap lets the platform submit more — sensitive (§42)."""
        return (_cap_raised(self.before_max_applications_per_day,
                            self.max_applications_per_day)
                or _cap_raised(self.before_max_applications_per_week,
                               self.max_applications_per_week))

    @model_validator(mode="after")
    def _moves_and_is_coherent(self) -> Self:
        if (self.max_applications_per_day == self.before_max_applications_per_day
                and self.max_applications_per_week == self.before_max_applications_per_week):
            raise ValueError("a SET_APPLICATION_VOLUME change must alter a cap")
        if self.max_applications_per_day is not None \
                and self.max_applications_per_week is not None \
                and self.max_applications_per_day > self.max_applications_per_week:
            raise ValueError(
                "max_applications_per_day must not exceed max_applications_per_week")
        return self

    def diff(self) -> tuple[FieldChange, ...]:
        changes = (
            _field_change("max_applications_per_day",
                          before=_fmt_cap(self.before_max_applications_per_day),
                          after=_fmt_cap(self.max_applications_per_day)),
            _field_change("max_applications_per_week",
                          before=_fmt_cap(self.before_max_applications_per_week),
                          after=_fmt_cap(self.max_applications_per_week)),
        )
        return tuple(change for change in changes if change is not None)


class SetMinimumScoreChange(DomainModel):
    """Set an application policy's overall score floor (routes to `ApplicationPolicyService`).

    The coarse selectivity lever: `minimum_overall_score` is the floor a match must clear to be
    applied to. `None` means *no floor*, the least selective setting — so lowering the floor, or
    removing it entirely, lets the platform apply to weaker matches and reads as sensitive (§42).
    Raising the floor is safe.
    """

    kind: Literal[StrategyChangeKind.SET_MINIMUM_SCORE] = StrategyChangeKind.SET_MINIMUM_SCORE
    application_policy_id: ApplicationPolicyId
    minimum_overall_score: Score | None = None
    before_minimum_overall_score: Score | None = None

    @property
    def target(self) -> StrategyChangeTarget:
        return StrategyChangeTarget.APPLICATION_POLICY

    @property
    def target_ref(self) -> UUID:
        return self.application_policy_id

    @property
    def is_sensitive(self) -> bool:
        """Lowering or removing the score floor admits weaker matches — sensitive (§42)."""
        return _score_lowered(
            self.before_minimum_overall_score, self.minimum_overall_score)

    @model_validator(mode="after")
    def _moves(self) -> Self:
        if self.minimum_overall_score == self.before_minimum_overall_score:
            raise ValueError("a SET_MINIMUM_SCORE change must alter the score floor")
        return self

    def diff(self) -> tuple[FieldChange, ...]:
        change = _field_change("minimum_overall_score",
                               before=_fmt_score(self.before_minimum_overall_score),
                               after=_fmt_score(self.minimum_overall_score))
        return (change,) if change is not None else ()


# The discriminated union the layer parses and persists. `kind` is the discriminator, so
# Pydantic selects exactly one member and reports a precise error for a `kind` that is not a
# member — never a silently widened match, exactly as `ChatAction` is parsed. `STRATEGY_CHANGE_
# ADAPTER` is the one reusable validator the engine, the executor and the mappers share.
StrategyChange = Annotated[
    SetSearchRadiusChange
    | SetSearchKeywordsChange
    | SetSearchSourcesChange
    | SetSearchOpportunityTypesChange
    | SetPolicyOpportunityTypesChange
    | SetApplicationVolumeChange
    | SetMinimumScoreChange,
    Field(discriminator="kind"),
]

STRATEGY_CHANGE_ADAPTER: TypeAdapter[StrategyChange] = TypeAdapter(StrategyChange)


class StrategyChangeProposal(DomainModel):
    """One approved-or-not edit to one search or policy — the spine's only mutation (§34-45).

    User-owned like every entity, read `WHERE user_id = ?`. It wraps a single typed `change`
    and adds the lifecycle a mutation needs: `target`/`target_id` name the resource (validated
    to agree with the change, so the queryable columns can never contradict the payload);
    `target_version` is the target's `updated_at` when this was drafted, the precondition
    approval revalidates against (`STALE_STRATEGY_PROPOSAL` on drift); `expires_at` lets an
    unacted suggestion lapse; `source_recommendation_id` links back to the `CareerRecommendation`
    that motivated it, so a change is traceable to the evidence that suggested it. `summary` is
    the human sentence a surface shows; `generator_key`/`llm_run_id` record whether a provider
    polished that prose behind the Phase 11 router. `status` starts `PROPOSED` and only leaves
    that state through an explicit transition, so the change is applied at most once.

    The model holds no write authority of its own: it describes a change and its precondition,
    and the executor — after a human confirms and the version still matches — hands the typed
    change to the existing service that owns the edit. `is_sensitive` surfaces from the change
    so a surface can gate a loosening edit behind a second confirmation.
    """

    id: StrategyChangeProposalId
    user_id: UserId
    target: StrategyChangeTarget
    target_id: UUID
    change: StrategyChange
    target_version: UtcDatetime
    summary: NonEmptyStr
    source_recommendation_id: CareerRecommendationId | None = None
    generator_key: NonEmptyStr | None = None
    llm_run_id: LLMRunId | None = None
    status: StrategyChangeProposalStatus = StrategyChangeProposalStatus.PROPOSED
    created_at: UtcDatetime
    updated_at: UtcDatetime
    expires_at: UtcDatetime

    @model_validator(mode="after")
    def _target_agrees_and_timestamps_are_coherent(self) -> Self:
        if self.target is not self.change.target:
            raise ValueError(
                "proposal target must match its change's target: "
                f"{self.target} != {self.change.target}")
        if self.target_id != self.change.target_ref:
            raise ValueError("proposal target_id must match the id the change acts on")
        if self.updated_at < self.created_at:
            raise ValueError(
                "StrategyChangeProposal updated_at must not precede created_at")
        if self.expires_at <= self.created_at:
            raise ValueError("StrategyChangeProposal expires_at must follow created_at")
        return self

    @property
    def is_open(self) -> bool:
        """Whether this proposal may still be confirmed, dismissed or expired."""
        return self.status is StrategyChangeProposalStatus.PROPOSED

    @property
    def is_sensitive(self) -> bool:
        """Whether the wrapped change loosens a safety brake (derived, never stored, §42)."""
        return self.change.is_sensitive

    def diff(self) -> tuple[FieldChange, ...]:
        """The before → after of exactly the fields the change moves (§37)."""
        return self.change.diff()

    def is_expired(self, *, as_of: UtcDatetime) -> bool:
        """Whether `as_of` has reached `expires_at` — the proposal has lapsed (§38)."""
        return as_of >= self.expires_at

    def matches_target_version(self, current_version: UtcDatetime) -> bool:
        """Whether the live target still carries the version this was drafted against (§39).

        The executor's precondition: a `False` here is exactly the `STALE_STRATEGY_PROPOSAL`
        case — the target moved on since drafting, so applying a stale before/after would
        clobber the newer state, and approval must refuse rather than write.
        """
        return current_version == self.target_version

    def _to_terminal(
            self, status: StrategyChangeProposalStatus, *, at: UtcDatetime,
    ) -> "StrategyChangeProposal":
        """Return a copy in a terminal `status`, refusing to move an already-closed proposal.

        The one place the "only a `PROPOSED` proposal may leave that state" rule lives, so
        `executed`/`rejected`/`failed`/`dismissed`/`expired` cannot double-apply a change or
        resurrect a dismissed one.
        """
        if not self.is_open:
            raise ValueError(
                f"a {self.status} proposal is terminal and cannot become {status}")
        return self.model_copy(update={"status": status, "updated_at": at})

    def executed(self, *, at: UtcDatetime) -> "StrategyChangeProposal":
        """Mark the change applied to the live target (§44)."""
        return self._to_terminal(StrategyChangeProposalStatus.EXECUTED, at=at)

    def rejected(self, *, at: UtcDatetime) -> "StrategyChangeProposal":
        """Mark the change refused at re-validation — never permitted (§44)."""
        return self._to_terminal(StrategyChangeProposalStatus.REJECTED, at=at)

    def failed(self, *, at: UtcDatetime) -> "StrategyChangeProposal":
        """Mark the change permitted but not completed — the service call raised (§44)."""
        return self._to_terminal(StrategyChangeProposalStatus.FAILED, at=at)

    def dismissed(self, *, at: UtcDatetime) -> "StrategyChangeProposal":
        """Mark the user having declined the proposal (§44)."""
        return self._to_terminal(StrategyChangeProposalStatus.DISMISSED, at=at)

    def expired(self, *, at: UtcDatetime) -> "StrategyChangeProposal":
        """Mark an unacted proposal lapsed past its `expires_at` (§38, §44)."""
        return self._to_terminal(StrategyChangeProposalStatus.EXPIRED, at=at)


class StrategyChangeExecution(DomainModel):
    """The record of one attempt to apply a confirmed proposal — the executor's audit (§45).

    Written once per proposal — the id is derived from the proposal, so a double-confirm
    collapses onto one row rather than applying the change twice. `outcome` says whether the
    change was refused at re-validation (`REJECTED` — a stale version, a vanished target),
    permitted but failed (`FAILED` — the service raised), or applied (`SUCCEEDED`).
    `observed_target_version` is the `updated_at` the executor read from the live target, kept
    so an audit can see the version it checked the precondition against; `result_ref` carries
    the handle the edit produced (the target's new `updated_at`, say) and `detail` is a
    secret-free human note.
    """

    id: StrategyChangeExecutionId
    proposal_id: StrategyChangeProposalId
    user_id: UserId
    outcome: StrategyChangeExecutionOutcome
    observed_target_version: UtcDatetime | None = None
    detail: str | None = None
    result_ref: str | None = None
    created_at: UtcDatetime

    @property
    def succeeded(self) -> bool:
        """Whether the change was written to the live target."""
        return self.outcome is StrategyChangeExecutionOutcome.SUCCEEDED
