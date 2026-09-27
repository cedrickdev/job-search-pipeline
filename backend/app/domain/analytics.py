"""`CareerAnalytics` — the deterministic, self-describing measurement of a funnel (§13-25).

Phase 15 turns a pile of `ApplicationOutcome` rows into numbers a candidate can trust:
how far applications get, how often they convert, how long each step takes, and how those
differ by role, source, opportunity type and document strategy. The whole module obeys
four rules the spec makes acceptance-critical, and every model here is shaped to make a
violation impossible rather than merely discouraged:

- **Count applications, not events (§13, §70).** The funnel measures *how many
  applications reached a stage*, deduplicated — three recorded interview rounds on one
  application advance it to `INTERVIEW` exactly once. The stage an application reaches is
  the furthest of its effective outcomes, computed by the pure `furthest_funnel_stage`
  rule so the SQL and any Python check agree to the row.
- **Every metric is self-describing (§14).** A rate carries its `numerator`, `denominator`,
  `sample_size` and `window`; a timing carries its `sample_size` and `window`. A number
  lifted out of a report still says what it was measured over, so no surface can quote
  "80%" without the "of 5" beside it.
- **Silence is censored, never counted as failure (§15-16, §71).** An application submitted
  yesterday has not been *given time* to answer; folding it into a response-rate denominator
  would make a fresh batch look like a wall of rejections. Maturity — concluded, or observed
  at least `DEFAULT_OBSERVATION_HORIZON_DAYS` — is decided by one centralized rule
  (`is_application_mature`) and the split is reported (`MaturityCensoring`), never hidden.
- **Deterministic, versioned, no LLM (§24-25, §47-48).** These are result values the SQL
  aggregation fills and Pydantic re-validates on read; no provider is anywhere near the
  math. `CAREER_ANALYTICS_VERSION` is stamped on every report so a number computed under
  one recipe is never silently compared with a newer one, exactly as `MatchProfile.version`
  guards match scores.

Pure domain values: `backend.app.domain` imports the standard library and Pydantic only
(docs/ARCHITECTURE.md §1). This module depends on `outcome` for the milestone vocabulary
it maps into stages, and on nothing heavier.
"""
from collections.abc import Iterable
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.base import DomainModel, NonEmptyStr, UtcDatetime
from backend.app.domain.identifiers import UserId
from backend.app.domain.outcome import OutcomeKind

# The version stamped on every report. Bump it when the funnel vocabulary, a rate's
# definition, the censoring rule or the timing recipe changes, so a metric computed under
# one recipe is never silently compared with a newer one (§24-25). A pure string, not an
# enum: it is provenance written onto results, matched exactly, never dispatched on.
CAREER_ANALYTICS_VERSION = "career-analytics/1.0"

# How long an application must be observed before its silence *means* something (§15-16).
# Below this, "no response yet" is too early to tell — the application is censored out of
# rate denominators rather than counted as a failure. Thirty days is the spec default and
# lives here, once, so the funnel, the rates and the recommendation engine share one horizon
# rather than each inventing its own. Callers may override per report; the default is what
# makes two reports comparable.
DEFAULT_OBSERVATION_HORIZON_DAYS = 30


class FunnelStage(StrEnum):
    """The ordered milestones an application passes through, coarsened for counting (§13, §22).

    A *cumulative* ladder, not the raw outcome vocabulary: an application is counted at a
    stage when its furthest effective outcome reaches that stage or beyond, so reaching
    `OFFER` implies it was counted at every shallower stage too. `SUBMITTED` is the base —
    every application in the funnel has been submitted — and the stages climb from there.
    The negative terminals (`REJECTED`, `WITHDRAWN`, a declined offer) are deliberately *not*
    stages: they can strike at any depth and say nothing about how far the application got,
    so they live in the maturity/censoring axis, not the progress axis. Order is authority
    here, so it is pinned in `_STAGE_ORDER`, never inferred from the string values.
    """

    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    SCREEN = "SCREEN"
    ASSESSMENT = "ASSESSMENT"
    INTERVIEW = "INTERVIEW"
    OFFER = "OFFER"
    ACCEPTED = "ACCEPTED"


