"""`CareerAnalyticsService` — the deterministic funnel the recommendation engine reads (§13-25).

The "measure" link. It reads one account's applications (for the population and, from the
authoritative Phase-12 event trail, the real submitted-at anchor), its effective outcomes (the
hiring facts), its role classifications and the postings they name, and the exact candidate-
document versions the applications pinned (for the by-role/source/type/document-strategy slices),
and folds them into a single self-describing `CareerAnalytics` report. No provider is anywhere
near the math (§24-25, §47): this is arithmetic over rows, and the same inputs always yield the
same report, stamped with the `CAREER_ANALYTICS_VERSION`.

Four disciplines the spec makes acceptance-critical, each enforced here rather than hoped for:

- **Count applications, not events (§13).** An application's furthest *effective* outcome fixes the
  stage it reached, via the pure `furthest_funnel_stage`; three interview rounds advance it to
  `INTERVIEW` once. The funnel base is every application that reached the employer — a submitted
  Phase-12 state, or (independently) any effective outcome, since an employer signal implies a
  submission. A `FAILED` execution that never reached the employer is not in the funnel.
- **Observe, never drive (§2, §84).** This service reads `ApplicationState` to decide who was
  submitted; it never writes one. A `REJECTED` outcome lowers no rate by touching execution state —
  it simply is not a funnel rung. Maturity is decided from the *outcomes* (a terminal hiring fact),
  not from a Phase-12 terminal state, keeping the two axes cleanly apart.
- **Silence is censored, never failure (§15-16).** Every denominator asks `is_application_mature`
  with the right reference instant — the real submitted-at for submission-anchored rates, the base
  step's own instant for later transitions — so a fresh application is withheld, not scored as a
  rejection, and the split is reported in `MaturityCensoring`. An application whose real submission
  instant is unknown is excluded from a submission-anchored metric outright, never anchored to a
  creation time that would fabricate a duration.
- **Self-describing, versioned (§14, §24-25).** Every rate carries numerator/denominator/window;
  every timing its sample and window; the report its version. An unclassified role, source or type
  is a `None`-keyed cell, never a catch-all bucket.
"""
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from backend.app.domain.analytics import (
    CAREER_ANALYTICS_VERSION,
    DEFAULT_OBSERVATION_HORIZON_DAYS,
    CareerAnalytics,
    CareerFunnel,
    ConversionRate,
    DimensionBreakdown,
    DimensionCell,
    DimensionKind,
    FunnelStage,
    FunnelStageCount,
    MaturityCensoring,
    ObservationWindow,
    RateKind,
    TimingKind,
    TimingStat,
    furthest_funnel_stage,
    is_application_mature,
    outcome_stage,
    rate_stages,
    stage_rank,
)
from backend.app.domain.application import Application, ApplicationState
from backend.app.domain.application_event import ApplicationEventType
from backend.app.domain.documents import CandidateDocument
from backend.app.domain.identifiers import UserId
from backend.app.domain.opportunity import Opportunity
from backend.app.domain.outcome import (
    TERMINAL_OUTCOME_KINDS,
    ApplicationOutcome,
    OutcomeKind,
)
from backend.app.domain.role import classify_role_family
from backend.app.repositories.contracts import (
    ApplicationEventRepository,
    ApplicationOutcomeRepository,
    ApplicationRepository,
    CandidateDocumentRepository,
    OpportunityRepository,
    RoleClassificationRepository,
)

# A single account's applications and outcomes are read whole to be measured — analytics is an
# aggregate over the entire population, not a page of it. This bound is generous enough for any
# real job search yet finite, so a pathological account cannot make one report unbounded; true
# pagination is a scaling concern past Phase 15, noted rather than pretended.
_ANALYTICS_SCAN_LIMIT = 10_000

# The Phase-12 execution states that mean an application reached the employer. `WITHDRAWN` is
# included because the transition graph only reaches it from `SUBMITTED`/`SUBMISSION_STATE_UNKNOWN`,
# so a withdrawal implies a prior submission; `FAILED` is excluded because a submission that failed
# never reached the employer (§2 — execution failure is not a hiring fact).
_SUBMITTED_STATES: frozenset[ApplicationState] = frozenset({
    ApplicationState.SUBMITTED,
    ApplicationState.SUBMISSION_STATE_UNKNOWN,
    ApplicationState.WITHDRAWN,
})

