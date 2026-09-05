# tests/test_v2_discovery_orchestration.py
"""One sweep, end to end: the mapping in, the isolation during, the report out.

Two halves. The first is `requests.py` — §10's "no second search-preference model",
which in practice is one function and one asymmetry worth remembering: several
places make several requests, because "within 20 km of Lausanne" and "within 20 km
of Geneva" are two searches and `DiscoveryRequest.radius` is singular.

The second is `orchestrator.py`, and the tests that matter most there are the ones
about what *does not* happen. A source that raises does not end the sweep (§14). A
country with no selectable source produces a named outcome rather than an empty list
that reads as "nobody is hiring" (§18). A radius nothing can honour is stated once,
at sweep level, instead of being silently dropped (§11). And no board appears by
name anywhere in this file's expectations except as fake metadata — if the
orchestrator ever needed to know which source it was talking to, these tests would
still pass and `test_v2_discovery_boundaries.py` would fail, which is the division of
labour between the two.

`FakeSource` and `ScriptedSource` make every case here run without a socket, a YAML
file or a V1 module, and the concurrency bound is observed by counting what is in
flight rather than by sleeping.
"""
import asyncio

import pytest

from backend.app.discovery.contracts import (
    DiscoveryRequest,
    DiscoveryResult,
    DiscoveryWarning,
    DiscoveryWarningCode,
    OpportunitySource,
    SourceFailureCode,
    SourceHealth,
    SourceHealthStatus,
    SourceMetadata,
)
from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.normalization import opportunity_from_posting
from backend.app.discovery.orchestrator import DiscoveryOrchestrator
from backend.app.discovery.registry import SourceRegistry
from backend.app.discovery.requests import (
    MAX_LOOKBACK_DAYS,
    countries_in_scope,
    discovery_requests_for,
)
from backend.app.domain.common import GeoPoint
from backend.app.domain.opportunity import Opportunity, OpportunityType, WorkplaceMode
from backend.app.domain.search import (
    CountrySearchArea,
    RadiusSearchArea,
    RemoteOnlySearchArea,
    SearchArea,
    SearchProfile,
)
from country_packs.contracts import SourceBinding
from country_packs.errors import CountryPackError, CountryPackErrorCode
from country_packs.registry import CountryPackRegistry
from tests.v2_discovery import (
    NOW,
    FakeSource,
    a_health,
    a_metadata,
    a_pack,
    a_result,
    a_search_profile,
    frozen_clock,
)

Cap = SourceCapability
Warn = DiscoveryWarningCode
PACK = a_pack("CH")
LAUSANNE = GeoPoint(latitude=46.5197, longitude=6.6323)


def an_opportunity(source_key: str = "jobup",
                   url: str = "https://example.ch/offre/1") -> Opportunity:
    """A real `Opportunity`, so provenance can be asserted rather than assumed."""
    return opportunity_from_posting(
        {"company": "Neocraft SA", "title": "Developpeur backend", "url": url},
        metadata=a_metadata(source_key), pack=PACK, fetched_at=NOW)


class ScriptedSource:
    """An `OpportunitySource` that answers a different result on each call.

    `FakeSource` covers the one-answer cases; a source asked twice — once per radius
    area — needs to be able to say HEALTHY and then time out, which is what
    `SweepReport.health` resolves and cannot be tested with a fixed answer.
    """

    def __init__(self, metadata: SourceMetadata,
                 *results: DiscoveryResult) -> None:
        self._metadata = metadata
        self._results = results
        self.requests: list[DiscoveryRequest] = []

    @property
    def metadata(self) -> SourceMetadata:
        return self._metadata

    async def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        self.requests.append(request)
        return self._results[min(len(self.requests), len(self._results)) - 1]

    async def healthcheck(self) -> SourceHealth:
        return a_health(self._metadata.source_key)