# The funnel ladder, shallow to deep. `_STAGE_RANK` turns it into a comparable index so
# "furthest reached" is a max over ranks; nothing else should hard-code the order, so a
# stage inserted here works everywhere at once.
_STAGE_ORDER: tuple[FunnelStage, ...] = (
    FunnelStage.SUBMITTED,
    FunnelStage.ACKNOWLEDGED,
    FunnelStage.SCREEN,
    FunnelStage.ASSESSMENT,
    FunnelStage.INTERVIEW,
    FunnelStage.OFFER,
    FunnelStage.ACCEPTED,
)
_STAGE_RANK: dict[FunnelStage, int] = {
    stage: rank for rank, stage in enumerate(_STAGE_ORDER)}


# Which funnel stage a recorded outcome advances an application to, or `None` for an outcome
# that concludes without advancing progress. `OFFER_DECLINED` maps to `OFFER`: the offer was
# *reached* even though the candidate said no. `REJECTED` and `WITHDRAWN` map to nothing —
# they are terminal facts on the orthogonal maturity axis, never a rung on the progress
# ladder (§13). A repeatable milestone (a second interview round) maps to the same stage as
# the first, which is exactly why the funnel counts applications and not events.
_OUTCOME_KIND_STAGE: dict[OutcomeKind, FunnelStage] = {
    OutcomeKind.ACKNOWLEDGED: FunnelStage.ACKNOWLEDGED,
    OutcomeKind.SCREEN: FunnelStage.SCREEN,
    OutcomeKind.ASSESSMENT: FunnelStage.ASSESSMENT,
    OutcomeKind.INTERVIEW: FunnelStage.INTERVIEW,
    OutcomeKind.OFFER_RECEIVED: FunnelStage.OFFER,
    OutcomeKind.OFFER_DECLINED: FunnelStage.OFFER,
    OutcomeKind.OFFER_ACCEPTED: FunnelStage.ACCEPTED,
}


def stage_rank(stage: FunnelStage) -> int:
    """The depth of a stage on the funnel ladder — `SUBMITTED` is 0, `ACCEPTED` the deepest."""
    return _STAGE_RANK[stage]


def outcome_stage(kind: OutcomeKind) -> FunnelStage | None:
    """The funnel stage `kind` advances an application to, or `None` if it does not advance it.

    `REJECTED` and `WITHDRAWN` return `None`: they conclude an application without placing it
    on a progress rung. Every other kind maps to the stage it reaches (§13, §22).
    """
    return _OUTCOME_KIND_STAGE.get(kind)


def furthest_funnel_stage(kinds: Iterable[OutcomeKind]) -> FunnelStage:
    """The deepest funnel stage a set of an application's outcome kinds reaches (§13).

    Pure and deterministic — the Python twin of the SQL the analytics layer runs, kept here
    so the count is reproducible and testable. Starts at `SUBMITTED` (every funnel
    application has been submitted) and climbs to the deepest stage any kind maps to; kinds
    that map to nothing (`REJECTED`, `WITHDRAWN`) leave the reached stage untouched, because
    a rejection does not un-reach the interview that preceded it. Feeding it the *same*
    milestone twice yields the same stage, which is what makes the funnel count applications
    rather than events.
    """
    reached = FunnelStage.SUBMITTED
    for kind in kinds:
        stage = _OUTCOME_KIND_STAGE.get(kind)
        if stage is not None and _STAGE_RANK[stage] > _STAGE_RANK[reached]:
            reached = stage
    return reached


def is_application_mature(
    *,
    reference: UtcDatetime,
    as_of: UtcDatetime,
    has_terminal_outcome: bool,
    horizon_days: int = DEFAULT_OBSERVATION_HORIZON_DAYS,
) -> bool:
    """Whether an application has been observed long enough for its silence to mean something.

    The one centralized censoring rule (§15-16, §71): an application is *mature* — safe to
    put in a rate denominator — when it has either concluded (a terminal outcome: a
    rejection, a withdrawal, an accepted or declined offer) or been observed at least
    `horizon_days` since `reference` (its applied-at instant, or the start of whichever step
    a rate measures). Everything else is *censored*: too fresh to distinguish "no answer yet"
    from "no answer coming", so it is excluded from the denominator, not counted as a
    failure. Defined here rather than in the SQL so the funnel, the conversion rates and the
    recommendation engine all ask the same question the same way.
    """
    if has_terminal_outcome:
        return True
    return (as_of - reference) >= timedelta(days=horizon_days)