# The outcome kinds that count as the employer's first *response* — any signal the employer sent,
# a rejection included, but never the candidate's own later decision (an accepted/declined offer)
# nor a withdrawal. Time-to-first-response is measured to the earliest of these (§17).
_FIRST_RESPONSE_KINDS: frozenset[OutcomeKind] = frozenset({
    OutcomeKind.ACKNOWLEDGED,
    OutcomeKind.SCREEN,
    OutcomeKind.ASSESSMENT,
    OutcomeKind.INTERVIEW,
    OutcomeKind.OFFER_RECEIVED,
    OutcomeKind.REJECTED,
})

# The kinds that all imply an offer was *received*, whatever the candidate later did with it — so
# time-to-offer is measured to the earliest of them, and the OFFER stage's instant likewise.
_OFFER_KINDS: frozenset[OutcomeKind] = frozenset({
    OutcomeKind.OFFER_RECEIVED,
    OutcomeKind.OFFER_DECLINED,
    OutcomeKind.OFFER_ACCEPTED,
})


# The funnel ladder rebuilt from the public rank, so this module never depends on the domain's
# private `_STAGE_ORDER`: sorting the members by their rank yields exactly the ordered ladder
# `CareerFunnel` demands, and a stage inserted in the domain flows through here unchanged.
_LADDER: tuple[FunnelStage, ...] = tuple(sorted(FunnelStage, key=stage_rank))

# Which outcome kinds close each timing measurement. The duration for a timing is measured from
# the applied-at anchor to the *earliest* outcome whose kind is in the set, so a timing exists
# only when both real instants exist (§17). First-response takes any employer signal; interview
# takes the first interview; offer takes the first offer *received* (however it was later
# resolved); decision takes whichever terminal outcome concluded the process.
_TIMING_END_KINDS: dict[TimingKind, frozenset[OutcomeKind]] = {
    TimingKind.TIME_TO_FIRST_RESPONSE: _FIRST_RESPONSE_KINDS,
    TimingKind.TIME_TO_INTERVIEW: frozenset({OutcomeKind.INTERVIEW}),
    TimingKind.TIME_TO_OFFER: _OFFER_KINDS,
    TimingKind.TIME_TO_DECISION: TERMINAL_OUTCOME_KINDS,
}


def _percentile(sorted_values: list[float], quantile: float) -> float:
    """A linear-interpolated percentile (the type-7 recipe), monotonic in `quantile`.

    Monotonicity is the point: because the estimate rises with the quantile over one sample,
    `p25 <= median <= p75` holds by construction, so `TimingStat`'s ordering invariant is a
    property of the arithmetic rather than something to hope the data respects. A single value
    is its own every-percentile.
    """
    count = len(sorted_values)
    if count == 1:
        return sorted_values[0]
    position = quantile * (count - 1)
    lower = int(position)
    upper = min(lower + 1, count - 1)
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def _window(instants: Iterable[datetime | None]) -> ObservationWindow:
    """The submitted-at span of a population, or the empty window when it is empty (§14).

    `None` anchors — applications whose real submission instant is unavailable — carry no span
    and are filtered out, so the window is the true range of the submissions it could observe,
    never widened by an application it cannot place in time.
    """
    ordered = sorted(instant for instant in instants if instant is not None)
    if not ordered:
        return ObservationWindow()
    return ObservationWindow(earliest_applied_at=ordered[0], latest_applied_at=ordered[-1])


def _document_strategy_signature(application: Application,
                                 documents: dict[str, CandidateDocument]) -> str | None:
    """The stable document-strategy key an application's pinned documents form, or `None` (§21).

    Built from the *exact* pinned versions (`document.version(pinned.version)`, never `.latest`),
    so two applications sent with genuinely different document strategies land in different
    cohorts and one whose author later revised a document is not retroactively moved. Each
    resolved version contributes `type=generator_key/language`, with `unknown` standing in for a
    version minted before its generator was recorded; the components are sorted and joined so the
    signature is order-independent. `None` when the application pinned nothing or none of its
    pinned versions still resolve — an honest unclassified cell, never a fabricated cohort.
    """
    components: list[str] = []
    for pinned in application.pinned_documents:
        document = documents.get(str(pinned.document_id))
        if document is None:
            continue
        version = document.version(pinned.version)
        if version is None:
            continue
        generator = version.generator_key or "unknown"
        components.append(f"{pinned.document_type.value}={generator}/{version.language}")
    if not components:
        return None
    return "|".join(sorted(components))


