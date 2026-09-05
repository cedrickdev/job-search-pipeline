"""V1's thirteen source modules, wrapped — not rewritten (§7).

Every adapter in this package delegates to the V1 function that already works.
`pipeline/sources/jobup.py` still parses jobup's `__INIT__` blob, `wtj.py` still
talks to Algolia, and neither file changes. What this module adds is the four things
V1 has no place to put:

**Async, without touching V1.** The V1 sources are synchronous `requests` code, so
each call runs in a worker thread (`asyncio.to_thread`). The calls a single source
needs stay *sequential inside that thread*: two boards can be swept at once, but
one board is never hit twice concurrently — V1's politeness, preserved by
construction rather than by comment.

**Requests only where they buy something.** V1 calls every source once per
query × location. `migros`, `jobscout24` and `manpower` ignore both arguments and
return the same hardcoded listing every time, so V1 fetches the same page nine
times. Here the cross product is built from the dimensions the source actually
claims, which is fewer requests for identical results — and the `KEYWORD_IGNORED` /
`LOCATION_IGNORED` warnings say so out loud.

**A typed answer instead of an exception.** `discover` returns a `DiscoveryResult`
whose `health` is the failure. One source going down cannot end another's sweep
(§14), and `failures.py` is what keeps a Jooble URL out of the report (§12).

**Provenance (§15).** Every posting keeps its source key, its URL, the instant it
was fetched and its untyped fields, because `normalization.opportunity_from_posting`
puts them there.
"""
import asyncio
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Final, Protocol

import yaml

from backend.app.discovery import failures
from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.contracts import (
    DiscoveryMetrics,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoveryWarning,
    DiscoveryWarningCode,
    SourceHealth,
    SourceMetadata,
)
from backend.app.discovery.normalization import (
    PostingRejected,
    opportunity_from_posting,
)
from backend.app.domain.base import CountryCode
from backend.app.domain.opportunity import Opportunity
from country_packs.contracts import CountryPack
from pipeline import paths
from pipeline.http_fetch import FetchError

Clock = Callable[[], datetime]
PackResolver = Callable[[CountryCode], CountryPack]
Posting = Mapping[str, Any]

# One source, one sweep, at most this many requests. V1 has no such bound because
# its cross product came from a fixed 3 × 3 config file; a saved `SearchProfile`
# has no such ceiling, and 5 keywords × 4 locations × 12 sources is 240 requests
# nobody asked for. Truncation is deterministic (the plan is ordered) and reported
# as `PARTIAL_RESULTS` rather than silently applied.
MAX_REQUESTS_PER_SWEEP: Final = 24

# A healthcheck asks for the smallest window a V1 source accepts, because the
# question is "does this endpoint answer?" and not "what is on it".
PROBE_LOOKBACK_DAYS: Final = 1


def utc_now() -> datetime:
    """The adapters' clock, injectable so a test can pin an instant."""
    return datetime.now(UTC)


class V1SearchCallable(Protocol):
    """`pipeline/sources/*.py:search_jobs`, as all ten query modules define it."""

    def __call__(self, query: str, location: str,
                 lookback_days: int = ...) -> list[dict[str, Any]]: ...


class V1FetchCallable(Protocol):
    """`pipeline/sources/{greenhouse,lever,ashby}.py:fetch_jobs`."""

    def __call__(self, token: str, company: str) -> list[dict[str, Any]]: ...


class CompanyBoard(Protocol):
    """One entry of `config/companies.yaml`: a board token and a display name."""

    @property
    def token(self) -> str: ...

    @property
    def company(self) -> str: ...


class _Board:
    """A company board entry, as read from V1's own configuration file."""

    __slots__ = ("_company", "_token")

    def __init__(self, token: str, company: str) -> None:
        self._token = token
        self._company = company

    @property
    def token(self) -> str:
        return self._token

    @property
    def company(self) -> str:
        return self._company


