"""One company discovery pass: choose the providers, run them, keep failures apart.

The company-side counterpart of `backend.app.discovery.orchestrator`, and the same
two properties are the point of the module:

**No provider appears by name** (§18). There is no `if provider_key == "greenhouse"`
and no import of `providers/`: everything this module knows about a provider is
`metadata` and two coroutines. Registering a fourth provider is a change to
`bootstrap.py`, and this file does not move.

**One provider failing costs its neighbours nothing** (§26). Providers run under a
semaphore inside `gather`, never a `TaskGroup` — `TaskGroup`'s first exception
cancels its siblings, which is precisely the behaviour §26 forbids. A provider that
raises instead of returning a result becomes that provider's `UNAVAILABLE` health.

**What this module does not do.** It does not resolve identities, does not
deduplicate across providers and writes nothing. Two providers reporting the same
employer is expected — a configured ATS organization and a stored opportunity often
describe one company — and turning that into a single `company_id` is
`resolution.compare` reading the database, which §17 puts in an application service.
A pass answers "which employers did we hear about, and how complete is that answer".
"""
import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from time import perf_counter
from typing import Final

from backend.app.companies import failures
from backend.app.companies.contracts import (
    CompanyDiscoveryCapability,
    CompanyDiscoveryProvider,
    CompanyDiscoveryRequest,
    CompanyDiscoveryResult,
    CompanyDiscoveryWarning,
    CompanyDiscoveryWarningCode,
    DiscoveredCompany,
    ProviderHealth,
    ProviderHealthStatus,
)
from backend.app.companies.registry import CompanyProviderRegistry
from backend.app.domain.base import CountryCode, DomainModel, UtcDatetime

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since a `perf_counter()` reading.

    `perf_counter` and not the clock, for the reason its Phase 5 twin gives: a
    pass's duration must not move because someone corrected the system time in the
    middle of it. The injected clock stays what it is for — the timestamps a reader
    sees.
    """
    return max(0, round((perf_counter() - started) * 1000))


# How many providers may be in flight at once. Four, matching Phase 5's number, and
# generous for Phase 6: every provider reads a local file or the local database, so
# the limiter is here to bound a future provider rather than today's three. A knob
# so a test can pin it to 1 and get a deterministic order of arrival.
MAX_CONCURRENT_PROVIDERS: Final = 4

# Which condition wins when one provider is asked twice in one process. Same order
# and same argument as `discovery.orchestrator._SEVERITY`: a partial answer still
# carries companies, a missing variable is fixable in a minute, an outage is not.
_SEVERITY: Final[Mapping[ProviderHealthStatus, int]] = {
    ProviderHealthStatus.HEALTHY: 0,
    ProviderHealthStatus.DEGRADED: 1,
    ProviderHealthStatus.MISCONFIGURED: 2,
    ProviderHealthStatus.UNAVAILABLE: 3,
}


class CompanyDiscoveryReport(DomainModel):
    """Everything one pass produced, and how complete it is.

    Deliberately not just a list of companies. A caller that cannot see the health
    and the warnings cannot tell "no employer is configured" from "the one provider
    that knows them could not read its file", and docs/V2_SPECIFICATION.md §22 lists
    hiding failed source health as a non-goal. §28's acceptance criterion — a
    configured company with zero opportunities still being found — is only
    demonstrable if the empty case and the broken case are distinguishable.
    """

    country: CountryCode | None = None
    started_at: UtcDatetime
    duration_ms: int
    results: tuple[CompanyDiscoveryResult, ...] = ()
    # Pass-level only: the ones that belong to the request rather than to a
    # provider's own reading of its source. Per-provider warnings stay inside their
    # `CompanyDiscoveryResult`, except the two this module is the only one able to
    # notice — see `_pass_warnings`.
    warnings: tuple[CompanyDiscoveryWarning, ...] = ()

    @property
    def companies(self) -> tuple[DiscoveredCompany, ...]:
        """Every company reported, in the order the providers ran.

        Not deduplicated: the same employer arriving from two providers is two
        claims with two provenances, and collapsing them here would throw away the
        second discovery record §5 asks to keep. The service resolves them one by
        one against what is stored.
        """
        return tuple(company for result in self.results
                     for company in result.companies)

    @property
    def health(self) -> tuple[ProviderHealth, ...]:
        """One record per provider, in key order, worst condition first."""
        worst: dict[str, ProviderHealth] = {}
        for result in self.results:
            health = result.health
            current = worst.get(health.source_key)
            if current is None or _SEVERITY[health.status] > _SEVERITY[current.status]:
                worst[health.source_key] = health
        return tuple(worst[key] for key in sorted(worst))

    @property
    def provider_keys(self) -> tuple[str, ...]:
        """Every provider that was asked, deduplicated, in key order."""
        return tuple(health.source_key for health in self.health)

    @property
    def unusable_provider_keys(self) -> tuple[str, ...]:
        """Providers whose answer must not be read as complete (§26)."""
        return tuple(health.source_key for health in self.health
                     if not health.is_usable)

    @property
    def is_complete(self) -> bool:
        """Whether every provider answered fully and nothing was truncated."""
        return not self.unusable_provider_keys and not self.warnings and all(
            result.health.status is ProviderHealthStatus.HEALTHY
            and not result.warnings for result in self.results)


class CompanyDiscoveryOrchestrator:
    """Runs company discovery passes. Owns no provider and knows none by name.

    The registry is injected because it *is* the composition: `bootstrap` builds it
    once per process, a test builds it per case with two fake providers, and neither
    has to touch a YAML file to do it.

    No `CountryPackRegistry`, unlike its Phase 5 twin, and the asymmetry is
    deliberate: a source is chosen partly by a pack's `SourceBinding`, whereas a
    provider is chosen by the country it declares — `CompanyProviderMetadata.serves`
    is the whole rule. A pack is read where §19 says it is read: by `identity` and
    `resolution`, for legal suffixes and domain preferences, which is canonicalization
    rather than selection.
    """

    def __init__(self, *, registry: CompanyProviderRegistry,
                 clock: Clock = utc_now,
                 max_concurrency: int = MAX_CONCURRENT_PROVIDERS) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._registry = registry
        self._clock = clock
        self._max_concurrency = max_concurrency

    async def run(self, request: CompanyDiscoveryRequest) -> CompanyDiscoveryReport:
        """Ask every eligible provider for companies, once.

        Raises nothing a caller has to catch for correctness. An empty registry, a
        country nobody serves, a provider that crashes: each is a fact in the
        report, because a pass that answered for two providers out of three is a
        useful answer and an exception is not.
        """
        started_at = self._clock()
        started = perf_counter()
        providers = self._registry.providers_for(
            country=request.country, provider_keys=request.provider_keys)
        warnings = self._pass_warnings(request, providers)

        results = await self._run(request, providers)
        for result in results:
            self._registry.record_health(result.health)
        return CompanyDiscoveryReport(
            country=request.country, started_at=started_at,
            duration_ms=_elapsed_ms(started), results=results, warnings=warnings)

    async def healthcheck(
        self, *, country: CountryCode | None = None
    ) -> tuple[ProviderHealth, ...]:
        """Probe the providers without running a pass, in key order.

        Same isolation as `run` — one refusal is one record, never an exception that
        ends the round — and every answer is recorded in the registry so a status
        endpoint can read it without probing again.
        """
        providers = self._registry.providers_for(country=country)
        limiter = asyncio.Semaphore(self._max_concurrency)

        async def probe(provider: CompanyDiscoveryProvider) -> ProviderHealth:
            async with limiter:
                started = perf_counter()
                try:
                    return await provider.healthcheck()
                except Exception as exc:
                    # Same argument as `_discover`: a probe that raises is a bug in
                    # one provider and must not cost the others their answer.
                    return failures.health_from_exception(
                        exc, provider.metadata, checked_at=self._clock(),
                        latency_ms=_elapsed_ms(started))

        records = await asyncio.gather(*(probe(provider) for provider in providers))
        for record in records:
            self._registry.record_health(record)
        return tuple(sorted(records, key=lambda record: record.source_key))

    async def _run(
        self, request: CompanyDiscoveryRequest,
        providers: Sequence[CompanyDiscoveryProvider],
    ) -> tuple[CompanyDiscoveryResult, ...]:
        """Every provider, at most `max_concurrency` in flight.

        `gather` over a semaphore-guarded coroutine, and deliberately not a
        `TaskGroup`: the first provider to fail must not cancel the others (§26).
        `return_exceptions` is not set either — `_discover` converts every exception
        into a result, so an exception arriving here would be a bug in this module
        and should be loud rather than silently collected.

        Results come back in provider order, which is `(priority, provider_key)`
        order, so two identical passes produce identical reports (§23).
        """
        if not providers:
            return ()
        limiter = asyncio.Semaphore(self._max_concurrency)

        async def run_one(
            provider: CompanyDiscoveryProvider) -> CompanyDiscoveryResult:
            async with limiter:
                return await self._discover(request, provider)

        return tuple(await asyncio.gather(
            *(run_one(provider) for provider in providers)))

    async def _discover(self, request: CompanyDiscoveryRequest,
                        provider: CompanyDiscoveryProvider,
                        ) -> CompanyDiscoveryResult:
        """One provider's turn, guaranteed to produce a result.

        `CompanyDiscoveryProvider.discover` is contractually not allowed to raise,
        so this `except` catches a bug rather than an expected condition — and it
        still has to be here, because §26 says one provider's fault may not corrupt
        another's result and an escaping exception would end the pass for everybody.

        `Exception`, not `BaseException`: `asyncio.CancelledError` is not an
        `Exception`, so a cancelled pass stays cancelled instead of being recorded as
        three provider outages.
        """
        metadata = provider.metadata
        started = perf_counter()
        try:
            result = await provider.discover(request)
        except Exception as exc:
            return CompanyDiscoveryResult(
                provider_key=metadata.provider_key,
                # `failures` is the only module allowed to read an exception, and
                # the reason is one line above the call: an exception message can
                # carry the credential that caused it (§26).
                health=failures.health_from_exception(
                    exc, metadata, checked_at=self._clock(),
                    latency_ms=_elapsed_ms(started)),
                completed_at=self._clock())
        return self._within_limit(result, request)

    def _within_limit(self, result: CompanyDiscoveryResult,
                      request: CompanyDiscoveryRequest) -> CompanyDiscoveryResult:
        """Hold a provider to the `limit` it was given, and say so if it went over.

        A provider is expected to enforce its own limit and to warn — every Phase 6
        provider does. This is the backstop `MAX_COMPANIES_PER_PROVIDER` exists for:
        without it, one provider ignoring `limit` would hand an unbounded list to a
        service that would then write all of it, and §1 asks that only justified
        information be persisted. Truncation is announced rather than silent, and the
        provider's own warnings are kept.
        """
        if len(result.companies) <= request.limit:
            return result
        dropped = len(result.companies) - request.limit
        truncation = CompanyDiscoveryWarning(
            code=CompanyDiscoveryWarningCode.LIMIT_TRUNCATED,
            detail=f"returned {len(result.companies)} companies for a limit of "
                   f"{request.limit}; the last {dropped} were dropped by the "
                   "orchestrator",
            provider_key=result.provider_key)
        return result.model_copy(update={
            "companies": result.companies[:request.limit],
            "warnings": (*result.warnings, truncation)})

    def _pass_warnings(
        self, request: CompanyDiscoveryRequest,
        providers: Sequence[CompanyDiscoveryProvider],
    ) -> tuple[CompanyDiscoveryWarning, ...]:
        """What is true of the whole selection rather than of one provider's source.

        Two conditions, and this module is the only place that can see either:

        - **Nothing was selected.** §18's counterpart to `NO_SOURCE_SELECTED`. Not
          attributed to a provider, because there is none to blame.
        - **A country filter nobody honours.** Attributed to each provider that
          cannot restrict its output, which is where Phase 5's rule that a
          pass-level warning names no source does not carry over: there the warning
          was about the request, here it is about a named provider's declared
          inability, and a reader has to know which one returned too much.
        """
        if not providers:
            restricted = " within the caller's allow-list" if request.provider_keys \
                else ""
            where = f" for {request.country}" if request.country else ""
            return (CompanyDiscoveryWarning(
                code=CompanyDiscoveryWarningCode.NO_PROVIDER_SELECTED,
                detail=f"no company discovery provider is both registered and "
                       f"enabled{where}{restricted}"),)
        if request.country is None:
            return ()
        return tuple(
            CompanyDiscoveryWarning(
                code=CompanyDiscoveryWarningCode.COUNTRY_FILTER_NOT_SUPPORTED,
                detail=f"cannot restrict its output to {request.country} and "
                       "returned everything it knows",
                provider_key=provider.metadata.provider_key)
            for provider in providers
            if not provider.metadata.supports(
                CompanyDiscoveryCapability.COUNTRY_FILTER))
