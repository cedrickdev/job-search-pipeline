"""The typed contracts every source plugin and every discovery run speaks.

`OpportunitySource` at the bottom is the whole point of Phase 5: the orchestrator
holds a tuple of these and nothing else. It cannot tell an HTTP JSON API from an
HTML listing from an Algolia index, and it must not be able to
(docs/ARCHITECTURE.md §7).

Everything here is a frozen `DomainModel` except the Protocol itself, so a result
can be handed between the orchestrator, a report and a repository without
defensive copying.
"""
from enum import StrEnum
from typing import Annotated, Protocol, Self, runtime_checkable

from pydantic import Field, model_validator

from backend.app.discovery.capabilities import SourceCapability
from backend.app.domain.base import (
    CountryCode,
    DomainModel,
    HttpUrlStr,
    NonEmptyStr,
    UtcDatetime,
)
from backend.app.domain.common import GeoPoint
from backend.app.domain.opportunity import Opportunity, OpportunityType, WorkplaceMode

# A source key is an identity, so it is constrained rather than free text: lower
# snake case, and stable for the life of the source. §5 of the phase order is
# explicit that identity must not be derived from a class name — a rename would
# silently orphan every `OpportunitySourceRecord` already stored under the old
# one, and `OpportunityRepository.get_by_source` would stop finding them.
SourceKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)]

# An environment variable NAME. The pattern is the point: it makes it impossible
# to put a credential *value* in source metadata, because a real API key does not
# look like this (docs/ENGINEERING_STANDARDS.md §Security).
EnvVarName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)]


class SourceType(StrEnum):
    """What kind of thing the source is — descriptive, never dispatched on.

    A report groups by it and an operator reads it. The moment the orchestrator
    branched on this value, the abstraction the phase asks for would be gone.
    """

    ATS_BOARD = "ATS_BOARD"
    JOB_BOARD = "JOB_BOARD"
    AGGREGATOR = "AGGREGATOR"
    SEARCH_INDEX = "SEARCH_INDEX"
    COMPANY_CAREER_SITE = "COMPANY_CAREER_SITE"
    STAFFING_AGENCY = "STAFFING_AGENCY"


class RateLimitMetadata(DomainModel):
    """What the source tolerates, declared rather than discovered the hard way.

    Advisory in Phase 5 — nothing here throttles yet — but it is metadata, not
    behaviour, and it belongs beside the source that knows it (§5).
    """

    requests_per_minute: Annotated[int, Field(gt=0)] | None = None
    min_interval_seconds: Annotated[float, Field(ge=0.0)] | None = None
    notes: NonEmptyStr | None = None