class Gate:
    """Counts how many sources are in flight at once, releasing at a threshold.

    How a concurrency bound is observed without a `sleep`: the first `expected`
    arrivals block, which is what makes the peak meaningful, and the event they set
    lets everyone through — so the test is deterministic and finishes as fast as the
    event loop can schedule it.
    """

    def __init__(self, expected: int) -> None:
        self.in_flight = 0
        self.peak = 0
        self._expected = expected
        self._released = asyncio.Event()

    async def __call__(self, source: OpportunitySource) -> None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        if self.in_flight >= self._expected:
            self._released.set()
        await self._released.wait()
        self.in_flight -= 1


def a_registry(*sources: OpportunitySource) -> SourceRegistry:
    registry = SourceRegistry()
    registry.register_all(sources)
    return registry


def an_orchestrator(registry: SourceRegistry, *,
                    packs: CountryPackRegistry | None = None,
                    max_concurrency: int = 4) -> DiscoveryOrchestrator:
    """An orchestrator over a registry, with a pack that binds everything in it.

    A test about isolation should not also be a test about bindings —
    `test_v2_source_registry.py` owns that filter — so the default pack enables
    every source registered, at the adapter's own priority.
    """
    if packs is None:
        packs = CountryPackRegistry((a_pack("CH", *(
            SourceBinding(source_key=key) for key in registry.source_keys)),))
    return DiscoveryOrchestrator(registry=registry, packs=packs,
                                 clock=frozen_clock(),
                                 max_concurrency=max_concurrency)


def a_profile(*areas: SearchArea, **overrides: object) -> SearchProfile:
    """A profile over the areas given, or over Switzerland when none is."""
    if areas:
        overrides["areas"] = areas
    return a_search_profile(**overrides)


def a_radius(radius_km: float = 30.0, label: str | None = "Lausanne",
             center: GeoPoint = LAUSANNE) -> RadiusSearchArea:
    return RadiusSearchArea(center=center, radius_km=radius_km, label=label)


def codes(warnings: tuple[DiscoveryWarning, ...]) -> list[DiscoveryWarningCode]:
    return [warning.code for warning in warnings]


# --- §10: SearchProfile → DiscoveryRequest -----------------------------------

def test_a_profile_that_does_not_reach_the_country_maps_to_no_request():
    """The empty tuple is the answer, and the caller reports it.

    Inventing a country-wide request instead would sweep a country the user never
    asked about — the one mistake in this mapping nobody would notice.
    """
    assert discovery_requests_for(a_profile(), country="FR") == ()


def test_the_flat_areas_become_one_request_carrying_every_label():
    """Two cities are two labels in one search, not two sweeps."""
    profile = a_profile(CountrySearchArea(country="CH", label="Lausanne"),
                        CountrySearchArea(country="CH", label="Geneve"))
    requests = discovery_requests_for(profile, country="CH")
    assert len(requests) == 1
    assert requests[0].locations == ("Lausanne", "Geneve")
    assert requests[0].radius is None


def test_a_country_area_without_a_label_searches_the_whole_country():
    """No label is not an empty label: the request simply carries no location."""
    requests = discovery_requests_for(a_profile(CountrySearchArea(country="CH")),
                                      country="CH")
    assert requests[0].locations == ()
    assert requests[0].country == "CH"


def test_each_radius_area_is_a_search_of_its_own():
    """The asymmetry this module exists to explain: `radius` is singular.

    "Within 20 km of Lausanne" and "within 20 km of Geneva" are two searches, so a
    profile with two circles and one country produces three requests — and the flat
    one comes first, which is what makes a report's outcomes readable in order.
    """
    profile = a_profile(CountrySearchArea(country="CH", label="Vaud"),
                        a_radius(20.0, "Lausanne"),
                        a_radius(20.0, "Geneve"))
    requests = discovery_requests_for(profile, country="CH")
    assert [r.radius.label if r.radius else None for r in requests] == [
        None, "Lausanne", "Geneve"]
    assert requests[0].locations == ("Vaud",)


def test_a_radius_request_carries_its_label_as_the_only_searchable_text():
    """§11 in the mapping layer: no source can take coordinates.

    The label travels as a place name and the constraint travels beside it, unhonoured
    but stated, which is what lets the orchestrator warn instead of pretending.
    """
    requests = discovery_requests_for(a_profile(a_radius(25.0, "Yverdon")),
                                      country="CH")
    assert len(requests) == 1
    assert requests[0].locations == ("Yverdon",)
    assert requests[0].radius is not None
    assert requests[0].radius.radius_km == 25.0
    assert requests[0].radius.center == LAUSANNE