class RateKind(StrEnum):
    """The named conversion rates the funnel reports — the closed set from §23.

    Each is a ratio of applications that reached one stage to those that reached an earlier
    base stage, and the base is what makes them honest: `OFFER_CONVERSION` is offers per
    *interview*, not per application, so a strong closer with few interviews is not punished
    for a thin top of funnel. `RESPONSE` counts any employer signal (reaching `ACKNOWLEDGED`
    or beyond) against mature submitted applications — its complement is the ghost rate the
    platform never stores as a fact (§5). The base and target stages live in
    `_RATE_STAGES`, so the definition of each rate is one table a reader can audit.
    """

    RESPONSE = "RESPONSE"
    INTERVIEW_CONVERSION = "INTERVIEW_CONVERSION"
    OFFER_CONVERSION = "OFFER_CONVERSION"
    ACCEPTANCE = "ACCEPTANCE"


# Each named rate as (base stage, target stage): the denominator counts applications whose
# furthest stage reached the base, the numerator those that also reached the target. The
# base is never assumed to be `SUBMITTED` — `OFFER_CONVERSION` and `ACCEPTANCE` measure a
# later transition — which is the whole point of reporting them separately (§23).
_RATE_STAGES: dict[RateKind, tuple[FunnelStage, FunnelStage]] = {
    RateKind.RESPONSE: (FunnelStage.SUBMITTED, FunnelStage.ACKNOWLEDGED),
    RateKind.INTERVIEW_CONVERSION: (FunnelStage.SUBMITTED, FunnelStage.INTERVIEW),
    RateKind.OFFER_CONVERSION: (FunnelStage.INTERVIEW, FunnelStage.OFFER),
    RateKind.ACCEPTANCE: (FunnelStage.OFFER, FunnelStage.ACCEPTED),
}


def rate_stages(kind: RateKind) -> tuple[FunnelStage, FunnelStage]:
    """The `(base, target)` stages that define a named rate — its denominator and numerator."""
    return _RATE_STAGES[kind]


class TimingKind(StrEnum):
    """The elapsed-time measurements the funnel reports, in real calendar days (§17).

    Each is the gap between two real timestamps — never a status age, never an estimate —
    so a duration exists only when both ends actually happened. `TIME_TO_FIRST_RESPONSE` is
    from submission to the first employer signal; `TIME_TO_INTERVIEW` to the first interview;
    `TIME_TO_OFFER` to the offer; `TIME_TO_DECISION` from submission to whichever terminal
    outcome concluded the process. They are summarized by median with a p25/p75 spread rather
    than a mean, because a handful of slow replies should not drag the typical wait, and the
    spread is where the honesty about variance lives.
    """

    TIME_TO_FIRST_RESPONSE = "TIME_TO_FIRST_RESPONSE"
    TIME_TO_INTERVIEW = "TIME_TO_INTERVIEW"
    TIME_TO_OFFER = "TIME_TO_OFFER"
    TIME_TO_DECISION = "TIME_TO_DECISION"


class DimensionKind(StrEnum):
    """The axes the funnel can be sliced along — the closed set of breakdown dimensions (§18-21).

    Each answers "does my funnel differ *by* …": `ROLE_FAMILY` (§18, the deterministic
    `RoleFamily` a posting title classifies to), `SOURCE` (§20, which board or channel the
    opportunity came from), `OPPORTUNITY_TYPE` (§19, student job vs internship vs permanent),
    and `DOCUMENT_STRATEGY` (§21, the pinned document version's provenance — its generator,
    prompt version and language, grouped by exact version, never by parsing prose). A cell
    whose dimension value is unknown keeps a `None` key rather than a catch-all bucket, the
    same discipline `RoleFamily` keeps: an honest gap beats a lie that averages everything
    that failed to classify.
    """

    ROLE_FAMILY = "ROLE_FAMILY"
    SOURCE = "SOURCE"
    OPPORTUNITY_TYPE = "OPPORTUNITY_TYPE"
    DOCUMENT_STRATEGY = "DOCUMENT_STRATEGY"