class SourceMetadata(DomainModel):
    """Who a source is and what it can do (§5).

    `countries` empty means country-agnostic, following the repo-wide convention
    that an empty collection restricts nothing (`SearchProfile` states it). A
    Greenhouse board is wherever the company is, and LinkedIn takes the country
    as a parameter; neither is Swiss, so neither names a country. `indeed_ch` is
    `("CH",)` because its host is `ch-fr.indeed.com` and it cannot answer for
    anywhere else.

    `enabled` here is the *source's own* switch — "this implementation is fit to
    run at all". Whether a given country uses it is a Country Pack's
    `SourceBinding.enabled`, and the registry needs both to say yes.

    No secret can live in this model: `credential_env_vars` holds variable NAMES,
    constrained to a shape a real credential does not have.
    """

    source_key: SourceKey
    display_name: NonEmptyStr
    source_type: SourceType
    countries: tuple[CountryCode, ...] = ()
    capabilities: frozenset[SourceCapability] = frozenset()
    # Capabilities the source accepts but honours only as a ranking hint. Declared
    # separately instead of being left out, because leaving `LOCATION_SEARCH` out
    # of Welcome to the Jungle would stop the adapter passing the location at all
    # and change V1's results.
    advisory_capabilities: frozenset[SourceCapability] = frozenset()
    enabled: bool = True
    # Lower runs first. Ties break on `source_key`, so ordering is total.
    priority: Annotated[int, Field(ge=0)] = 100
    rate_limit: RateLimitMetadata | None = None
    requires_credentials: bool = False
    credential_env_vars: tuple[EnvVarName, ...] = ()
    documentation_url: HttpUrlStr | None = None
    notes: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _claims_are_coherent(self) -> Self:
        unclaimed = self.advisory_capabilities - self.capabilities
        if unclaimed:
            raise ValueError(
                "advisory_capabilities must be a subset of capabilities; "
                f"{sorted(unclaimed)} is advertised as advisory but not claimed")
        if self.requires_credentials and not self.credential_env_vars:
            raise ValueError("a source that requires credentials must name the "
                             "environment variables that carry them")
        if len(set(self.countries)) != len(self.countries):
            raise ValueError("countries must not repeat a country")
        return self

    def supports(self, capability: SourceCapability) -> bool:
        return capability in self.capabilities

    def supports_reliably(self, capability: SourceCapability) -> bool:
        """Claimed, and not merely as a ranking hint."""
        return (capability in self.capabilities
                and capability not in self.advisory_capabilities)

    def serves(self, country: CountryCode) -> bool:
        """Whether this source can answer for a country.

        No countries declared means no restriction, not "serves nowhere".
        """
        return not self.countries or country in self.countries

    @property
    def sole_country(self) -> CountryCode | None:
        """The country a posting from this source is in, when that is knowable.

        A source that declares exactly one country returns postings in it —
        `ch-fr.indeed.com` cannot hand back a German vacancy, and jobs.migros.ch
        publishes one Swiss cooperative's openings. That is the only case where
        the normalizer may fill `Location.country`; for LinkedIn or a Greenhouse
        board the country is genuinely unknown until Phase 7 geocodes it, and
        guessing "CH" because the search was Swiss would invent a fact.
        """
        return self.countries[0] if len(self.countries) == 1 else None


class RadiusConstraint(DomainModel):
    """"Within `radius_km` of `center`", as a source-facing request.

    Carried separately from `locations` because it is the one part of a request
    no current source can honour: `SourceCapability.RADIUS_SEARCH` has no holder,
    so the orchestrator has to have an explicit, tested policy for it rather than
    passing a number nobody reads (§11).
    """

    center: GeoPoint
    radius_km: Annotated[float, Field(gt=0.0, le=500.0)]
    label: NonEmptyStr | None = None


class DiscoveryRequest(DomainModel):
    """One country's worth of "go and look", in source-neutral terms (§6).

    `keywords` and `locations` are tuples rather than a single value each because
    a sweep is inherently plural, and because the alternative — the orchestrator
    looping and calling `discover()` per pair — would put V1's query × location
    shape *back* into the core. A source that needs that cross product does it
    inside its own adapter, sequentially, at its own pace.

    `source_keys` is the profile's allow-list, carried on the request so the
    orchestrator can honour it without knowing what a `SearchProfile` is; empty
    means no restriction, as everywhere else.

    `cursor` is an opaque token a source minted and only that source parses. No
    source-specific pagination object ever reaches orchestration (§6).
    """

    country: CountryCode
    keywords: tuple[NonEmptyStr, ...] = ()
    locations: tuple[NonEmptyStr, ...] = ()
    radius: RadiusConstraint | None = None
    opportunity_types: tuple[OpportunityType, ...] = ()
    workplace_modes: tuple[WorkplaceMode, ...] = ()
    # A preference, not a filter: it tells the orchestrator whether remote-first
    # sources are worth querying. `SearchProfile.includes_remote` is where it
    # comes from and says why.
    include_remote: bool = True
    remote_only: bool = False
    # V1's window, and its meaning: how far back a source should look. Only
    # sources claiming INCREMENTAL_DISCOVERY can use it.
    lookback_days: Annotated[int, Field(ge=1, le=90)] = 3
    # Per source, per request — not a total. A cap on one board's answer must not
    # depend on how many other boards ran first.
    limit: Annotated[int, Field(gt=0, le=1000)] = 100
    source_keys: tuple[SourceKey, ...] = ()
    cursor: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _preferences_are_coherent(self) -> Self:
        if self.remote_only and not self.include_remote:
            raise ValueError("a remote-only request cannot exclude remote work")
        return self

    def allows_source(self, source_key: str) -> bool:
        return not self.source_keys or source_key in self.source_keys