@dataclass(frozen=True)
class _ApplicationFacts:
    """Everything one funnel application contributes, gathered once so the math reads plainly.

    Pairs an `Application` (the execution state the funnel base reads) with its *effective*
    outcomes only — superseded and retracted rows never reach the count (§48) — and with the
    application's real `submitted_at` instant, the earliest Phase-12 `SUBMITTED` event's time or
    `None` when no submission is on the authoritative trail. The anchor is that submission fact,
    never `application.created_at`, which precedes any submission. The derived views are the
    vocabulary the funnel, the rates and the timings share, computed from the same rows so no two
    of them can disagree about how far this application got.
    """

    application: Application
    outcomes: tuple[ApplicationOutcome, ...]
    submitted_at: datetime | None

    @property
    def kinds(self) -> frozenset[OutcomeKind]:
        """The distinct effective outcome kinds recorded — the input to the furthest-stage rule."""
        return frozenset(outcome.kind for outcome in self.outcomes)

    @property
    def furthest_stage(self) -> FunnelStage:
        """The deepest stage this application reached, deduplicated over its outcomes (§13)."""
        return furthest_funnel_stage(self.kinds)

    @property
    def has_terminal_outcome(self) -> bool:
        """Whether a terminal hiring fact has concluded this application — the maturity input."""
        return any(outcome.kind in TERMINAL_OUTCOME_KINDS for outcome in self.outcomes)

    def submission_maturity(self, *, as_of: datetime, horizon_days: int) -> bool | None:
        """Whether this submission is mature enough to score, or `None` when it cannot be judged.

        A terminal hiring fact concludes the process, so a concluded application is mature
        whatever its submission instant. Absent a terminal fact, maturity is a real elapsed span
        and so needs the real `submitted_at`: without one the application is neither mature nor
        censored — it is simply excluded from submission-anchored metrics rather than invented
        into one bucket or the other.
        """
        if self.has_terminal_outcome:
            return True
        if self.submitted_at is None:
            return None
        return is_application_mature(
            reference=self.submitted_at, as_of=as_of,
            has_terminal_outcome=False, horizon_days=horizon_days)

    def reached_stage_at(self, stage: FunnelStage) -> datetime | None:
        """When this application first reached `stage` or beyond, or `None` if it never did.

        The earliest `occurred_at` among outcomes deep enough to imply the stage, so a rate whose
        base is a mid-funnel step measures maturity from when that step was actually reached.
        """
        threshold = stage_rank(stage)
        instants = [outcome.occurred_at for outcome in self.outcomes
                    if (reached := outcome_stage(outcome.kind)) is not None
                    and stage_rank(reached) >= threshold]
        return min(instants) if instants else None

    def first_time_of(self, kinds: frozenset[OutcomeKind]) -> datetime | None:
        """The earliest `occurred_at` among outcomes of these kinds, or `None` if none occurred."""
        instants = [outcome.occurred_at for outcome in self.outcomes if outcome.kind in kinds]
        return min(instants) if instants else None


