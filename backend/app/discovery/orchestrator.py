"""One sweep: choose the sources, run them together, keep their failures apart.

This module is what §13 asks for and, just as importantly, what it forbids. The
pipeline it implements is

    SearchProfile → Country Pack → DiscoveryRequest(s) → SourceRegistry
                  → eligible OpportunitySource plugins → concurrent discover()
                  → SweepReport

and nowhere in it does a board appear by name. There is no `run_jobup()`, no
`if source_key == "jooble"`, no import of `adapters`: the only things this module
knows about a source are `metadata` and two coroutines. Adding a thirteenth Swiss
board is a change to `adapters/v1_catalog.py` and `country_packs/ch/sources.yaml`,
and this file does not move — which is acceptance criterion 8, and
`tests/test_v2_discovery_boundaries.py` fails if the import ever appears.

**Isolation (§14).** Sources run under a semaphore, never in an unbounded
`gather` over everything the registry returned, and each one is wrapped: an
adapter that raises instead of returning a `DiscoveryResult` becomes that
source's `UNAVAILABLE` health and costs its neighbours nothing. `TaskGroup` is
deliberately not used — its first exception cancels its siblings, which is the
opposite of the requirement here.

**What this module does not do.** It does not filter titles, exclude keywords,
score, or deduplicate across sources: `SearchProfile.excluded_keywords` is a
matching concern (Phase 9), and two boards publishing the same vacancy is what
`Opportunity.dedup_fingerprint` is for downstream. A sweep answers "what is
out there, and how complete is the answer" — nothing else.
"""
import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from time import perf_counter
from typing import Final

from backend.app.discovery import failures
from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.contracts import (
    DiscoveryMetrics,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoveryWarning,
    DiscoveryWarningCode,
    OpportunitySource,
    RadiusConstraint,
    SourceHealth,
    SourceHealthStatus,
)
from backend.app.discovery.registry import SourceRegistry
from backend.app.discovery.requests import (
    DEFAULT_LIMIT,
    DEFAULT_LOOKBACK_DAYS,
    discovery_requests_for,
)
from backend.app.domain.base import CountryCode, DomainModel, UtcDatetime
from backend.app.domain.opportunity import Opportunity
from backend.app.domain.search import SearchProfile
from country_packs.contracts import CountryPack
from country_packs.registry import CountryPackRegistry