class SourceHealthStatus(StrEnum):
    """The four normalized answers to "is this source working?" (§12).

    V1 reports one boolean per source (`{"ok": false}`), which cannot distinguish
    the three failures an operator would act on differently: a board that is down
    (wait), a board that returned some of its pages (results are incomplete), and
    a board that was never configured (set the variable). `MISCONFIGURED` is the
    one that saves the most time — `jooble` without `JOOBLE_API_KEY` is not an
    outage, and V1 reports it as one.
    """

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    MISCONFIGURED = "MISCONFIGURED"


class SourceFailureCode(StrEnum):
    """Why a source did not answer, as a code a dashboard can group by.

    `SOURCE_UNAVAILABLE` and `SOURCE_RATE_LIMITED` are the two
    docs/ENGINEERING_STANDARDS.md §Observability names; the rest split what would
    otherwise be lumped into the first and lose the distinction that matters.
    """

    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_RATE_LIMITED = "SOURCE_RATE_LIMITED"
    SOURCE_FORBIDDEN = "SOURCE_FORBIDDEN"
    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
    SOURCE_MISCONFIGURED = "SOURCE_MISCONFIGURED"
    SOURCE_PARSE_FAILED = "SOURCE_PARSE_FAILED"
    SOURCE_PARTIAL_FAILURE = "SOURCE_PARTIAL_FAILURE"
    # The catch-all for a bug in an adapter rather than a fault at the source.
    SOURCE_ADAPTER_ERROR = "SOURCE_ADAPTER_ERROR"


class SourceHealth(DomainModel):
    """One source's condition at one instant (§12).

    `detail` is deliberately not "the error message". Nothing in this package
    puts `str(exc)` in it: V1's `FetchError` embeds the URL it failed on, and
    `pipeline/sources/jooble.py` fetches `https://jooble.org/api/{key}` — so the
    obvious implementation writes an API key into a health report, a run summary
    and, eventually, a dashboard. `failures.classify_failure` builds this field
    from a fixed vocabulary instead, and `failures.redact_secrets` is a second
    pass over whatever it built.
    """

    source_key: SourceKey
    status: SourceHealthStatus
    checked_at: UtcDatetime
    latency_ms: Annotated[int, Field(ge=0)] | None = None
    reason: SourceFailureCode | None = None
    detail: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _a_failure_says_why(self) -> Self:
        if self.status is not SourceHealthStatus.HEALTHY and self.reason is None:
            raise ValueError("a source that is not HEALTHY must carry a reason code")
        if self.status is SourceHealthStatus.HEALTHY and self.reason is not None:
            raise ValueError("a HEALTHY source must not carry a failure reason")
        return self

    @property
    def is_usable(self) -> bool:
        """Whether this source produced anything worth reading."""
        return self.status in (SourceHealthStatus.HEALTHY,
                               SourceHealthStatus.DEGRADED)