class ObservationWindow(DomainModel):
    """The span of applied-at instants a report's applications fall in — its self-description (§14).

    Bounds by the earliest and latest submission the report counted, so a metric extracted
    from the report still says which slice of history it measured. Empty — both bounds
    `None` — when the report counted no applications at all, which is a real state (a brand
    new account) rather than an error; a window with one bound set and the other not would be
    a lie about coverage, so the invariant forbids it.
    """

    earliest_applied_at: UtcDatetime | None = None
    latest_applied_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _bounds_are_coherent(self) -> Self:
        earliest, latest = self.earliest_applied_at, self.latest_applied_at
        if (earliest is None) != (latest is None):
            raise ValueError(
                "an ObservationWindow must set both bounds or neither (an empty window)")
        if earliest is not None and latest is not None and latest < earliest:
            raise ValueError("ObservationWindow latest_applied_at must not precede earliest")
        return self

    @property
    def is_empty(self) -> bool:
        """Whether the report counted no applications — both bounds are absent."""
        return self.earliest_applied_at is None


class MaturityCensoring(DomainModel):
    """How the submitted population split into mature and censored, under which horizon (§15-16).

    Reported beside the funnel so a reader can see how much of the denominator was withheld
    as too-fresh-to-tell: `mature_count` applications were old enough (or concluded) to
    count, `censored_count` were not, and `observation_horizon_days` is the rule that drew
    the line as of `as_of`. Making the split visible is the point — a response rate over 12
    mature of 40 submitted is a different claim than one over 40, and hiding the 28 censored
    would be exactly the dishonesty §15 forbids.
    """

    observation_horizon_days: Annotated[int, Field(ge=1)] = DEFAULT_OBSERVATION_HORIZON_DAYS
    as_of: UtcDatetime
    mature_count: Annotated[int, Field(ge=0)]
    censored_count: Annotated[int, Field(ge=0)]

    @property
    def total_count(self) -> int:
        """Every submitted application considered — mature plus censored."""
        return self.mature_count + self.censored_count


class FunnelStageCount(DomainModel):
    """How many applications reached one funnel stage — cumulative, deduplicated (§13).

    `applications` counts *distinct* applications whose furthest effective outcome reached
    this stage or beyond, so it is a whole-application count and never an event count: an
    application with three interview rounds contributes one to `INTERVIEW`. Because the count
    is cumulative, it is monotonically non-increasing down the ladder, an invariant
    `CareerFunnel` enforces across the whole tuple.
    """

    stage: FunnelStage
    applications: Annotated[int, Field(ge=0)]


class CareerFunnel(DomainModel):
    """The whole cumulative funnel — one count per stage, plus how it was censored (§13-16).

    Carries the full ordered ladder (`SUBMITTED` through `ACCEPTED`), its `window`, and the
    `censoring` split of the submitted base, so the funnel is a complete, self-describing
    metric. Two invariants keep it honest: the stages must be exactly `_STAGE_ORDER` (a
    partial funnel would let a reader mistake a missing stage for a zero), and the counts
    must not rise as the ladder deepens (reaching `OFFER` implies reaching `INTERVIEW`, so
    `count(OFFER) <= count(INTERVIEW)`). The `SUBMITTED` count is the top of the funnel and
    the denominator base for the submission-anchored rates.
    """

    stages: tuple[FunnelStageCount, ...]
    window: ObservationWindow
    censoring: MaturityCensoring

    @model_validator(mode="after")
    def _stages_are_the_full_ordered_ladder(self) -> Self:
        if tuple(entry.stage for entry in self.stages) != _STAGE_ORDER:
            raise ValueError(
                "a CareerFunnel must list every FunnelStage exactly once, in ladder order")
        counts = [entry.applications for entry in self.stages]
        if any(deeper > shallower
               for shallower, deeper in zip(counts, counts[1:], strict=False)):
            raise ValueError(
                "funnel counts must not increase down the ladder: reaching a deeper stage "
                "implies reaching every shallower one")
        return self

    def count_at(self, stage: FunnelStage) -> int:
        """How many applications reached `stage` or beyond."""
        for entry in self.stages:
            if entry.stage is stage:
                return entry.applications
        raise KeyError(stage)  # pragma: no cover - stages validated complete above

    @property
    def submitted(self) -> int:
        """The top of the funnel — every application counted."""
        return self.count_at(FunnelStage.SUBMITTED)