def test_a_radius_without_a_label_leaves_a_source_nothing_to_type():
    """Honest rather than helpful: a fabricated place name would be a wrong search."""
    requests = discovery_requests_for(a_profile(a_radius(25.0, None)), country="CH")
    assert requests[0].locations == ()
    assert requests[0].radius is not None


def test_a_radius_is_swept_whatever_country_the_caller_named():
    """A circle carries no country, so the caller's pack is the assumption made."""
    assert len(discovery_requests_for(a_profile(a_radius()), country="FR")) == 1


def test_the_queries_reach_the_search_box_and_the_title_filter_stays_behind():
    """`title_keywords` narrow a result set; sending them would widen the sweep."""
    profile = a_profile(queries=("developpeur backend",), title_keywords=("python",))
    assert discovery_requests_for(profile, country="CH")[0].keywords == (
        "developpeur backend",)


def test_title_keywords_are_the_fallback_when_there_is_no_query():
    """Sending nothing would fetch a board's unfiltered front page."""
    profile = a_profile(queries=(), title_keywords=("python",))
    assert discovery_requests_for(profile, country="CH")[0].keywords == ("python",)


def test_the_allow_list_is_lowercased_rather_than_dropped():
    """A saved `Jobup` means `jobup`; discarding it would widen the sweep."""
    profile = a_profile(source_keys=("JobUp", "jooble"))
    assert discovery_requests_for(profile, country="CH")[0].source_keys == (
        "jobup", "jooble")


def test_a_remote_only_scope_becomes_a_remote_only_request():
    profile = a_profile(RemoteOnlySearchArea(country="CH", label="Remote CH"))
    request = discovery_requests_for(profile, country="CH")[0]
    assert request.remote_only
    assert request.include_remote


def test_a_scope_that_is_only_partly_remote_is_not_a_remote_only_request():
    """One on-site city in the profile and the request must still return on-site work."""
    profile = a_profile(RemoteOnlySearchArea(label="Remote"),
                        CountrySearchArea(country="CH", label="Lausanne"))
    request = discovery_requests_for(profile, country="CH")[0]
    assert not request.remote_only
    assert request.locations == ("Remote", "Lausanne")


def test_the_typed_preferences_travel_untouched():
    """§10's whole point: one search-preference model, mapped rather than mirrored."""
    profile = a_profile(opportunity_types=(OpportunityType.APPRENTICESHIP,),
                        workplace_modes=(WorkplaceMode.HYBRID, WorkplaceMode.REMOTE))
    request = discovery_requests_for(profile, country="CH")[0]
    assert request.opportunity_types == (OpportunityType.APPRENTICESHIP,)
    assert request.workplace_modes == (WorkplaceMode.HYBRID, WorkplaceMode.REMOTE)
    assert request.include_remote


def test_the_lookback_is_capped_however_long_a_caller_asks_for():
    """A month of history from a board that pages three days is a slow way to fail."""
    profile = a_profile()
    assert discovery_requests_for(profile, country="CH",
                                  lookback_days=30)[0].lookback_days \
        == MAX_LOOKBACK_DAYS


def test_the_limit_and_the_cursor_travel_on_every_request():
    """Including the radius ones: a resume token belongs to the sweep, not to an area."""
    profile = a_profile(CountrySearchArea(country="CH", label="Vaud"), a_radius())
    requests = discovery_requests_for(profile, country="CH", limit=7, cursor="page-2")
    assert [(r.limit, r.cursor) for r in requests] == [(7, "page-2"), (7, "page-2")]


def test_countries_in_scope_names_each_country_once_in_declaration_order():
    profile = a_profile(CountrySearchArea(country="CH", label="Vaud"),
                        CountrySearchArea(country="FR", label="Annecy"),
                        RemoteOnlySearchArea(country="CH"))
    assert countries_in_scope(profile) == ("CH", "FR")