class DiscoveryWarningCode(StrEnum):
    """Why an answer is not quite the question that was asked.

    This vocabulary exists because of §11 and because of what the V1 inspection
    found. `pipeline/sources/migros.py` takes `query` and `location` and fetches
    one hardcoded Vaud listing; `welcometothejungle.py` folds the location into a
    free-text Algolia query; only `linkedin` and `indeed_ch` can honour
    `lookback_days`. V1 has nowhere to say any of that, so a caller reading its
    results cannot tell a filtered result set from an unfiltered one. Every case
    below is a fact the orchestrator or an adapter *knows* at the moment it
    happens, and a warning is how it survives to the caller.

    A warning is never a failure: a source that warns still returns postings and
    is still `HEALTHY`, unless something else went wrong.
    """

    # The request asked for something the source does not claim at all.
    CAPABILITY_NOT_SUPPORTED = "CAPABILITY_NOT_SUPPORTED"
    # The source accepts the parameter but only ranks by it (`advisory_capabilities`).
    CAPABILITY_ADVISORY_ONLY = "CAPABILITY_ADVISORY_ONLY"
    # §11, named separately because it is the one the phase order calls out: a
    # profile asked for "within N km" and no selected source does native radius.
    RADIUS_NOT_SUPPORTED = "RADIUS_NOT_SUPPORTED"
    # The adapter received the value and discards it — the three hardcoded-listing
    # Swiss sources.
    KEYWORD_IGNORED = "KEYWORD_IGNORED"
    LOCATION_IGNORED = "LOCATION_IGNORED"
    LOOKBACK_IGNORED = "LOOKBACK_IGNORED"
    CURSOR_IGNORED = "CURSOR_IGNORED"
    # More postings were available than `limit` allowed through.
    LIMIT_TRUNCATED = "LIMIT_TRUNCATED"
    # Some of the source's own sub-fetches failed; what came back is incomplete.
    PARTIAL_RESULTS = "PARTIAL_RESULTS"
    # One posting could not be normalized (no title, no company, no URL). Dropping
    # it silently is how a source appears to shrink for no reason.
    POSTING_SKIPPED = "POSTING_SKIPPED"
    # The registry matched nothing for this request. §18 requires this to be an
    # explicit result rather than an empty list that looks like "nothing is hiring".
    NO_SOURCE_SELECTED = "NO_SOURCE_SELECTED"


class DiscoveryWarning(DomainModel):
    """One reason a result is narrower, broader or shorter than the request.

    `detail` is written for an operator and, like `SourceHealth.detail`, never
    carries an exception string — the same secret-leak argument applies, and the
    same `failures.redact_secrets` pass covers it.
    """

    code: DiscoveryWarningCode
    detail: NonEmptyStr
    # Absent for a sweep-level warning: `NO_SOURCE_SELECTED` and the §11 radius
    # warning belong to the request, not to any one source.
    source_key: SourceKey | None = None
    capability: SourceCapability | None = None


class DiscoveryMetrics(DomainModel):
    """What one source's turn actually cost and produced (§6).

    `requests_made` is the number that makes a behaviour change visible: V1
    fetches `migros` sixteen times per run (8 queries × 2 locations) for one
    hardcoded page, and the V2 adapter fetches it once. Without a counter, the
    only way to see that is to watch the network.

    `duration_ms` is per source, and `merge` deliberately does not add them up: a
    concurrent sweep's wall clock is not the sum of its parts, and a report that
    claimed otherwise would overstate every run. The orchestrator measures the
    sweep itself, so the merged value is `None` rather than a fabricated total.
    """

    requests_made: Annotated[int, Field(ge=0)] = 0
    postings_seen: Annotated[int, Field(ge=0)] = 0
    opportunities_returned: Annotated[int, Field(ge=0)] = 0
    postings_skipped: Annotated[int, Field(ge=0)] = 0
    duration_ms: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _counts_add_up(self) -> Self:
        accounted = self.opportunities_returned + self.postings_skipped
        if accounted > self.postings_seen:
            raise ValueError(
                f"postings_seen ({self.postings_seen}) cannot be smaller than "
                f"returned + skipped ({accounted})")
        return self

    def merge(self, other: "DiscoveryMetrics") -> "DiscoveryMetrics":
        """Sum the counters of two sources' turns. See the note on `duration_ms`."""
        return DiscoveryMetrics(
            requests_made=self.requests_made + other.requests_made,
            postings_seen=self.postings_seen + other.postings_seen,
            opportunities_returned=(self.opportunities_returned
                                    + other.opportunities_returned),
            postings_skipped=self.postings_skipped + other.postings_skipped,
        )