# Duplicated from `adapters.v1_sources` rather than imported from it: this module
# may not import an adapter, and that boundary is worth more than two lines.
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since a `perf_counter()` reading.

    `perf_counter` and not the clock: a sweep's duration must not move because
    someone corrected the system time in the middle of it, and the injected clock
    stays what it is for — the timestamps a reader sees.
    """
    return max(0, round((perf_counter() - started) * 1000))


def _describe(radius: RadiusConstraint) -> str:
    """A radius the way an operator would name it, for a warning detail.

    The label when the profile gave one, coordinates otherwise: "30 km around
    Lausanne" is actionable, "30 km around a GeoPoint" is not.
    """
    where = radius.label or (f"{radius.center.latitude:.4f}, "
                             f"{radius.center.longitude:.4f}")
    return f"{radius.radius_km:g} km around {where}"


# How many sources may be in flight at once. Four because these are public boards
# being swept on someone else's infrastructure and because each source is itself
# sequential inside its worker thread: the number bounds threads, sockets and the
# blast radius of a sweep, and it is a knob rather than a constant so a test can
# pin concurrency to 1 and get a deterministic order of arrival.
MAX_CONCURRENT_SOURCES: Final = 4

# Which status a sweep should report when one source answered twice — once per
# request — with two different conditions. Ordered by how much it should worry an
# operator, not by how bad it sounds: a partial answer still carries postings, a
# missing variable is a fixed condition someone can resolve in a minute, and an
# outage is the one nobody can do anything about.
_SEVERITY: Final[Mapping[SourceHealthStatus, int]] = {
    SourceHealthStatus.HEALTHY: 0,
    SourceHealthStatus.DEGRADED: 1,
    SourceHealthStatus.MISCONFIGURED: 2,
    SourceHealthStatus.UNAVAILABLE: 3,
}


class SourceOutcome(DomainModel):
    """What one source made of one request.

    The request travels with the result so a report is self-describing: "jobup
    returned 40 postings" is not readable without "…for these two keywords around
    Yverdon", and a sweep of a profile with three areas produces three requests
    whose results would otherwise be indistinguishable.
    """

    request: DiscoveryRequest
    result: DiscoveryResult

    @property
    def source_key(self) -> str:
        """Read off the health record, which is where a result states its identity."""
        return self.result.health.source_key


class SweepReport(DomainModel):
    """Everything one sweep of one country produced, and how complete it is.

    Deliberately not just a list of opportunities. A caller that cannot see the
    health and the warnings cannot tell "nobody is hiring" from "nine boards
    answered and three were down", and docs/V2_SPECIFICATION.md §22 lists hiding
    failed source health as a non-goal.
    """

    country: CountryCode
    started_at: UtcDatetime
    duration_ms: int
    outcomes: tuple[SourceOutcome, ...] = ()
    # Sweep-level only: the ones that belong to the request rather than to a
    # source (`NO_SOURCE_SELECTED`, and §11's radius policy). Per-source warnings
    # stay inside their own `DiscoveryResult`.
    warnings: tuple[DiscoveryWarning, ...] = ()

    @property
    def opportunities(self) -> tuple[Opportunity, ...]:
        """Every opportunity found, in the order the sources were queried.

        Not deduplicated across sources: ids are source-scoped by construction, so
        the same vacancy on two boards is two `Opportunity` objects with the same
        `dedup_fingerprint`, and resolving that is the repository's job at write
        time (docs/PERSISTENCE.md) and Phase 6's when it canonicalizes employers.
        """
        return tuple(opportunity for outcome in self.outcomes
                     for opportunity in outcome.result.opportunities)

    @property
    def metrics(self) -> DiscoveryMetrics:
        """The counters summed over every source. `duration_ms` is the sweep's own.

        `DiscoveryMetrics.merge` drops durations on purpose — a concurrent sweep's
        wall clock is not the sum of its parts — so the sweep's measured elapsed
        time is put back here, where it is the honest number.
        """
        total = DiscoveryMetrics()
        for outcome in self.outcomes:
            total = total.merge(outcome.result.metrics)
        return DiscoveryMetrics(
            requests_made=total.requests_made,
            postings_seen=total.postings_seen,
            opportunities_returned=total.opportunities_returned,
            postings_skipped=total.postings_skipped,
            duration_ms=self.duration_ms)

    @property
    def health(self) -> tuple[SourceHealth, ...]:
        """One record per source swept, in key order, worst condition first.

        "Worst" because a source asked twice — once per radius area — can answer
        HEALTHY and then time out, and a report that showed the second answer only
        would depend on the order the tasks happened to finish. `_SEVERITY` says
        which of two conditions wins and why.
        """
        worst: dict[str, SourceHealth] = {}
        for outcome in self.outcomes:
            health = outcome.result.health
            current = worst.get(health.source_key)
            if current is None or _SEVERITY[health.status] > _SEVERITY[current.status]:
                worst[health.source_key] = health
        return tuple(worst[key] for key in sorted(worst))

    @property
    def source_keys(self) -> tuple[str, ...]:
        """Every source that was asked, deduplicated, in key order."""
        return tuple(health.source_key for health in self.health)

    @property
    def unusable_source_keys(self) -> tuple[str, ...]:
        """Sources whose answer must not be read as complete (§12)."""
        return tuple(health.source_key for health in self.health
                     if not health.is_usable)

    @property
    def is_complete(self) -> bool:
        """Whether every source answered fully and nothing was truncated.

        The one question a caller has to be able to ask before treating a sweep as
        the state of the market.
        """
        return not self.unusable_source_keys and not self.warnings and all(
            outcome.result.health.status is SourceHealthStatus.HEALTHY
            and not outcome.result.warnings for outcome in self.outcomes)


class DiscoveryOrchestrator:
    """Runs sweeps. Holds no source of its own and knows no board by name.

    Both registries are injected because both are composition: `bootstrap` builds
    them once per process, a test builds them per case with two fake sources, and
    neither has to touch a YAML file or a V1 module to do it.
    """

    def __init__(self, *, registry: SourceRegistry, packs: CountryPackRegistry,
                 clock: Clock = utc_now,
                 max_concurrency: int = MAX_CONCURRENT_SOURCES) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._registry = registry
        self._packs = packs
        self._clock = clock
        self._max_concurrency = max_concurrency

    async def sweep(
        self,
        profile: SearchProfile,
        *,
        country: CountryCode,
        limit: int = DEFAULT_LIMIT,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        cursor: str | None = None,
    ) -> SweepReport:
        """Sweep one country for one profile.

        Raises `CountryPackError` when the country has no pack — that is a
        composition mistake, not a discovery outcome, and §16 asks for it to be
        loud. Everything that can go wrong *after* that point is data in the
        report: no source selected, a source down, a filter not honoured.
        """
        pack = self._pack(country)
        requests = discovery_requests_for(profile, country=country, limit=limit,
                                          lookback_days=lookback_days, cursor=cursor)
        started_at = self._clock()
        started = perf_counter()
        if not requests:
            return self._report(country, started_at, started, (), (self._warn(
                DiscoveryWarningCode.NO_SOURCE_SELECTED,
                f"the profile names no area in {country}, so nothing was swept"),))

        warnings: list[DiscoveryWarning] = []
        plan: list[tuple[DiscoveryRequest, OpportunitySource]] = []
        for request in requests:
            sources = self._registry.sources_for(
                country=country, source_keys=request.source_keys, pack=pack)
            warnings.extend(self._request_warnings(request, sources))
            plan.extend((request, source) for source in sources)
        if not plan:
            # Once for the sweep, not once per request: three radius areas against
            # an empty selection are one condition an operator has to fix, not three.
            restricted = any(request.source_keys for request in requests)
            warnings.append(self._warn(
                DiscoveryWarningCode.NO_SOURCE_SELECTED,
                f"no source is both registered and enabled for {country}"
                + (" within the profile's allow-list" if restricted else "")))

        outcomes = await self._run(plan)
        for outcome in outcomes:
            self._registry.record_health(outcome.result.health)
        return self._report(country, started_at, started, outcomes, tuple(warnings))

    async def healthcheck(
        self, *, country: CountryCode | None = None
    ) -> tuple[SourceHealth, ...]:
        """Probe the sources without searching for anything, in key order.

        Same isolation as a sweep — one refusal is one record, never an exception
        that ends the round — and every answer is recorded in the registry so a
        status endpoint can read it without probing again.

        A source that does not claim `HEALTHCHECK` still answers: its adapter says
        what it knows from the last sweep instead of spending a request, which is
        what makes this safe to call on a schedule against boards behind anti-bot
        protection. `V1SourceAdapter.healthcheck` documents that reading.
        """
        pack = self._pack(country)
        sources = self._registry.sources_for(country=country, pack=pack)
        limiter = asyncio.Semaphore(self._max_concurrency)

        async def probe(source: OpportunitySource) -> SourceHealth:
            async with limiter:
                started = perf_counter()
                try:
                    return await source.healthcheck()
                except Exception as exc:
                    # Same argument as `_discover`: a probe that raises is a bug in
                    # one adapter and must not cost the other eleven their answer.
                    return failures.health_from_exception(
                        exc, source.metadata, checked_at=self._clock(),
                        latency_ms=_elapsed_ms(started))

        records = await asyncio.gather(*(probe(source) for source in sources))
        for record in records:
            self._registry.record_health(record)
        return tuple(sorted(records, key=lambda record: record.source_key))

    def _pack(self, country: CountryCode | None) -> CountryPack | None:
        """The country's pack, or `None` when the caller asked about all of them.

        Not a lookup with a default. A country being swept must have a pack, and
        `CountryPackRegistry.get` raising is §16's "malformed or missing
        configuration fails loudly"; only `None` — "every country" — answers `None`.
        """
        return None if country is None else self._packs.get(country)

    async def _run(
        self, plan: Sequence[tuple[DiscoveryRequest, OpportunitySource]]
    ) -> tuple[SourceOutcome, ...]:
        """Every (request, source) pair, at most `max_concurrency` in flight.

        `gather` over a semaphore-guarded coroutine, and deliberately not a
        `TaskGroup`: the first board to fail must not cancel the other eleven
        (§14). `return_exceptions` is not set either — `_discover` converts every
        exception into a result, so an exception arriving here would be a bug in
        this module and should be loud rather than silently collected.

        Results come back in plan order, which is registry order, which is the
        pack's priority order: `SweepReport.opportunities` inherits that, so two
        identical sweeps produce identical reports.
        """
        if not plan:
            return ()
        limiter = asyncio.Semaphore(self._max_concurrency)

        async def run_one(request: DiscoveryRequest,
                          source: OpportunitySource) -> SourceOutcome:
            async with limiter:
                return await self._discover(request, source)

        return tuple(await asyncio.gather(
            *(run_one(request, source) for request, source in plan)))

    async def _discover(self, request: DiscoveryRequest,
                        source: OpportunitySource) -> SourceOutcome:
        """One source's turn, guaranteed to produce an outcome.

        `OpportunitySource.discover` is contractually not allowed to raise, so this
        `except` catches a bug rather than an expected condition — and it still has
        to be here, because §14 says one source's fault may not corrupt another's
        result and an escaping exception would end the sweep for everybody.

        `Exception`, not `BaseException`: `asyncio.CancelledError` is not an
        `Exception`, so a cancelled sweep stays cancelled instead of being recorded
        as twelve source outages.
        """
        started = perf_counter()
        try:
            result = await source.discover(request)
        except Exception as exc:
            latency_ms = _elapsed_ms(started)
            result = DiscoveryResult(
                # `failures` is the only module allowed to read an exception, and
                # the reason is one line above the call: `str(exc)` from a jooble
                # failure contains the API key.
                health=failures.health_from_exception(
                    exc, source.metadata, checked_at=self._clock(),
                    latency_ms=latency_ms),
                # The counters stay at zero. An adapter that raised never reported
                # what it spent, and a guess here would make `requests_made`
                # unreadable as a signal; the measured wall clock is real, so it
                # is the one number kept.
                metrics=DiscoveryMetrics(duration_ms=latency_ms))
        return SourceOutcome(request=request, result=result)

    def _request_warnings(
        self, request: DiscoveryRequest, sources: Sequence[OpportunitySource]
    ) -> tuple[DiscoveryWarning, ...]:
        """What is true of one request's whole source set rather than of one source.

        Today that is §11 and only §11. Each adapter already warns that *it* cannot
        filter by distance; this one answers the different question a caller
        actually has — "were these results distance-filtered at all?" — which no
        single source's warning can answer, and it is emitted only when the answer
        is no for every source that will run.

        The policy it makes explicit: the centre travels as a place name, the
        radius is not applied, and nothing pretends otherwise. Phase 7 owns
        database-backed radius semantics.
        """
        if request.radius is None or not sources:
            return ()
        if any(source.metadata.supports(SourceCapability.RADIUS_SEARCH)
               for source in sources):
            return ()
        return (self._warn(
            DiscoveryWarningCode.RADIUS_NOT_SUPPORTED,
            f"no source swept filters by distance, so {_describe(request.radius)} "
            "was sent as a place name only and these results are not "
            "radius-filtered", SourceCapability.RADIUS_SEARCH),)

    @staticmethod
    def _warn(code: DiscoveryWarningCode, detail: str,
              capability: SourceCapability | None = None) -> DiscoveryWarning:
        """A sweep-level warning, which by construction names no source.

        `DiscoveryResult` refuses a warning attributed to another source; the
        mirror of that rule is that a warning about the *request* must not be
        attributed to a source at all, or a reader would go looking for a fault in
        a board that behaved exactly as it says it does.
        """
        return DiscoveryWarning(code=code, detail=detail, capability=capability)

    def _report(self, country: CountryCode, started_at: datetime, started: float,
                outcomes: tuple[SourceOutcome, ...],
                warnings: tuple[DiscoveryWarning, ...]) -> SweepReport:
        """Stamp a report with the sweep's own elapsed time. The one exit point."""
        return SweepReport(country=country, started_at=started_at,
                           duration_ms=_elapsed_ms(started), outcomes=outcomes,
                           warnings=warnings)