def test_countries_in_scope_ignores_a_radius_because_a_circle_has_no_country():
    """A profile made only of circles does not say which country to sweep.

    30 km around Geneva is in two of them, so the answer is the caller's to give —
    and an empty tuple reads correctly as "this profile does not say".
    """
    assert countries_in_scope(a_profile(a_radius())) == ()


def test_a_remote_only_area_without_a_country_narrows_nothing():
    assert countries_in_scope(a_profile(RemoteOnlySearchArea())) == ()


# --- §13: a sweep resolves through the registry -------------------------------

@pytest.mark.asyncio
async def test_a_sweep_asks_every_selected_source_in_the_packs_own_order():
    """Acceptance criteria 1 and 2, in one assertion each.

    The order is the pack's, the sources are the registry's, and the request each
    one receives is the profile's — no board is named anywhere in the path.
    """
    jobup, jooble = FakeSource(a_metadata("jobup")), FakeSource(a_metadata("jooble"))
    registry = a_registry(jooble, jobup)
    packs = CountryPackRegistry((a_pack(
        "CH", SourceBinding(source_key="jobup", priority=1),
        SourceBinding(source_key="jooble", priority=80)),))
    report = await an_orchestrator(registry, packs=packs).sweep(a_profile(),
                                                                country="CH")
    assert [outcome.source_key for outcome in report.outcomes] == ["jobup", "jooble"]
    assert report.country == "CH"
    assert report.started_at == NOW
    assert len(jobup.requests) == 1 and len(jooble.requests) == 1


@pytest.mark.asyncio
async def test_the_source_receives_the_request_the_profile_mapped_to():
    """The §10 mapping and the §13 sweep, joined up once end to end."""
    board = FakeSource(a_metadata("jobup"))
    await an_orchestrator(a_registry(board)).sweep(
        a_profile(queries=("developpeur",),
                  areas=(CountrySearchArea(country="CH", label="Lausanne"),)),
        country="CH", limit=25)
    request = board.requests[0]
    assert request.country == "CH"
    assert request.keywords == ("developpeur",)
    assert request.locations == ("Lausanne",)
    assert request.limit == 25


@pytest.mark.asyncio
async def test_a_sweep_of_two_areas_asks_the_same_source_once_per_search():
    """One circle, one country area: two requests, one source, two outcomes."""
    board = FakeSource(a_metadata("jobup"))
    report = await an_orchestrator(a_registry(board)).sweep(
        a_profile(CountrySearchArea(country="CH", label="Vaud"), a_radius()),
        country="CH")
    assert len(board.requests) == 2
    assert len(report.outcomes) == 2
    assert report.source_keys == ("jobup",)


# --- §14: one source's fault is its own ---------------------------------------

@pytest.mark.asyncio
async def test_a_source_that_raises_does_not_cost_its_neighbours_their_answer():
    """The requirement the whole module is shaped around.

    `discover` is contractually not allowed to raise, so this is a bug in one
    adapter — and a bug in one adapter may not turn eleven working boards into an
    empty sweep.
    """
    broken = FakeSource(a_metadata("jooble"), error=RuntimeError("boom"))
    working = FakeSource(a_metadata("jobup"),
                         result=a_result("jobup", an_opportunity()))
    report = await an_orchestrator(a_registry(broken, working)).sweep(a_profile(),
                                                                     country="CH")
    assert len(report.opportunities) == 1
    assert report.unusable_source_keys == ("jooble",)
    failed = next(h for h in report.health if h.source_key == "jooble")
    assert failed.status is SourceHealthStatus.UNAVAILABLE
    assert failed.reason is SourceFailureCode.SOURCE_ADAPTER_ERROR
    assert not report.is_complete