def load_v1_companies(path: object = None) -> Mapping[str, tuple[CompanyBoard, ...]]:
    """`config/companies.yaml`, keyed by ATS name.

    Reads V1's file at V1's path (`pipeline.paths.COMPANIES_PATH`) rather than
    introducing a second list, because that file is the operator's and duplicating
    it is how the two drift. The file ships with `greenhouse: []`, `lever: []` and
    `ashby: []`, so the three ATS adapters make no request and return nothing until
    an operator adds an employer — which is exactly V1's behaviour today.

    A malformed entry is skipped rather than fatal: one bad line in an operator's
    list must not stop a sweep of eleven other sources.
    """
    source = paths.COMPANIES_PATH if path is None else path
    try:
        with open(source, encoding="utf-8") as handle:  # type: ignore[call-overload]
            payload = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        return {}
    if not isinstance(payload, Mapping):
        return {}

    boards: dict[str, tuple[CompanyBoard, ...]] = {}
    for ats, entries in payload.items():
        if not isinstance(entries, Sequence) or isinstance(entries, str | bytes):
            continue
        usable: list[CompanyBoard] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            token, company = entry.get("token"), entry.get("company")
            if isinstance(token, str) and token and isinstance(company, str) \
                    and company:
                usable.append(_Board(token, company))
        boards[str(ats)] = tuple(usable)
    return boards


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