class DiscoveryResult(DomainModel):
    """Everything one source has to say about one request (§6).

    Four things travel together on purpose. The postings are the answer; the
    `health` is whether the answer is complete; the `warnings` are how it differs
    from the request; and `cursor` is where to resume. Splitting health out into a
    second call is what V1 effectively does, and it means a caller that wants both
    has to fetch twice or guess.

    `cursor` is opaque: a string the source minted and only that source parses.
    §6 forbids a source-specific pagination object reaching orchestration, so the
    type here is a string and the orchestrator never looks inside it.

    There is no separate `source_key` field — `health.source_key` is the one place
    it is written, so a result cannot disagree with its own health report.
    """

    health: SourceHealth
    opportunities: tuple[Opportunity, ...] = ()
    cursor: NonEmptyStr | None = None
    warnings: tuple[DiscoveryWarning, ...] = ()
    metrics: DiscoveryMetrics = DiscoveryMetrics()

    @model_validator(mode="after")
    def _payload_matches_its_own_report(self) -> Self:
        if self.metrics.opportunities_returned != len(self.opportunities):
            raise ValueError(
                "metrics.opportunities_returned "
                f"({self.metrics.opportunities_returned}) must equal the number of "
                f"opportunities carried ({len(self.opportunities)})")
        if not self.health.is_usable and self.opportunities:
            raise ValueError(
                f"a {self.health.status} source must not also return postings; "
                "DEGRADED is the status for an incomplete answer")
        foreign = {w.source_key for w in self.warnings
                   if w.source_key is not None and w.source_key != self.source_key}
        if foreign:
            raise ValueError(
                f"warnings must not be attributed to another source: {sorted(foreign)}")
        if self.cursor is not None and not self.health.is_usable:
            raise ValueError("a source that failed must not hand back a resume cursor")
        return self

    @property
    def source_key(self) -> str:
        return self.health.source_key

    @property
    def is_usable(self) -> bool:
        return self.health.is_usable


@runtime_checkable
class OpportunitySource(Protocol):
    """The only thing the orchestrator knows about a source (§3).

    Three members, and none of them mentions HTTP, HTML, Algolia, an ATS or a
    browser. `pipeline/sources/greenhouse.py` reads a JSON API,
    `pipeline/sources/coop.py` parses HTML with BeautifulSoup and
    `pipeline/sources/welcometothejungle.py` posts to an Algolia index; behind this
    Protocol they are indistinguishable, which is the property
    docs/ARCHITECTURE.md §7 asks for and `tests/test_v2_discovery_boundaries.py`
    enforces.

    `metadata` is a read-only property rather than an attribute so that both
    shapes satisfy it: an adapter storing a plain `self.metadata` attribute
    type-checks against a property in a Protocol, but the reverse is not true.

    `discover` returns rather than raises. An adapter that lets an exception out is
    a bug, and the orchestrator still contains it (§14) — but the contract is that
    a source reports its own failure as a `DiscoveryResult` carrying a non-HEALTHY
    `SourceHealth`, because that is the only form that can also carry the postings
    it did manage to collect.

    `healthcheck` is the cheap probe, for sources claiming
    `SourceCapability.HEALTHCHECK`. A source without that capability still has to
    implement the method — it answers from what it knows rather than reaching the
    network, and says so.

    Note for tests: this Protocol has a non-method member, so `isinstance` works
    and `issubclass` raises `TypeError`. Assert with `isinstance`.
    """

    @property
    def metadata(self) -> SourceMetadata: ...

    async def discover(self, request: DiscoveryRequest) -> DiscoveryResult: ...

    async def healthcheck(self) -> SourceHealth: ...