@pytest.mark.asyncio
async def test_a_raising_source_reports_no_counters_and_no_postings():
    """An adapter that crashed never said what it spent, so nothing is guessed.

    The measured wall clock is the one real number, and it is kept — a zero there
    would make `duration_ms` unreadable as a signal.
    """
    broken = FakeSource(a_metadata("jooble"), error=RuntimeError("boom"))
    report = await an_orchestrator(a_registry(broken)).sweep(a_profile(),
                                                            country="CH")
    metrics = report.outcomes[0].result.metrics
    assert (metrics.requests_made, metrics.postings_seen) == (0, 0)
    assert metrics.duration_ms is not None
    assert report.outcomes[0].result.opportunities == ()
    assert report.outcomes[0].request.country == "CH"


@pytest.mark.asyncio
async def test_a_sweep_records_what_every_source_said_in_the_registry():
    """§12: "what is the state of discovery?" needs no second sweep."""
    broken = FakeSource(a_metadata("jooble"), error=TimeoutError("timed out"))
    working = FakeSource(a_metadata("jobup"))
    registry = a_registry(broken, working)
    await an_orchestrator(registry).sweep(a_profile(), country="CH")
    assert registry.health_for("jobup").status is SourceHealthStatus.HEALTHY
    assert registry.unusable_source_keys() == ("jooble",)


@pytest.mark.asyncio
async def test_a_source_asked_twice_is_reported_by_its_worst_answer():
    """Otherwise the report would depend on which task finished last.

    One board, two circles: HEALTHY then UNAVAILABLE has to read as an outage, or a
    caller treats a half-swept area as the state of the market.
    """
    board = ScriptedSource(
        a_metadata("jobup"), a_result("jobup", an_opportunity()),
        a_result("jobup", health=a_health("jobup", SourceHealthStatus.UNAVAILABLE)))
    report = await an_orchestrator(a_registry(board)).sweep(
        a_profile(a_radius(20.0, "Lausanne"), a_radius(20.0, "Geneve")),
        country="CH")
    assert len(report.health) == 1
    assert report.health[0].status is SourceHealthStatus.UNAVAILABLE
    assert report.unusable_source_keys == ("jobup",)
    assert len(report.opportunities) == 1


# --- §18: an empty selection is a named outcome -------------------------------

@pytest.mark.asyncio
async def test_a_country_with_no_selectable_source_says_so_by_name():
    """An empty list of opportunities reads as "nobody is hiring". This does not.

    The pack registers no binding, so nothing is enabled for CH — a configuration
    state an operator can act on, and one a caller must be able to tell from a
    market with no vacancies.
    """
    board = FakeSource(a_metadata("jobup"))
    packs = CountryPackRegistry((a_pack("CH"),))
    report = await an_orchestrator(a_registry(board), packs=packs).sweep(
        a_profile(), country="CH")
    assert codes(report.warnings) == [Warn.NO_SOURCE_SELECTED]
    assert "no source is both registered and enabled for CH" \
        in report.warnings[0].detail
    assert report.outcomes == () and report.opportunities == ()
    assert board.requests == []
    assert not report.is_complete


@pytest.mark.asyncio
async def test_an_allow_list_that_matches_nothing_names_the_allow_list():
    """The same code, a different cause: the profile did the narrowing, not the pack.

    Two identical warnings would send an operator to `sources.yaml` for a problem
    that lives in a saved search.
    """
    report = await an_orchestrator(a_registry(FakeSource(a_metadata("jobup")))).sweep(
        a_profile(source_keys=("jooble",)), country="CH")
    assert codes(report.warnings) == [Warn.NO_SOURCE_SELECTED]
    assert "within the profile's allow-list" in report.warnings[0].detail


@pytest.mark.asyncio
async def test_a_profile_that_names_no_area_in_the_country_sweeps_nothing():
    """Nothing is asked and the report says why, rather than sweeping all of France."""
    board = FakeSource(a_metadata("jobup"))
    packs = CountryPackRegistry((a_pack("CH", SourceBinding(source_key="jobup")),
                                 a_pack("FR", SourceBinding(source_key="jobup"))))
    report = await an_orchestrator(a_registry(board), packs=packs).sweep(
        a_profile(), country="FR")
    assert codes(report.warnings) == [Warn.NO_SOURCE_SELECTED]
    assert "the profile names no area in FR" in report.warnings[0].detail
    assert board.requests == []


