"""What all three Phase 6 providers do identically, in one place.

Each provider differs only in *where its seeds come from*: a YAML file, the
opportunities table, an operator's list. Everything after that — enforce the limit,
warn when it truncates, stamp the health, never raise — is the same work, and
writing it three times is how the third copy ends up reporting `HEALTHY` for a pass
that read nothing.

So a provider here answers one question, `_seeds`, and this class does the rest:

**It never raises.** `discover` catches `Exception` around `_seeds` and turns it into
`UNAVAILABLE` health through `companies.failures`, which is the only module that
reads an exception (§26). A provider that raised would still be contained by the
orchestrator; containing it twice costs nothing and means a provider is safe to call
directly from a test or a CLI.

**It warns rather than shrinking silently.** A seed the provider had to skip is
`SEED_SKIPPED`, a source that was empty is `NOTHING_CONFIGURED`, a list longer than
`limit` is `LIMIT_TRUNCATED`. §18's counterpart at the provider level: a result that
is narrower than the question has to say why.

**It writes nothing.** No session, no repository, no clock beyond the injected one.
"""
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from time import perf_counter

from backend.app.companies import failures
from backend.app.companies.contracts import (
    CompanyDiscoveryRequest,
    CompanyDiscoveryResult,
    CompanyDiscoveryWarning,
    CompanyDiscoveryWarningCode,
    CompanyProviderMetadata,
    DiscoveredCompany,
    ProviderHealth,
)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


class SeedBatch:
    """What one provider's source yielded: the companies, and what it had to skip.

    A pair rather than two returns because the skips are only meaningful next to the
    companies — "two employers, one line unreadable" is a report, "one line
    unreadable" alone is a mystery. `empty_detail` is set when the source was
    *legitimately* empty, which is the `NOTHING_CONFIGURED` case a fresh deployment
    hits and which must not look like a failure (§28).
    """

    __slots__ = ("companies", "empty_detail", "skipped")

    def __init__(self, companies: Sequence[DiscoveredCompany], *,
                 skipped: Sequence[str] = (),
                 empty_detail: str | None = None) -> None:
        self.companies = tuple(companies)
        self.skipped = tuple(skipped)
        self.empty_detail = empty_detail


class LocalCompanyProvider:
    """A `CompanyDiscoveryProvider` over a bounded local source.

    Not a `Protocol` implementation by inheritance — `CompanyDiscoveryProvider` is a
    structural Protocol and nothing here subclasses it. This is a plain base class
    whose subclasses happen to satisfy it, which `tests` assert with `isinstance`.
    """

    def __init__(self, *, metadata: CompanyProviderMetadata,
                 clock: Clock = utc_now) -> None:
        self._metadata = metadata
        self._clock = clock

    @property
    def metadata(self) -> CompanyProviderMetadata:
        return self._metadata

    # -- what a subclass provides -------------------------------------------
    def _seeds(self, request: CompanyDiscoveryRequest) -> SeedBatch:
        """Every company this provider's source names, before `limit` is applied.

        Synchronous on purpose: all three Phase 6 sources are a file read or a list
        already in memory, and `async def` on a function that never awaits would
        suggest a request that is not being made. The one provider that does await
        — `stored_opportunities`, which reads a repository — overrides `_discover`
        instead, and its docstring says why.
        """
        raise NotImplementedError

    # -- the contract --------------------------------------------------------
    async def discover(self,
                       request: CompanyDiscoveryRequest) -> CompanyDiscoveryResult:
        """Read the source, hold it to `limit`, report. Never raises."""
        started = perf_counter()
        try:
            batch = await self._collect(request)
        except Exception as exc:
            return CompanyDiscoveryResult(
                provider_key=self._metadata.provider_key,
                health=failures.health_from_exception(
                    exc, self._metadata, checked_at=self._clock(),
                    latency_ms=_elapsed_ms(started)),
                completed_at=self._clock())
        return self._result(batch, request, latency_ms=_elapsed_ms(started))

    async def _collect(self, request: CompanyDiscoveryRequest) -> SeedBatch:
        """The seam `stored_opportunities` overrides to await its repository."""
        return self._seeds(request)

    async def healthcheck(self) -> ProviderHealth:
        """Whether the source can be read at all, without discovering anything.

        Reading a local file or an in-memory list *is* the cheap probe, so this runs
        the same collection with a default request and reports whether it worked.
        `HEALTHY` here means "the source is readable", not "it contains employers":
        an empty `config/companies.yaml` is a healthy provider with nothing to say,
        and conflating the two would make a fresh deployment look broken (§28).
        """
        started = perf_counter()
        try:
            await self._collect(CompanyDiscoveryRequest())
        except Exception as exc:
            return failures.health_from_exception(
                exc, self._metadata, checked_at=self._clock(),
                latency_ms=_elapsed_ms(started))
        return failures.healthy(self._metadata, checked_at=self._clock(),
                                latency_ms=_elapsed_ms(started))

    def _result(self, batch: SeedBatch, request: CompanyDiscoveryRequest, *,
                latency_ms: int) -> CompanyDiscoveryResult:
        """One batch, turned into the result the orchestrator records.

        `DEGRADED` when something was skipped, because a provider that read four
        lines out of five answered partially and §26 asks that be visible; `HEALTHY`
        otherwise, including when the source was empty.
        """
        warnings: list[CompanyDiscoveryWarning] = []
        companies = batch.companies
        if len(companies) > request.limit:
            warnings.append(self._warn(
                CompanyDiscoveryWarningCode.LIMIT_TRUNCATED,
                f"{len(companies)} companies are available and the request allowed "
                f"{request.limit}; the rest were not returned"))
            companies = companies[:request.limit]
        for detail in batch.skipped:
            warnings.append(self._warn(CompanyDiscoveryWarningCode.SEED_SKIPPED,
                                       detail))
        if not companies and batch.empty_detail is not None and not batch.skipped:
            warnings.append(self._warn(CompanyDiscoveryWarningCode.NOTHING_CONFIGURED,
                                       batch.empty_detail))

        checked_at = self._clock()
        health = (
            failures.degraded(
                self._metadata, checked_at=checked_at, latency_ms=latency_ms,
                detail=f"{len(batch.skipped)} entries of this provider's source could "
                       "not be read and were skipped")
            if batch.skipped else
            failures.healthy(self._metadata, checked_at=checked_at,
                             latency_ms=latency_ms))
        return CompanyDiscoveryResult(
            provider_key=self._metadata.provider_key, companies=companies,
            warnings=tuple(warnings), health=health, completed_at=checked_at)

    def _warn(self, code: CompanyDiscoveryWarningCode,
              detail: str) -> CompanyDiscoveryWarning:
        """A warning this provider owns, attributed to it.

        Always attributed, unlike a sweep-level warning: every code emitted here is
        a fact about *this* provider's source, and a reader has to know which file
        to go and fix.
        """
        return CompanyDiscoveryWarning(code=code, detail=detail,
                                       provider_key=self._metadata.provider_key)