class CareerAnalyticsService:
    """Fold one account's applications and outcomes into a deterministic, versioned report.

    Holds the stores it *reads* and writes to none of them: the outcome store (the hiring facts),
    the application store (the population and the execution state that says who reached the
    employer), the application-event store (the authoritative Phase-12 trail the real submission
    instant is read from), the role-classification store, the opportunity store (the
    by-role/source/type axes) and the candidate-document store (the exact pinned versions the
    document-strategy axis is built from). No clock in the constructor — `report` takes `now` —
    the convention every V2 service keeps, so a single report's censoring `as_of` and
    `computed_at` agree. Nothing here can move an `ApplicationState`: this is the "measure" link,
    never the "drive" one (§2, §84).
    """

    def __init__(self, outcomes: ApplicationOutcomeRepository,
                 applications: ApplicationRepository,
                 events: ApplicationEventRepository,
                 roles: RoleClassificationRepository,
                 opportunities: OpportunityRepository,
                 documents: CandidateDocumentRepository) -> None:
        self._outcomes = outcomes
        self._applications = applications
        self._events = events
        self._roles = roles
        self._opportunities = opportunities
        self._documents = documents

    async def report(self, user_id: UserId, *, now: datetime,
                     horizon_days: int = DEFAULT_OBSERVATION_HORIZON_DAYS) -> CareerAnalytics:
        """Compute this account's whole funnel report as of `now`, under one horizon (§13-25).

        Reads the population, folds every application to its furthest effective stage, and reports
        the funnel, the four named rates, the four timings and the role/source/type breakdowns —
        each self-describing, all over one window, all stamped with `CAREER_ANALYTICS_VERSION`.
        The same inputs always yield the same report; a fresh application is censored, never
        counted as a rejection.
        """
        facts = await self._gather(user_id)
        window = _window(fact.submitted_at for fact in facts)
        funnel = self._funnel(facts, window, now=now, horizon_days=horizon_days)
        rates = tuple(self._rate(kind, facts, window, now=now, horizon_days=horizon_days)
                      for kind in RateKind)
        timings = tuple(self._timing(kind, facts, window) for kind in TimingKind)
        breakdowns = await self._breakdowns(
            user_id, facts, now=now, horizon_days=horizon_days)
        return CareerAnalytics(
            user_id=user_id,
            analytics_version=CAREER_ANALYTICS_VERSION,
            window=window,
            funnel=funnel,
            rates=rates,
            timings=timings,
            breakdowns=breakdowns,
            computed_at=now)


    async def _gather(self, user_id: UserId) -> tuple[_ApplicationFacts, ...]:
        """Read the whole population and pair each funnel application with its effective outcomes.

        The funnel base is every application that reached the employer: one in a submitted
        execution state, or (independently) one carrying any effective outcome, since an employer
        signal implies a submission. A `FAILED` execution that never reached the employer and has
        no outcome is left out — an execution failure is not a hiring fact (§2).
        """
        applications = await self._applications.list_for_user(
            user_id, limit=_ANALYTICS_SCAN_LIMIT)
        outcomes = await self._outcomes.list_for_user(user_id, limit=_ANALYTICS_SCAN_LIMIT)
        effective: dict[str, list[ApplicationOutcome]] = {}
        for outcome in outcomes:
            if outcome.is_effective:
                effective.setdefault(str(outcome.application_id), []).append(outcome)
        facts: list[_ApplicationFacts] = []
        for application in applications:
            app_outcomes = tuple(effective.get(str(application.id), ()))
            if application.state in _SUBMITTED_STATES or app_outcomes:
                submitted_at = await self._submitted_at(user_id, application)
                facts.append(_ApplicationFacts(
                    application=application, outcomes=app_outcomes,
                    submitted_at=submitted_at))
        return tuple(facts)

    async def _submitted_at(self, user_id: UserId,
                            application: Application) -> datetime | None:
        """The real instant this application reached the employer, from its Phase-12 trail (§17).

        The earliest `SUBMITTED` event's `occurred_at` — the authoritative submission fact —
        never `application.created_at`, which precedes any submission and can miss it by days.
        The trail is read only for an application in a submitted execution state; anything else
        has no submission to anchor to, so its instant is honestly `None` (excluded from
        submission-anchored metrics rather than fabricated from a creation time).
        """
        if application.state not in _SUBMITTED_STATES:
            return None
        events = await self._events.list_for_application(
            user_id, application.id, limit=_ANALYTICS_SCAN_LIMIT)
        instants = [event.occurred_at for event in events
                    if event.event_type is ApplicationEventType.SUBMITTED]
        return min(instants) if instants else None

    def _funnel(self, facts: tuple[_ApplicationFacts, ...], window: ObservationWindow, *,
                now: datetime, horizon_days: int) -> CareerFunnel:
        """The cumulative per-stage count and the maturity split of the submitted base (§13-16).

        Maturity is three-valued per application: mature (concluded or past the horizon since its
        real submission), censored (submitted but still within the horizon), or unjudgeable (no
        submission instant on the trail). Only the first two are reported; an application whose
        submission cannot be placed in time is excluded from both counts rather than mislabelled
        as either observed or too-recent.
        """
        stages = tuple(
            FunnelStageCount(
                stage=stage,
                applications=sum(
                    1 for fact in facts
                    if stage_rank(fact.furthest_stage) >= stage_rank(stage)))
            for stage in _LADDER)
        maturities = [fact.submission_maturity(as_of=now, horizon_days=horizon_days)
                      for fact in facts]
        censoring = MaturityCensoring(
            observation_horizon_days=horizon_days, as_of=now,
            mature_count=sum(1 for maturity in maturities if maturity is True),
            censored_count=sum(1 for maturity in maturities if maturity is False))
        return CareerFunnel(stages=stages, window=window, censoring=censoring)


    def _rate(self, kind: RateKind, facts: tuple[_ApplicationFacts, ...],
              window: ObservationWindow, *, now: datetime, horizon_days: int) -> ConversionRate:
        """One named rate over `facts`: successes at the target over the mature base (§15-16, §23).

        An application that reached the *target* counts in numerator and denominator both — its
        progress is itself the observation, needing no submission instant. One that reached the
        *base* but not the target counts in the denominator only once it is mature: for the
        submitted base that is `submission_maturity` (which excludes an application whose real
        submission instant is unknown, never scoring it from a creation time), for a later
        transition it is maturity measured from when that base step was reached. A fresh one is
        censored out, never scored as a failure. So the numerator can never exceed the
        denominator, and a too-thin base yields a `denominator` of zero and a `None` rate.
        """
        base, target = rate_stages(kind)
        base_rank, target_rank = stage_rank(base), stage_rank(target)
        numerator = denominator = 0
        for fact in facts:
            reached = stage_rank(fact.furthest_stage)
            if reached < base_rank:
                continue
            if reached >= target_rank:
                numerator += 1
                denominator += 1
                continue
            if base is FunnelStage.SUBMITTED:
                if fact.submission_maturity(as_of=now, horizon_days=horizon_days) is True:
                    denominator += 1
                continue
            reference = fact.reached_stage_at(base)
            if reference is None:
                continue
            if is_application_mature(
                    reference=reference, as_of=now,
                    has_terminal_outcome=fact.has_terminal_outcome, horizon_days=horizon_days):
                denominator += 1
        return ConversionRate(
            kind=kind, numerator=numerator, denominator=denominator, window=window)

    def _timing(self, kind: TimingKind, facts: tuple[_ApplicationFacts, ...],
                window: ObservationWindow) -> TimingStat:
        """One elapsed-time measurement, in real days, as a median with a p25/p75 spread (§17).

        A duration exists only when both ends really happened, measured from the real submission
        instant to the earliest outcome that closes this timing; an application with no submission
        instant on the trail is skipped, and a negative span (an outcome predating the submission,
        an impossible chronology) is dropped rather than trusted. With no completed durations the
        stat is an honest absence — sample zero, no quartiles — never a fabricated zero.
        """
        durations = sorted(
            days for fact in facts
            if fact.submitted_at is not None
            and (end := fact.first_time_of(_TIMING_END_KINDS[kind])) is not None
            and (days := (end - fact.submitted_at).total_seconds() / 86400.0) >= 0.0)
        if not durations:
            return TimingStat(kind=kind, sample_size=0, window=window)
        return TimingStat(
            kind=kind, sample_size=len(durations),
            median_days=_percentile(durations, 0.5),
            p25_days=_percentile(durations, 0.25),
            p75_days=_percentile(durations, 0.75),
            window=window)


    async def _breakdowns(self, user_id: UserId, facts: tuple[_ApplicationFacts, ...], *,
                          now: datetime,
                          horizon_days: int) -> tuple[DimensionBreakdown, ...]:
        """The funnel sliced by role family, source, opportunity type and document strategy.

        The four §18-21 axes. Loads the postings the applications name (a shared fact, read by id),
        this account's role
        classifications, and the exact candidate-document versions the applications pinned, then
        keys each application onto each axis. An application whose axis value is unknown — a
        spontaneous target with no posting, an unclassified title, a posting without a type, an
        application that pinned no document or whose pinned versions no longer resolve — falls
        into the honest `None` cell, never a catch-all bucket.
        """
        opportunities: dict[str, Opportunity] = {}
        for opportunity_id in {fact.application.opportunity_id for fact in facts
                               if fact.application.opportunity_id is not None}:
            opportunity = await self._opportunities.get(opportunity_id)
            if opportunity is not None:
                opportunities[str(opportunity_id)] = opportunity
        classifications = await self._roles.list_for_user(
            user_id, limit=_ANALYTICS_SCAN_LIMIT)
        by_opportunity = {str(item.opportunity_id): item for item in classifications}
        documents: dict[str, CandidateDocument] = {}
        for document_id in {pinned.document_id for fact in facts
                            for pinned in fact.application.pinned_documents}:
            document = await self._documents.get(user_id, document_id)
            if document is not None:
                documents[str(document_id)] = document

        def role_key(fact: _ApplicationFacts) -> str | None:
            opportunity = self._opportunity_of(fact, opportunities)
            classification = by_opportunity.get(str(fact.application.opportunity_id))
            if classification is not None and classification.role_family is not None:
                return classification.role_family.value
            if opportunity is None:
                return None
            family = classify_role_family(opportunity.title)
            return family.value if family is not None else None

        def source_key(fact: _ApplicationFacts) -> str | None:
            opportunity = self._opportunity_of(fact, opportunities)
            return opportunity.source.source_key if opportunity is not None else None

        def type_key(fact: _ApplicationFacts) -> str | None:
            opportunity = self._opportunity_of(fact, opportunities)
            if opportunity is None or opportunity.opportunity_type is None:
                return None
            return opportunity.opportunity_type.value

        def document_key(fact: _ApplicationFacts) -> str | None:
            return _document_strategy_signature(fact.application, documents)

        keyers: dict[DimensionKind, Callable[[_ApplicationFacts], str | None]] = {
            DimensionKind.ROLE_FAMILY: role_key,
            DimensionKind.SOURCE: source_key,
            DimensionKind.OPPORTUNITY_TYPE: type_key,
            DimensionKind.DOCUMENT_STRATEGY: document_key,
        }
        return tuple(
            self._breakdown(dimension, keyer, facts, now=now, horizon_days=horizon_days)
            for dimension, keyer in keyers.items())

    @staticmethod
    def _opportunity_of(fact: _ApplicationFacts,
                        opportunities: dict[str, Opportunity]) -> Opportunity | None:
        """The posting an application named, or `None` for a spontaneous or unresolved target."""
        opportunity_id = fact.application.opportunity_id
        return opportunities.get(str(opportunity_id)) if opportunity_id is not None else None

    def _breakdown(self, dimension: DimensionKind,
                   keyer: "Callable[[_ApplicationFacts], str | None]",
                   facts: tuple[_ApplicationFacts, ...], *,
                   now: datetime, horizon_days: int) -> DimensionBreakdown:
        """Group `facts` onto one axis and compute each cell's counts and computable rates.

        A cell reports only the rates it has a base for — a rate whose denominator is zero is
        omitted rather than shown as `None`, so a thin slice is honestly silent about a rate it
        cannot compute. Cells are ordered by key with the `None` (unclassified) cell last, so a
        report is stable across runs.
        """
        grouped: dict[str | None, list[_ApplicationFacts]] = {}
        for fact in facts:
            grouped.setdefault(keyer(fact), []).append(fact)
        cells: list[DimensionCell] = []
        for key in sorted(grouped, key=lambda value: (value is None, value or "")):
            cell_facts = tuple(grouped[key])
            cell_window = _window(fact.submitted_at for fact in cell_facts)
            rates = tuple(
                rate for kind in RateKind
                if (rate := self._rate(
                    kind, cell_facts, cell_window,
                    now=now, horizon_days=horizon_days)).denominator > 0)
            cells.append(DimensionCell(
                dimension=dimension, key=key,
                applications=len(cell_facts), rates=rates))
        return DimensionBreakdown(
            dimension=dimension, cells=tuple(cells),
            window=_window(fact.submitted_at for fact in facts))