@pytest.mark.asyncio
async def test_a_country_with_no_pack_is_a_composition_error_not_an_outcome():
    """§16: a missing pack fails loudly, because no sweep of DE was ever configured.

    The distinction that matters: everything that can go wrong *after* the pack is
    found is data in the report, and this is not one of those things.
    """
    orchestrator = an_orchestrator(a_registry(FakeSource(a_metadata("jobup"))))
    with pytest.raises(CountryPackError) as raised:
        await orchestrator.sweep(a_profile(), country="DE")
    assert raised.value.code is CountryPackErrorCode.COUNTRY_PACK_NOT_FOUND
    assert "DE" in str(raised.value)


# --- §11: a radius nothing can honour -----------------------------------------

@pytest.mark.asyncio
async def test_the_radius_policy_is_stated_once_for_the_whole_source_set():
    """Not once per source: the question is whether the *results* were filtered.

    Each adapter already warns that it personally cannot filter by distance. What a
    caller actually needs to know is whether anything did, and no single source's
    warning can answer that.
    """
    registry = a_registry(FakeSource(a_metadata("jobup")),
                          FakeSource(a_metadata("jooble")))
    report = await an_orchestrator(registry).sweep(
        a_profile(a_radius(30.0, "Lausanne")), country="CH")
    assert codes(report.warnings) == [Warn.RADIUS_NOT_SUPPORTED]
    warning = report.warnings[0]
    assert "30 km around Lausanne" in warning.detail
    assert "not radius-filtered" in warning.detail
    assert warning.capability is Cap.RADIUS_SEARCH
    # A request-level warning names no source: a reader would otherwise go looking
    # for a fault in a board behaving exactly as it says it does.
    assert warning.source_key is None
    assert not report.is_complete


@pytest.mark.asyncio
async def test_a_source_that_really_filters_by_distance_silences_the_policy():
    """The day one grows a distance parameter, the warning has to stop appearing."""
    capable = FakeSource(a_metadata("jobup", capabilities=frozenset(
        {Cap.KEYWORD_SEARCH, Cap.RADIUS_SEARCH})))
    report = await an_orchestrator(a_registry(capable)).sweep(
        a_profile(a_radius()), country="CH")
    assert report.warnings == ()
    assert report.is_complete


@pytest.mark.asyncio
async def test_nothing_is_said_about_a_radius_no_source_was_going_to_honour():
    """With an empty selection the radius is moot, and one warning is the useful one.

    Reporting both would tell an operator two things about one problem, and the
    actionable one is that CH has no enabled source.
    """
    packs = CountryPackRegistry((a_pack("CH"),))
    report = await an_orchestrator(a_registry(FakeSource(a_metadata("jobup"))),
                                   packs=packs).sweep(a_profile(a_radius()),
                                                      country="CH")
    assert codes(report.warnings) == [Warn.NO_SOURCE_SELECTED]


@pytest.mark.asyncio
async def test_two_circles_are_two_conditions_and_each_names_its_own_centre():
    """One warning per unhonoured radius, because each names a different place.

    Collapsing them would leave a reader unable to tell which of the two areas came
    back unfiltered — and the answer is both, said twice on purpose.
    """
    report = await an_orchestrator(a_registry(FakeSource(a_metadata("jobup")))).sweep(
        a_profile(a_radius(30.0, "Lausanne"), a_radius(15.0, "Yverdon")),
        country="CH")
    assert codes(report.warnings) == [Warn.RADIUS_NOT_SUPPORTED] * 2
    assert [w.detail.split(" was sent")[0] for w in report.warnings] == [
        "no source swept filters by distance, so 30 km around Lausanne",
        "no source swept filters by distance, so 15 km around Yverdon"]


@pytest.mark.asyncio
async def test_a_radius_without_a_label_is_described_by_its_coordinates():
    """A radius with no label is described by its coordinates, not by its type."""
    report = await an_orchestrator(a_registry(FakeSource(a_metadata("jobup")))).sweep(
        a_profile(a_radius(30.0, None)), country="CH")
    assert "46.5197, 6.6323" in report.warnings[0].detail


# --- what a report says about itself ------------------------------------------