class ConversionRate(DomainModel):
    """One named rate — successes over a base, carrying everything needed to trust it (§14, §23).

    The self-describing-metric rule made concrete: `numerator` applications reached the
    target stage out of `denominator` that reached the base (and were mature — a censored
    application is in neither count), measured over `window`. `rate` is derived, never
    stored, and is `None` when the denominator is zero, because "no interviews yet, so no
    offer rate" is an honest absence, not 0%. `sample_size` exposes the denominator under the
    name every `CareerMetric` answers reliability with, so the recommendation engine's
    minimum-sample check reads one field across funnel rates, timings and breakdowns alike.
    """

    kind: RateKind
    numerator: Annotated[int, Field(ge=0)]
    denominator: Annotated[int, Field(ge=0)]
    window: ObservationWindow

    @model_validator(mode="after")
    def _numerator_within_denominator(self) -> Self:
        if self.numerator > self.denominator:
            raise ValueError(
                "a ConversionRate numerator cannot exceed its denominator "
                "(reaching the target implies reaching the base)")
        return self

    @property
    def rate(self) -> float | None:
        """Successes over base on the unit interval, or `None` when the base is empty."""
        if self.denominator == 0:
            return None
        return self.numerator / self.denominator

    @property
    def sample_size(self) -> int:
        """The number of trials backing the rate — its denominator, named for reliability."""
        return self.denominator

    def rate_percent(self) -> int | None:
        """`rate` on the 0-100 presentation scale, or `None` when undefined."""
        value = self.rate
        if value is None:
            return None
        return int(value * 100 + 0.5)


class TimingStat(DomainModel):
    """A median-and-spread summary of one elapsed-time measurement, in real days (§17).

    `median_days` with `p25_days`/`p75_days` describes the typical wait and its spread from
    `sample_size` real durations, over `window`. All three are `None` together exactly when
    `sample_size` is zero — no completed transitions, so nothing to summarize, reported as
    absence rather than a fabricated zero. When present the quartiles are ordered
    (`p25 <= median <= p75`), the invariant that keeps a spread from lying about itself. Days
    are non-negative floats: a fractional day is a real answer (an assessment returned the
    same afternoon), and a negative one would mean an effect preceded its cause.
    """

    kind: TimingKind
    sample_size: Annotated[int, Field(ge=0)]
    median_days: Annotated[float, Field(ge=0.0)] | None = None
    p25_days: Annotated[float, Field(ge=0.0)] | None = None
    p75_days: Annotated[float, Field(ge=0.0)] | None = None
    window: ObservationWindow

    @model_validator(mode="after")
    def _quartiles_present_iff_sampled_and_ordered(self) -> Self:
        present = (self.median_days, self.p25_days, self.p75_days)
        if self.sample_size == 0:
            if any(value is not None for value in present):
                raise ValueError("a TimingStat with no sample carries no quartiles")
            return self
        if any(value is None for value in present):
            raise ValueError(
                "a TimingStat with a sample must carry median, p25 and p75 days")
        assert self.p25_days is not None and self.median_days is not None \
            and self.p75_days is not None
        if not (self.p25_days <= self.median_days <= self.p75_days):
            raise ValueError("TimingStat quartiles must be ordered: p25 <= median <= p75")
        return self


class DimensionCell(DomainModel):
    """One slice of the funnel along a dimension — one role family, one source, one type (§18-21).

    `key` is the dimension's value: a `RoleFamily` name, a source key, an `OpportunityType`
    value, or a document-strategy signature — or `None`, the honest "unclassified/unknown"
    bucket that a value the platform could not place falls into, never folded into a
    catch-all. `applications` is how many submitted applications this slice holds, and `rates`
    are the same named conversions computed within it, so a reader can ask "do my data
    applications convert better than my engineering ones" and get numerators, denominators
    and sample sizes per slice rather than one blended figure. A slice reports only the rates
    it has data for; a rate absent from `rates` is a slice too thin to compute it.
    """

    dimension: DimensionKind
    key: NonEmptyStr | None
    applications: Annotated[int, Field(ge=0)]
    rates: tuple[ConversionRate, ...] = ()

    @model_validator(mode="after")
    def _rates_do_not_repeat(self) -> Self:
        kinds = [rate.kind for rate in self.rates]
        if len(kinds) != len(set(kinds)):
            raise ValueError("a DimensionCell must not carry the same RateKind twice")
        return self

    def rate_for(self, kind: RateKind) -> ConversionRate | None:
        """This slice's rate of one kind, or `None` when the slice was too thin to compute it."""
        for rate in self.rates:
            if rate.kind is kind:
                return rate
        return None