class V1SourceAdapter:
    """The `OpportunitySource` half of a wrapped V1 module.

    Everything except *which calls to make* lives here; a subclass answers that
    question with `_plan`, and the orchestrator cannot tell the two apart — which is
    §3's requirement that a source's transport stay invisible to the core.
    """

    def __init__(self, *, metadata: SourceMetadata, packs: PackResolver,
                 clock: Clock = utc_now) -> None:
        self._metadata = metadata
        self._packs = packs
        self._clock = clock

    @property
    def metadata(self) -> SourceMetadata:
        return self._metadata

    # -- what a subclass provides -------------------------------------------
    def _plan(self, request: DiscoveryRequest) -> tuple[Callable[[], list[
            dict[str, Any]]], ...]:
        """The calls this request needs, in a deterministic order."""
        raise NotImplementedError

    def _probe(self) -> Callable[[], object] | None:
        """The cheapest call that proves the source answers, or `None`."""
        return None

    # -- the contract --------------------------------------------------------
    async def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        """Sweep this source. Never raises: a failure comes back as `health`."""
        warnings = list(self._input_warnings(request))
        missing = self._missing_credentials()
        if missing:
            # No request at all: V1 finds out inside `search_jobs` and spends a
            # round trip discovering its own configuration gap.
            return DiscoveryResult(
                health=failures.misconfigured(self._metadata,
                                              checked_at=self._clock(),
                                              missing=missing),
                warnings=tuple(warnings))

        started = perf_counter()
        try:
            pack = self._packs(request.country)
            plan = self._plan(request)
        except Exception as exc:
            return DiscoveryResult(
                health=failures.health_from_exception(
                    exc, self._metadata, checked_at=self._clock(),
                    latency_ms=_elapsed_ms(started)),
                warnings=tuple(warnings))

        if len(plan) > MAX_REQUESTS_PER_SWEEP:
            warnings.append(self._warn(
                DiscoveryWarningCode.PARTIAL_RESULTS,
                f"{len(plan)} requests were planned; the first "
                f"{MAX_REQUESTS_PER_SWEEP} were made"))
            plan = plan[:MAX_REQUESTS_PER_SWEEP]

        postings, errors = await asyncio.to_thread(self._run, plan)
        latency_ms = _elapsed_ms(started)
        observed_at = self._clock()
        return self._result(request, pack, postings, errors, warnings,
                            observed_at=observed_at, latency_ms=latency_ms,
                            planned=len(plan))

    async def healthcheck(self) -> SourceHealth:
        """Ask the source whether it is there — at most one request.

        Three answers, and the third is the one to read carefully:

        - a declared credential that is not set → `MISCONFIGURED`, no request;
        - `HEALTHCHECK` claimed → one probe, and its outcome;
        - `HEALTHCHECK` not claimed → `HEALTHY`, meaning *nothing is known to be
          wrong*, because no request was made. `linkedin` is the deliberate case:
          probing a hostile endpoint would spend an unsolicited request to learn
          what the next sweep reports anyway. A caller that needs the distinction
          reads `metadata.supports(HEALTHCHECK)`, and a dashboard should render
          "not probed" rather than a green light.
        """
        missing = self._missing_credentials()
        if missing:
            return failures.misconfigured(self._metadata, checked_at=self._clock(),
                                          missing=missing)
        probe = (self._probe()
                 if self._metadata.supports(SourceCapability.HEALTHCHECK) else None)
        if probe is None:
            return failures.healthy(self._metadata, checked_at=self._clock())

        started = perf_counter()
        try:
            await asyncio.to_thread(self._guarded, probe)
        except Exception as exc:
            return failures.health_from_exception(
                exc, self._metadata, checked_at=self._clock(),
                latency_ms=_elapsed_ms(started))
        return failures.healthy(self._metadata, checked_at=self._clock(),
                                latency_ms=_elapsed_ms(started))

    # -- internals -----------------------------------------------------------
    def _missing_credentials(self) -> tuple[str, ...]:
        """Declared variables that are absent or blank, checked before any request."""
        if not self._metadata.requires_credentials:
            return ()
        return tuple(name for name in self._metadata.credential_env_vars
                     if not os.environ.get(name, "").strip())

    @staticmethod
    def _guarded(call: Callable[[], Any]) -> Any:
        """Run a V1 call, translating its exception into a V2 one.

        `FetchError` carries the URL it failed on and V1's jooble URL carries the
        API key, so the message crosses this boundary exactly once — into
        `SourceFetchError`, which `failures.classify_failure` reads and never
        copies out.
        """
        try:
            return call()
        except FetchError as exc:
            raise failures.SourceFetchError(str(exc)) from exc

    def _run(self, plan: Sequence[Callable[[], list[dict[str, Any]]]]) -> tuple[
            list[Posting], list[Exception]]:
        """Every call in the plan, sequentially, in one worker thread.

        Sequential on purpose: these are twelve public boards, and the throughput
        worth having comes from sweeping *different* sources at once (the
        orchestrator's job), never from parallelising requests to one of them.

        One failed call does not end the plan. A source that answers four of six
        queries has partial results, which is a `DEGRADED` sweep with data in it —
        V1 records the error and moves on too, and this is that behaviour typed.
        """
        postings: list[Posting] = []
        errors: list[Exception] = []
        for call in plan:
            try:
                postings.extend(self._guarded(call))
            except Exception as exc:
                errors.append(exc)
        return postings, errors

    def _warn(self, code: DiscoveryWarningCode, detail: str,
              capability: SourceCapability | None = None) -> DiscoveryWarning:
        return DiscoveryWarning(code=code, detail=detail,
                                source_key=self._metadata.source_key,
                                capability=capability)

    def _input_warnings(self, request: DiscoveryRequest) -> Iterator[DiscoveryWarning]:
        """Say, once per sweep, which parts of the request this source cannot honour.

        This is the §11 requirement and the answer to the question V1 cannot
        answer: given a result set, was the filter applied or not? A caller that
        reads no warning knows the answer was filtered as asked.
        """
        meta = self._metadata
        for capability, asked, subject in (
            (SourceCapability.KEYWORD_SEARCH, bool(request.keywords), "keywords"),
            (SourceCapability.LOCATION_SEARCH, bool(request.locations), "locations"),
        ):
            if not asked:
                continue
            ignored = (DiscoveryWarningCode.KEYWORD_IGNORED
                       if capability is SourceCapability.KEYWORD_SEARCH
                       else DiscoveryWarningCode.LOCATION_IGNORED)
            if not meta.supports(capability):
                yield self._warn(ignored, f"this source accepts no {subject}; its "
                                          "own listing is returned as published",
                                 capability)
            elif not meta.supports_reliably(capability):
                yield self._warn(DiscoveryWarningCode.CAPABILITY_ADVISORY_ONLY,
                                 f"{subject} affect ranking rather than the result "
                                 "set", capability)

        if request.radius is not None \
                and not meta.supports(SourceCapability.RADIUS_SEARCH):
            yield self._warn(DiscoveryWarningCode.RADIUS_NOT_SUPPORTED,
                             f"{request.radius.radius_km:g} km around the requested "
                             "centre was not applied; a geographic stage must filter "
                             "these results (Phase 7)",
                             SourceCapability.RADIUS_SEARCH)

        if not meta.supports(SourceCapability.INCREMENTAL_DISCOVERY):
            yield self._warn(DiscoveryWarningCode.LOOKBACK_IGNORED,
                             f"lookback_days={request.lookback_days} was not applied; "
                             "this source returns whatever it currently lists",
                             SourceCapability.INCREMENTAL_DISCOVERY)

        if request.cursor is not None \
                and not meta.supports(SourceCapability.PAGINATION):
            yield self._warn(DiscoveryWarningCode.CURSOR_IGNORED,
                             "this source has no pagination; the cursor was ignored "
                             "and page one returned", SourceCapability.PAGINATION)

        for capability, asked, subject in (
            (SourceCapability.OPPORTUNITY_TYPE_FILTER, bool(request.opportunity_types),
             "opportunity types"),
            (SourceCapability.REMOTE_FILTER,
             bool(request.workplace_modes) or request.remote_only, "workplace modes"),
        ):
            if asked and not meta.supports(capability):
                yield self._warn(DiscoveryWarningCode.CAPABILITY_NOT_SUPPORTED,
                                 f"{subject} must be filtered after normalization; "
                                 "this source cannot filter them", capability)

    def _normalize(self, postings: Sequence[Posting], *, pack: CountryPack,
                   fetched_at: datetime) -> tuple[tuple[Opportunity, ...], int]:
        """Postings → opportunities, deduplicated, with a count of what was dropped.

        Deduplication is by derived id, which is per source and per posting: the
        same vacancy legitimately comes back twice when two keywords match it, and
        counting it twice would inflate every metric downstream. The *first*
        sighting wins so the order stays the source's own.
        """
        kept: dict[Any, Opportunity] = {}
        skipped = 0
        for posting in postings:
            try:
                opportunity = opportunity_from_posting(
                    posting, metadata=self._metadata, pack=pack,
                    fetched_at=fetched_at)
            except PostingRejected:
                skipped += 1
                continue
            kept.setdefault(opportunity.id, opportunity)
        return tuple(kept.values()), skipped

    def _result(self, request: DiscoveryRequest, pack: CountryPack,
                postings: Sequence[Posting], errors: Sequence[Exception],
                warnings: list[DiscoveryWarning], *, observed_at: datetime,
                latency_ms: int, planned: int) -> DiscoveryResult:
        """Assemble the one object the orchestrator reads."""
        opportunities, skipped = self._normalize(postings, pack=pack,
                                                 fetched_at=observed_at)
        if skipped:
            warnings.append(self._warn(
                DiscoveryWarningCode.POSTING_SKIPPED,
                f"{skipped} of {len(postings)} postings had no company, title or "
                "URL and were dropped"))
        if len(opportunities) > request.limit:
            warnings.append(self._warn(
                DiscoveryWarningCode.LIMIT_TRUNCATED,
                f"{len(opportunities)} opportunities were found; the first "
                f"{request.limit} were kept"))
            opportunities = opportunities[:request.limit]

        if errors and len(errors) >= planned:
            # Every call failed: this is an outage, and the first exception is as
            # representative as the tenth.
            health = failures.health_from_exception(
                errors[0], self._metadata, checked_at=observed_at,
                latency_ms=latency_ms)
        elif errors:
            classification = failures.classify_failure(errors[0], self._metadata)
            health = failures.degraded(
                self._metadata, checked_at=observed_at, latency_ms=latency_ms,
                detail=f"{len(errors)} of {planned} requests failed: "
                       f"{classification.detail}")
        else:
            health = failures.healthy(self._metadata, checked_at=observed_at,
                                      latency_ms=latency_ms)

        if not health.is_usable:
            # The contract refuses postings under an unusable health, and rightly:
            # a caller must not have to guess whether a truncated list is complete.
            opportunities = ()

        return DiscoveryResult(
            health=health,
            opportunities=opportunities,
            warnings=tuple(warnings),
            metrics=DiscoveryMetrics(
                requests_made=planned,
                postings_seen=len(postings),
                opportunities_returned=len(opportunities),
                postings_skipped=skipped if health.is_usable else 0,
                duration_ms=latency_ms),
        )