@pytest.mark.asyncio
async def test_the_metrics_are_summed_over_the_sources_with_the_sweeps_own_clock():
    """`merge` drops durations on purpose: a concurrent sweep is not a sum of parts.

    The sweep's measured elapsed time is put back at the top level, where it is the
    honest number.
    """
    registry = a_registry(FakeSource(a_metadata("jobup"),
                                     result=a_result("jobup", an_opportunity())),
                          FakeSource(a_metadata("jooble"),
                                     result=a_result("jooble", an_opportunity(
                                         "jooble", "https://example.ch/offre/2"))))
    report = await an_orchestrator(registry).sweep(a_profile(), country="CH")
    assert report.metrics.requests_made == 2
    assert report.metrics.postings_seen == 2
    assert report.metrics.opportunities_returned == 2
    assert report.metrics.duration_ms == report.duration_ms


@pytest.mark.asyncio
async def test_a_report_is_complete_only_when_every_source_answered_in_full():
    board = FakeSource(a_metadata("jobup"), result=a_result("jobup", an_opportunity()))
    report = await an_orchestrator(a_registry(board)).sweep(a_profile(), country="CH")
    assert report.is_complete
    assert report.unusable_source_keys == ()


@pytest.mark.asyncio
async def test_one_truncated_source_is_enough_to_make_a_sweep_incomplete():
    """A per-source warning counts: `limit` cut the answer short and it still shows.

    The one question a caller has to be able to ask before treating a sweep as the
    state of the market, and a DEGRADED-free report can still fail it.
    """
    truncated = a_result("jobup", an_opportunity(), warnings=(DiscoveryWarning(
        code=Warn.LIMIT_TRUNCATED, detail="stopped at the caller's limit of 1",
        source_key="jobup"),))
    report = await an_orchestrator(
        a_registry(FakeSource(a_metadata("jobup"), result=truncated))).sweep(
            a_profile(), country="CH")
    assert report.warnings == ()  # nothing wrong with the request
    assert not report.is_complete
    assert report.unusable_source_keys == ()


@pytest.mark.asyncio
async def test_the_sweep_does_not_deduplicate_across_sources():
    """Pinned as a decision, not an oversight (§15 keeps both provenances).

    Two boards publishing one vacancy are two `Opportunity` objects that agree on
    `dedup_fingerprint`; collapsing them here would throw away which board found it
    first, and the repository resolves it at write time.
    """
    registry = a_registry(
        FakeSource(a_metadata("jobup"), result=a_result("jobup", an_opportunity())),
        FakeSource(a_metadata("jooble"),
                   result=a_result("jooble", an_opportunity("jooble"))))
    report = await an_orchestrator(registry).sweep(a_profile(), country="CH")
    assert len(report.opportunities) == 2
    first, second = report.opportunities
    assert first.source.source_key == "jobup"
    assert second.source.source_key == "jooble"
    assert first.dedup_fingerprint == second.dedup_fingerprint


@pytest.mark.asyncio
async def test_the_opportunities_come_back_in_the_order_the_sources_were_queried():
    """So two identical sweeps produce identical reports, page 1 included."""
    packs = CountryPackRegistry((a_pack(
        "CH", SourceBinding(source_key="jooble", priority=1),
        SourceBinding(source_key="jobup", priority=80)),))
    registry = a_registry(
        FakeSource(a_metadata("jobup"), result=a_result("jobup", an_opportunity())),
        FakeSource(a_metadata("jooble"),
                   result=a_result("jooble", an_opportunity("jooble"))))
    report = await an_orchestrator(registry, packs=packs).sweep(a_profile(),
                                                               country="CH")
    assert [o.source.source_key for o in report.opportunities] == ["jooble", "jobup"]


# --- §14: bounded fan-out, and cancellation that stays cancelled ---------------