class DimensionBreakdown(DomainModel):
    """The funnel sliced along one dimension — every cell, sharing one window (§18-21).

    Groups the `cells` for a single `DimensionKind`; the invariants keep the grouping honest:
    every cell must carry this breakdown's dimension, and no dimension value may appear
    twice, so a role family is one row with one set of rates rather than two that disagree.
    The `None`-keyed cell, when present, is the unclassified slice — reported as its own
    honest row, exactly as `RoleFamily` leaves an unrecognized title unclassified.
    """

    dimension: DimensionKind
    cells: tuple[DimensionCell, ...]
    window: ObservationWindow

    @model_validator(mode="after")
    def _cells_match_dimension_and_are_distinct(self) -> Self:
        if any(cell.dimension is not self.dimension for cell in self.cells):
            raise ValueError(
                "every DimensionCell must carry its DimensionBreakdown's dimension")
        keys = [cell.key for cell in self.cells]
        if len(keys) != len(set(keys)):
            raise ValueError("a DimensionBreakdown must not list the same key twice")
        return self

    def cell_for(self, key: str | None) -> DimensionCell | None:
        """The slice for one dimension value, or `None` if the report holds no such slice."""
        for cell in self.cells:
            if cell.key == key:
                return cell
        return None


class CareerAnalytics(DomainModel):
    """One user's whole funnel report — the typed `CareerMetric` set the engine consumes (§24-33).

    The bundle the analytics service returns and the recommendation engine reads instead of
    raw rows: a `funnel`, the named `rates`, the `timings`, and the `breakdowns`, all for one
    `user_id`, all measured over one `window`, all stamped with the `analytics_version` that
    computed them. The recommendation engine takes *this* — never a database cursor — so its
    inputs are typed, versioned and reproducible, and a metric it cites can be traced back to
    the exact recipe that produced it. Nothing here is authored by a provider; it is the
    deterministic output of the SQL aggregation, re-validated on the way in.
    """

    user_id: UserId
    analytics_version: NonEmptyStr = CAREER_ANALYTICS_VERSION
    window: ObservationWindow
    funnel: CareerFunnel
    rates: tuple[ConversionRate, ...] = ()
    timings: tuple[TimingStat, ...] = ()
    breakdowns: tuple[DimensionBreakdown, ...] = ()
    computed_at: UtcDatetime

    @model_validator(mode="after")
    def _metrics_do_not_repeat(self) -> Self:
        rate_kinds = [rate.kind for rate in self.rates]
        if len(rate_kinds) != len(set(rate_kinds)):
            raise ValueError("CareerAnalytics must not carry the same RateKind twice")
        timing_kinds = [timing.kind for timing in self.timings]
        if len(timing_kinds) != len(set(timing_kinds)):
            raise ValueError("CareerAnalytics must not carry the same TimingKind twice")
        dimensions = [breakdown.dimension for breakdown in self.breakdowns]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("CareerAnalytics must not carry the same DimensionKind twice")
        return self

    def rate_for(self, kind: RateKind) -> ConversionRate | None:
        """The overall rate of one kind, or `None` if the report did not compute it."""
        for rate in self.rates:
            if rate.kind is kind:
                return rate
        return None

    def timing_for(self, kind: TimingKind) -> TimingStat | None:
        """The overall timing of one kind, or `None` if the report did not compute it."""
        for timing in self.timings:
            if timing.kind is kind:
                return timing
        return None

    def breakdown_for(self, dimension: DimensionKind) -> DimensionBreakdown | None:
        """The slice along one dimension, or `None` if the report did not compute it."""
        for breakdown in self.breakdowns:
            if breakdown.dimension is dimension:
                return breakdown
        return None