class V1QuerySourceAdapter(V1SourceAdapter):
    """The ten `search_jobs(query, location, lookback_days)` modules.

    The one behavioural difference from V1 is the plan: V1 calls every source
    once per configured query × location, whatever the source does with them, so
    `migros` — which fetches one hardcoded Vaud listing and reads neither
    argument — is fetched once per pair. Here a dimension enters the cross
    product only if the metadata claims the matching capability, so a source that
    ignores keywords and locations is fetched exactly once and the sweep returns
    the same postings. `_input_warnings` is what tells the caller that happened.
    """

    def __init__(self, *, metadata: SourceMetadata, search: V1SearchCallable,
                 packs: PackResolver, clock: Clock = utc_now) -> None:
        super().__init__(metadata=metadata, packs=packs, clock=clock)
        self._search = search

    def _call(self, query: str, location: str,
              lookback_days: int) -> Callable[[], list[dict[str, Any]]]:
        """One V1 call, with its arguments bound now rather than at call time."""
        def call() -> list[dict[str, Any]]:
            return self._search(query, location, lookback_days=lookback_days)
        return call

    def _plan(self, request: DiscoveryRequest) -> tuple[Callable[[], list[
            dict[str, Any]]], ...]:
        """The cross product of the dimensions this source actually reads.

        `lookback_days` travels even to a source that ignores it, exactly as V1
        passes it: the argument is harmless and `LOOKBACK_IGNORED` is what says it
        had no effect. An empty dimension becomes one empty string — the repo
        convention is that an empty collection restricts nothing, and for a search
        box that means "whatever the board lists".
        """
        meta = self._metadata
        keywords = (request.keywords
                    if meta.supports(SourceCapability.KEYWORD_SEARCH) else ())
        locations = (request.locations
                     if meta.supports(SourceCapability.LOCATION_SEARCH) else ())
        return tuple(
            self._call(keyword, location, request.lookback_days)
            for keyword in (keywords or ("",))
            for location in (locations or ("",))
        )

    def _probe(self) -> Callable[[], object] | None:
        """One unfiltered search over the shortest window the source accepts."""
        return self._call("", "", PROBE_LOOKBACK_DAYS)