@pytest.mark.asyncio
async def test_no_more_sources_are_in_flight_than_the_bound_allows():
    """§14's "no uncontrolled fan-out", observed rather than assumed.

    Twelve boards swept at once is twelve sets of sockets and threads on someone
    else's infrastructure. The `Gate` counts arrivals instead of sleeping, so the
    peak is a fact and the test finishes as fast as the loop can schedule it.
    """
    gate = Gate(expected=2)
    registry = a_registry(*(FakeSource(a_metadata(key), on_discover=gate)
                            for key in ("aaa", "bbb", "ccc", "ddd")))
    report = await an_orchestrator(registry, max_concurrency=2).sweep(a_profile(),
                                                                     country="CH")
    assert gate.peak == 2
    assert len(report.outcomes) == 4
    assert gate.in_flight == 0


@pytest.mark.asyncio
async def test_a_bound_of_one_makes_a_sweep_strictly_sequential():
    """The knob a test uses to get a deterministic order of arrival."""
    gate = Gate(expected=1)
    registry = a_registry(*(FakeSource(a_metadata(key), on_discover=gate)
                            for key in ("aaa", "bbb", "ccc")))
    report = await an_orchestrator(registry, max_concurrency=1).sweep(a_profile(),
                                                                     country="CH")
    assert gate.peak == 1
    assert report.source_keys == ("aaa", "bbb", "ccc")


def test_a_bound_below_one_is_refused_rather_than_clamped():
    """Zero concurrency is a sweep that never runs, which is not a configuration."""
    with pytest.raises(ValueError, match="max_concurrency"):
        an_orchestrator(a_registry(FakeSource()), max_concurrency=0)


@pytest.mark.asyncio
async def test_a_cancelled_sweep_stays_cancelled():
    """Why `_discover` catches `Exception` and not `BaseException`.

    `CancelledError` is not an `Exception`, so a shutdown propagates instead of
    being recorded as twelve source outages that never happened.
    """
    board = FakeSource(a_metadata("jobup"), error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await an_orchestrator(a_registry(board)).sweep(a_profile(), country="CH")


# --- §12: the round of probes -------------------------------------------------

@pytest.mark.asyncio
async def test_a_healthcheck_probes_every_source_and_answers_in_key_order():
    """Sorted, not in completion order, so a status page does not reshuffle itself."""
    wtj, coop = FakeSource(a_metadata("wtj")), FakeSource(a_metadata("coop"))
    registry = a_registry(wtj, coop)
    records = await an_orchestrator(registry).healthcheck(country="CH")
    assert tuple(record.source_key for record in records) == ("coop", "wtj")
    assert (wtj.healthchecks, coop.healthchecks) == (1, 1)
    # Recorded on the way out, so the next caller needs no probe at all.
    assert registry.health_for("wtj").status is SourceHealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_a_probe_that_raises_becomes_one_record_and_not_a_lost_round():
    """Same isolation as a sweep: one refusal is one record, never an exception."""
    broken = FakeSource(a_metadata("jooble"), error=RuntimeError("boom"))
    registry = a_registry(broken, FakeSource(a_metadata("jobup")))
    records = await an_orchestrator(registry).healthcheck(country="CH")
    assert len(records) == 2
    failed = records[1]
    assert failed.source_key == "jooble"
    assert failed.status is SourceHealthStatus.UNAVAILABLE
    assert failed.reason is SourceFailureCode.SOURCE_ADAPTER_ERROR
    assert failed.checked_at == NOW
    assert failed.latency_ms is not None
    assert registry.unusable_source_keys() == ("jooble",)


@pytest.mark.asyncio
async def test_a_healthcheck_of_no_country_in_particular_probes_them_all():
    """A round about every country is the one question a pack cannot narrow.

    A status page asks about the whole registry; a country's own round is filtered
    by that country's pack, which is what keeps `indeed` (FR) out of a Swiss one.
    """
    registry = a_registry(FakeSource(a_metadata("indeed", countries=("FR",))),
                          FakeSource(a_metadata("jobup", countries=("CH",))))
    orchestrator = an_orchestrator(registry)
    everywhere = await orchestrator.healthcheck()
    swiss = await orchestrator.healthcheck(country="CH")
    assert tuple(r.source_key for r in everywhere) == ("indeed", "jobup")
    assert tuple(r.source_key for r in swiss) == ("jobup",)