class V1AtsSourceAdapter(V1SourceAdapter):
    """The three `fetch_jobs(token, company)` ATS modules.

    An ATS board answers for one employer, so the plan is the operator's company
    list and not the request's keywords: `COMPANY_FILTER` is the capability these
    three have and the other ten do not. `config/companies.yaml` ships empty, so
    the default plan is empty, no request is made and the sweep returns nothing —
    V1's behaviour today, and the reason this adapter is silent rather than warning
    on every run about a list the operator has not written yet.

    The boards are read once, at composition time, and passed in: an adapter that
    re-read the file on every sweep would make a discovery run depend on the state
    of the filesystem mid-flight.
    """

    def __init__(self, *, metadata: SourceMetadata, fetch: V1FetchCallable,
                 boards: Sequence[CompanyBoard], packs: PackResolver,
                 clock: Clock = utc_now) -> None:
        super().__init__(metadata=metadata, packs=packs, clock=clock)
        self._fetch = fetch
        self._boards = tuple(boards)

    @property
    def boards(self) -> tuple[CompanyBoard, ...]:
        """The employers this board is asked about, for a status report."""
        return self._boards

    def _call(self, board: CompanyBoard) -> Callable[[], list[dict[str, Any]]]:
        def call() -> list[dict[str, Any]]:
            return self._fetch(board.token, board.company)
        return call

    def _plan(self, request: DiscoveryRequest) -> tuple[Callable[[], list[
            dict[str, Any]]], ...]:
        """One call per configured board, in the operator's own order."""
        return tuple(self._call(board) for board in self._boards)

    def _probe(self) -> Callable[[], object] | None:
        """The first board, or nothing to probe at all.

        With no board configured there is no URL to ask: the ATS is not down, it
        is unused, and `healthcheck` reporting HEALTHY without a request is the
        honest answer (see `V1SourceAdapter.healthcheck`).
        """
        return self._call(self._boards[0]) if self._boards else None
