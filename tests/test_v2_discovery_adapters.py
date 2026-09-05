# tests/test_v2_discovery_adapters.py
"""The wrapped V1 sources: which calls they make, and what they say about them.

§18 asks for every Swiss source to be reachable through the new contract with no
live network, and `V1SearchCallable` is what makes that a one-line fake: an adapter
is a plan, a thread hop and a normalization pass around a callable, so a recording
callable exercises all three.

Two behaviours here are the reason the adapter layer exists rather than the
orchestrator calling V1 directly. The **plan** is built from the dimensions a source
claims, so `migros` is fetched once instead of once per query × location — the same
postings for a ninth of the requests. And **`_input_warnings`** says out loud which
part of the request was dropped on the floor, which is the question V1 cannot
answer: given a result set, was "Lausanne" a filter or not?

Nothing in this file opens a socket. `RecordingSearch` and `RecordingFetch` stand in
for `pipeline/sources/*.py`, and the one test that touches the real catalog only
builds adapters — binding a callable makes no request.
"""
import pytest
import yaml

from backend.app.discovery.adapters.v1_catalog import build_v1_sources, source_metadata
from backend.app.discovery.adapters.v1_sources import (
    MAX_REQUESTS_PER_SWEEP,
    PROBE_LOOKBACK_DAYS,
    V1AtsSourceAdapter,
    V1QuerySourceAdapter,
    load_v1_companies,
)
from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.contracts import (
    DiscoveryWarningCode,
    OpportunitySource,
    RadiusConstraint,
    SourceFailureCode,
    SourceHealthStatus,
    SourceMetadata,
)
from backend.app.domain.common import GeoPoint
from backend.app.domain.opportunity import OpportunityType
from country_packs.errors import CountryPackError, CountryPackErrorCode
from pipeline.http_fetch import FetchError
from tests.v2_discovery import NOW, a_metadata, a_pack, a_request, frozen_clock

Cap = SourceCapability
Warn = DiscoveryWarningCode
PACK = a_pack("CH")

# The two shapes that matter for a plan: a source that reads both dimensions, and
# one that reads neither and returns its own listing whatever it is asked.
SEARCHING = frozenset({Cap.KEYWORD_SEARCH, Cap.LOCATION_SEARCH})
HARDCODED = frozenset({Cap.HEALTHCHECK})


def a_posting(**overrides) -> dict[str, object]:
    """One row as `pipeline/sources/_common.py:normalize()` returns it."""
    posting: dict[str, object] = {
        "source": "fake_board", "company": "Neocraft SA",
        "title": "Developpeur backend", "url": "https://example.ch/offre/1",
        "location": "Lausanne", "description": "Une equipe, un produit.",
    }
    posting.update(overrides)
    return posting


class RecordingSearch:
    """`search_jobs`, recording its arguments instead of making a request.

    `error` fails every call (an outage); `errors` fails the calls whose index it
    names (a partial answer), which is the distinction `_result` turns into
    UNAVAILABLE versus DEGRADED.
    """

    def __init__(self, *, postings: list[dict[str, object]] | None = None,
                 error: BaseException | None = None,
                 errors: list[BaseException | None] | None = None) -> None:
        self._postings = [a_posting()] if postings is None else postings
        self._error = error
        self._errors = errors or []
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, query: str, location: str,
                 lookback_days: int = 3) -> list[dict[str, object]]:
        index = len(self.calls)
        self.calls.append((query, location, lookback_days))
        if self._error is not None:
            raise self._error
        if index < len(self._errors) and self._errors[index] is not None:
            raise self._errors[index]  # type: ignore[misc]
        return [dict(posting) for posting in self._postings]


class RecordingFetch:
    """`fetch_jobs(token, company)`, for the three ATS boards."""

    def __init__(self, *, postings: list[dict[str, object]] | None = None,
                 error: BaseException | None = None) -> None:
        self._postings = [a_posting()] if postings is None else postings
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def __call__(self, token: str, company: str) -> list[dict[str, object]]:
        self.calls.append((token, company))
        if self._error is not None:
            raise self._error
        return [dict(posting) for posting in self._postings]


class Board:
    """One `config/companies.yaml` entry."""

    def __init__(self, token: str, company: str) -> None:
        self.token = token
        self.company = company


def a_query_source(search: RecordingSearch, metadata: SourceMetadata | None = None, *,
                   packs=None) -> V1QuerySourceAdapter:
    return V1QuerySourceAdapter(
        metadata=metadata if metadata is not None else a_metadata(),
        search=search,
        packs=packs if packs is not None else (lambda country: PACK),
        clock=frozen_clock())


def an_ats_source(fetch: RecordingFetch, *boards: Board,
                  metadata: SourceMetadata | None = None) -> V1AtsSourceAdapter:
    return V1AtsSourceAdapter(
        metadata=metadata if metadata is not None else a_metadata(
            "greenhouse", capabilities=frozenset({Cap.COMPANY_FILTER,
                                                  Cap.HEALTHCHECK})),
        fetch=fetch, boards=boards, packs=lambda country: PACK,
        clock=frozen_clock())


def codes(result) -> set[DiscoveryWarningCode]:
    return {warning.code for warning in result.warnings}


# --- one contract, thirteen implementations ----------------------------------

def test_every_v1_source_is_reachable_through_the_contract():
    """§7: thirteen modules wrapped, none rewritten, none left behind.

    `boards={}` and a fake resolver, so the assertion is about the wrapping and not
    about an operator's company list. Building an adapter makes no request.
    """
    sources = build_v1_sources(packs=lambda country: PACK, boards={})
    assert len(sources) == 13
    assert all(isinstance(source, OpportunitySource) for source in sources)
    assert {source.metadata.source_key for source in sources} \
        == {metadata.source_key for metadata in source_metadata()}


# --- the plan: a request only where it buys something ------------------------

@pytest.mark.asyncio
async def test_a_source_reading_both_dimensions_is_called_once_per_pair():
    """V1's cross product, kept where it is justified."""
    search = RecordingSearch()
    source = a_query_source(search, a_metadata("jobup", capabilities=SEARCHING))
    await source.discover(a_request(keywords=("dev", "data"),
                                    locations=("Lausanne", "Geneve")))
    assert search.calls == [("dev", "Lausanne", 3), ("dev", "Geneve", 3),
                            ("data", "Lausanne", 3), ("data", "Geneve", 3)]


@pytest.mark.asyncio
async def test_a_source_reading_neither_dimension_is_called_exactly_once():
    """The V1 waste this adapter removes: nine identical fetches become one.

    `migros`, `jobscout24` and `manpower` read neither argument, so the same
    hardcoded listing came back once per query × location. The postings are
    unchanged; only the request count is.
    """
    search = RecordingSearch()
    source = a_query_source(search, a_metadata("migros", capabilities=HARDCODED))
    result = await source.discover(a_request(keywords=("dev", "data"),
                                             locations=("Lausanne", "Geneve")))
    assert search.calls == [("", "", 3)]
    assert result.metrics.requests_made == 1
    assert len(result.opportunities) == 1


@pytest.mark.asyncio
async def test_a_dimension_the_source_reads_but_the_request_omits_is_one_blank():
    """An empty collection restricts nothing — here, "whatever the board lists"."""
    search = RecordingSearch()
    source = a_query_source(search, a_metadata("jobup", capabilities=SEARCHING))
    await source.discover(a_request(keywords=(), locations=()))
    assert search.calls == [("", "", 3)]


@pytest.mark.asyncio
async def test_the_lookback_window_travels_even_to_a_source_that_ignores_it():
    """Exactly as V1 passes it: harmless, and `LOOKBACK_IGNORED` says it had no effect."""
    search = RecordingSearch()
    source = a_query_source(search, a_metadata("migros", capabilities=HARDCODED))
    result = await source.discover(a_request(lookback_days=7))
    assert search.calls == [("", "", 7)]
    assert Warn.LOOKBACK_IGNORED in codes(result)


@pytest.mark.asyncio
async def test_a_plan_larger_than_the_ceiling_is_truncated_and_reported():
    """A saved profile has no 3 × 3 ceiling, and 30 requests to one board is rude.

    V1's cross product came from a fixed config file; a `SearchProfile` can name
    five keywords and six cities. The truncation is deterministic — the plan is
    ordered — and `PARTIAL_RESULTS` is what stops it being silent.
    """
    search = RecordingSearch()
    source = a_query_source(search, a_metadata("jobup", capabilities=SEARCHING))
    result = await source.discover(a_request(
        keywords=("a", "b", "c", "d", "e"),
        locations=("f", "g", "h", "i", "j", "k")))
    assert len(search.calls) == MAX_REQUESTS_PER_SWEEP
    assert result.metrics.requests_made == MAX_REQUESTS_PER_SWEEP
    assert Warn.PARTIAL_RESULTS in codes(result)
    assert result.health.status is SourceHealthStatus.HEALTHY


# --- what the source could not honour (§11) ----------------------------------

@pytest.mark.asyncio
async def test_a_hardcoded_listing_says_it_read_neither_argument():
    """The two warnings that make an unfiltered result set legible."""
    source = a_query_source(RecordingSearch(),
                            a_metadata("migros", capabilities=HARDCODED))
    result = await source.discover(a_request(keywords=("dev",),
                                             locations=("Lausanne",)))
    assert {Warn.KEYWORD_IGNORED, Warn.LOCATION_IGNORED} <= codes(result)
    assert result.health.status is SourceHealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_a_parameter_that_only_reranks_is_reported_as_advisory():
    """jobup's location, in miniature: sent, honoured, but not a filter."""
    source = a_query_source(RecordingSearch(), a_metadata(
        "jobup", capabilities=SEARCHING,
        advisory_capabilities=frozenset({Cap.LOCATION_SEARCH})))
    result = await source.discover(a_request(locations=("Lausanne",)))
    advisory = [w for w in result.warnings
                if w.code is Warn.CAPABILITY_ADVISORY_ONLY]
    assert [w.capability for w in advisory] == [Cap.LOCATION_SEARCH]
    assert Warn.LOCATION_IGNORED not in codes(result)


@pytest.mark.asyncio
async def test_a_radius_no_source_can_honour_is_named_not_silently_dropped():
    """§11: no V1 source takes a distance, so every sweep says so per source.

    The warning names Phase 7 because that is where the filtering will happen; what
    matters here is that the caller cannot mistake these results for "within 20 km".
    """
    source = a_query_source(RecordingSearch(), a_metadata("jobup",
                                                          capabilities=SEARCHING))
    result = await source.discover(a_request(radius=RadiusConstraint(
        center=GeoPoint(latitude=46.52, longitude=6.63), radius_km=20.0)))
    radius = [w for w in result.warnings if w.code is Warn.RADIUS_NOT_SUPPORTED]
    assert len(radius) == 1
    assert radius[0].capability is Cap.RADIUS_SEARCH
    assert "20 km" in radius[0].detail


@pytest.mark.asyncio
async def test_a_cursor_a_source_cannot_read_is_reported_ignored():
    """Nothing claims PAGINATION, so a resumed sweep silently restarts (§6)."""
    source = a_query_source(RecordingSearch(), a_metadata("jobup",
                                                          capabilities=SEARCHING))
    result = await source.discover(a_request(cursor="page-2"))
    assert Warn.CURSOR_IGNORED in codes(result)
    assert result.cursor is None


@pytest.mark.asyncio
async def test_a_filter_the_source_cannot_apply_is_deferred_explicitly():
    """Types and workplace modes have to be filtered after normalization."""
    source = a_query_source(RecordingSearch(), a_metadata("jobup",
                                                          capabilities=SEARCHING))
    result = await source.discover(a_request(
        opportunity_types=(OpportunityType.INTERNSHIP,), remote_only=True))
    deferred = [w for w in result.warnings
                if w.code is Warn.CAPABILITY_NOT_SUPPORTED]
    assert {w.capability for w in deferred} == {Cap.OPPORTUNITY_TYPE_FILTER,
                                               Cap.REMOTE_FILTER}


@pytest.mark.asyncio
async def test_a_source_that_honours_everything_asked_warns_nothing():
    """The other half of the contract: silence means "filtered as asked"."""
    source = a_query_source(RecordingSearch(), a_metadata(
        "perfect", capabilities=frozenset({Cap.KEYWORD_SEARCH, Cap.LOCATION_SEARCH,
                                           Cap.INCREMENTAL_DISCOVERY})))
    result = await source.discover(a_request())
    assert result.warnings == ()


# --- health: an outage, a partial answer, a missing variable (§12) ------------

@pytest.mark.asyncio
async def test_every_call_failing_is_an_outage_that_carries_no_postings():
    """`DiscoveryResult` refuses postings under an unusable health, and rightly.

    A caller must not have to guess whether a short list is complete, so the
    postings collected before the failure are dropped rather than published as if
    they were the answer.
    """
    search = RecordingSearch(error=FetchError("https://example.ch: HTTP 503"))
    source = a_query_source(search, a_metadata("jobup", capabilities=SEARCHING))
    result = await source.discover(a_request(keywords=("dev", "data")))
    assert len(search.calls) == 2
    assert result.health.status is SourceHealthStatus.UNAVAILABLE
    assert result.health.reason is SourceFailureCode.SOURCE_UNAVAILABLE
    assert result.opportunities == ()
    assert result.metrics.opportunities_returned == 0
    assert not result.is_usable


@pytest.mark.asyncio
async def test_some_calls_failing_is_degraded_with_the_data_that_did_arrive():
    """V1 records the error and moves on; this is that behaviour, typed.

    Four answers out of six is real data plus an incomplete answer, which is
    exactly what DEGRADED means — and the detail counts the failures without
    quoting the exception.
    """
    search = RecordingSearch(errors=[FetchError("https://example.ch: HTTP 500"), None])
    source = a_query_source(search, a_metadata("jobup", capabilities=SEARCHING))
    result = await source.discover(a_request(keywords=("dev", "data")))
    assert result.health.status is SourceHealthStatus.DEGRADED
    assert result.health.reason is SourceFailureCode.SOURCE_PARTIAL_FAILURE
    assert "1 of 2 requests failed" in result.health.detail
    assert len(result.opportunities) == 1
    assert result.is_usable


@pytest.mark.asyncio
async def test_a_missing_credential_is_reported_before_any_request(monkeypatch):
    """V1 spends a round trip discovering its own configuration gap; this does not.

    The detail names the variable — the actionable half, and one that cannot be a
    secret because `EnvVarName` forbids the shape of one.
    """
    monkeypatch.delenv("JOOBLE_API_KEY", raising=False)
    search = RecordingSearch()
    source = a_query_source(search, a_metadata(
        "jooble", capabilities=SEARCHING, requires_credentials=True,
        credential_env_vars=("JOOBLE_API_KEY",)))
    result = await source.discover(a_request())
    assert search.calls == []
    assert result.health.status is SourceHealthStatus.MISCONFIGURED
    assert "JOOBLE_API_KEY" in result.health.detail
    assert result.metrics.requests_made == 0


@pytest.mark.asyncio
async def test_a_credential_that_is_set_lets_the_sweep_run(monkeypatch):
    """The other side of the gate, so the check cannot be a blanket refusal."""
    monkeypatch.setenv("JOOBLE_API_KEY", "b7f3c1e9d2a48f60")
    search = RecordingSearch()
    source = a_query_source(search, a_metadata(
        "jooble", capabilities=SEARCHING, requires_credentials=True,
        credential_env_vars=("JOOBLE_API_KEY",)))
    result = await source.discover(a_request())
    assert len(search.calls) == 1
    assert result.health.status is SourceHealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_a_blank_credential_counts_as_missing(monkeypatch):
    """`JOOBLE_API_KEY=` in a `.env` file is a configuration gap, not a key."""
    monkeypatch.setenv("JOOBLE_API_KEY", "   ")
    source = a_query_source(RecordingSearch(), a_metadata(
        "jooble", capabilities=SEARCHING, requires_credentials=True,
        credential_env_vars=("JOOBLE_API_KEY",)))
    result = await source.discover(a_request())
    assert result.health.status is SourceHealthStatus.MISCONFIGURED


@pytest.mark.asyncio
async def test_a_pack_that_cannot_be_resolved_fails_before_any_request():
    """A composition fault reported as this source's failure, not as a crash.

    §14: one source's problem — here, a sweep asking for a country nobody loaded —
    must not propagate out of `discover`, because the eleven other sources have
    nothing to do with it.
    """
    def no_pack(country: str):
        raise CountryPackError(CountryPackErrorCode.COUNTRY_PACK_NOT_FOUND,
                               f"no pack is registered for {country}")

    search = RecordingSearch()
    source = a_query_source(search, packs=no_pack)
    result = await source.discover(a_request())
    assert search.calls == []
    assert result.health.status is SourceHealthStatus.UNAVAILABLE
    assert result.health.reason is SourceFailureCode.SOURCE_ADAPTER_ERROR


# --- what comes back: dedup, skips, the limit, provenance --------------------

@pytest.mark.asyncio
async def test_the_same_vacancy_matching_two_keywords_is_counted_once():
    """Two keywords legitimately match one posting; two rows would inflate everything.

    `postings_seen` still says two, because two rows *were* seen — the metrics
    distinguish "fetched twice" from "found twice", which is what makes the
    deduplication auditable instead of invisible.
    """
    search = RecordingSearch()
    source = a_query_source(search, a_metadata("jobup", capabilities=SEARCHING))
    result = await source.discover(a_request(keywords=("dev", "data")))
    assert len(result.opportunities) == 1
    assert result.metrics.postings_seen == 2
    assert result.metrics.opportunities_returned == 1
    assert result.metrics.postings_skipped == 0


@pytest.mark.asyncio
async def test_a_row_that_cannot_say_who_is_hiring_is_dropped_and_counted():
    """One bad row must not fail a board that returned forty good ones."""
    search = RecordingSearch(postings=[
        a_posting(), a_posting(company="", url="https://example.ch/offre/2")])
    source = a_query_source(search, a_metadata("jobup", capabilities=SEARCHING))
    result = await source.discover(a_request(keywords=("dev",)))
    assert len(result.opportunities) == 1
    assert result.metrics.postings_skipped == 1
    skipped = [w for w in result.warnings if w.code is Warn.POSTING_SKIPPED]
    assert len(skipped) == 1
    assert result.health.status is SourceHealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_more_opportunities_than_the_limit_are_truncated_and_reported():
    """`limit` is per source per request, so one board's cap is its own."""
    search = RecordingSearch(postings=[
        a_posting(url="https://example.ch/offre/1"),
        a_posting(url="https://example.ch/offre/2", title="Data engineer")])
    source = a_query_source(search, a_metadata("jobup", capabilities=SEARCHING))
    result = await source.discover(a_request(keywords=("dev",), limit=1))
    assert len(result.opportunities) == 1
    assert Warn.LIMIT_TRUNCATED in codes(result)


@pytest.mark.asyncio
async def test_provenance_survives_the_wrapping():
    """§15, at the adapter boundary: which source, which URL, when, and the raw row.

    The clock is injected, so `fetched_at` is the instant the adapter recorded and
    not "roughly now" — a report that cannot be reproduced is not provenance.
    """
    search = RecordingSearch(postings=[a_posting(salary="CHF 90'000")])
    source = a_query_source(search, a_metadata("jobup", capabilities=SEARCHING))
    result = await source.discover(a_request(keywords=("dev",)))
    record = result.opportunities[0].source
    assert record.source_key == "jobup"
    assert record.source_url == "https://example.ch/offre/1"
    assert record.fetched_at == NOW
    assert record.raw["salary"] == "CHF 90'000"
    assert record.raw["location"] == "Lausanne"


# --- healthcheck: three answers, at most one request -------------------------

@pytest.mark.asyncio
async def test_a_probe_asks_for_the_smallest_window_the_source_accepts():
    """"Does this endpoint answer?" is not "what is on it"."""
    search = RecordingSearch()
    source = a_query_source(search, a_metadata(
        "wtj", capabilities=frozenset({Cap.KEYWORD_SEARCH, Cap.HEALTHCHECK})))
    health = await source.healthcheck()
    assert search.calls == [("", "", PROBE_LOOKBACK_DAYS)]
    assert health.status is SourceHealthStatus.HEALTHY
    assert health.checked_at == NOW


@pytest.mark.asyncio
async def test_a_probe_that_fails_reports_the_outage():
    search = RecordingSearch(error=FetchError("https://example.ch: HTTP 429"))
    source = a_query_source(search, a_metadata(
        "wtj", capabilities=frozenset({Cap.HEALTHCHECK})))
    health = await source.healthcheck()
    assert health.status is SourceHealthStatus.UNAVAILABLE
    assert health.reason is SourceFailureCode.SOURCE_RATE_LIMITED


@pytest.mark.asyncio
async def test_a_source_that_claims_no_healthcheck_is_never_probed():
    """The four anti-bot endpoints: HEALTHY here means "nothing is known to be wrong".

    Probing LinkedIn would spend an unsolicited request to learn what the next sweep
    reports for free. A dashboard tells the two apart by reading
    `metadata.supports(HEALTHCHECK)`, which is why that claim is withheld on purpose.
    """
    search = RecordingSearch()
    source = a_query_source(search, a_metadata("linkedin", capabilities=SEARCHING))
    health = await source.healthcheck()
    assert search.calls == []
    assert health.status is SourceHealthStatus.HEALTHY
    assert health.latency_ms is None


@pytest.mark.asyncio
async def test_an_unset_credential_is_reported_without_a_probe(monkeypatch):
    """The third answer, and the one an operator can fix in five seconds."""
    monkeypatch.delenv("JOOBLE_API_KEY", raising=False)
    search = RecordingSearch()
    source = a_query_source(search, a_metadata(
        "jooble", capabilities=frozenset({Cap.HEALTHCHECK}),
        requires_credentials=True, credential_env_vars=("JOOBLE_API_KEY",)))
    health = await source.healthcheck()
    assert search.calls == []
    assert health.status is SourceHealthStatus.MISCONFIGURED
    assert "JOOBLE_API_KEY" in health.detail


# --- the three ATS boards ----------------------------------------------------

@pytest.mark.asyncio
async def test_an_ats_board_is_asked_once_per_configured_employer():
    """The plan is the operator's company list, not the request's keywords."""
    fetch = RecordingFetch()
    source = an_ats_source(fetch, Board("neocraft", "Neocraft SA"),
                           Board("acme", "Acme AG"))
    result = await source.discover(a_request(keywords=("dev", "data")))
    assert fetch.calls == [("neocraft", "Neocraft SA"), ("acme", "Acme AG")]
    assert result.metrics.requests_made == 2
    assert source.boards[0].company == "Neocraft SA"


@pytest.mark.asyncio
async def test_an_ats_board_with_no_employer_makes_no_request_and_says_nothing():
    """`config/companies.yaml` ships empty, and V1 is silent about it too.

    An adapter that warned on every run about a list the operator has not written
    yet would train them to ignore warnings. Nothing was asked, so nothing failed:
    HEALTHY with zero requests is the honest report.
    """
    fetch = RecordingFetch()
    result = await an_ats_source(fetch).discover(a_request())
    assert fetch.calls == []
    assert result.opportunities == ()
    assert result.health.status is SourceHealthStatus.HEALTHY
    assert result.metrics.requests_made == 0


@pytest.mark.asyncio
async def test_an_ats_probe_asks_the_first_employer_only():
    """One request answers "is this ATS reachable?" for every board on it."""
    fetch = RecordingFetch()
    source = an_ats_source(fetch, Board("neocraft", "Neocraft SA"),
                           Board("acme", "Acme AG"))
    health = await source.healthcheck()
    assert fetch.calls == [("neocraft", "Neocraft SA")]
    assert health.status is SourceHealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_an_unused_ats_is_not_an_outage():
    """Nothing to probe: the ATS is unused, not down (see `_probe`)."""
    fetch = RecordingFetch()
    health = await an_ats_source(fetch).healthcheck()
    assert fetch.calls == []
    assert health.status is SourceHealthStatus.HEALTHY


# --- the operator's company list ---------------------------------------------

def _companies(tmp_path, payload) -> object:
    path = tmp_path / "companies.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_the_company_list_is_read_in_the_operators_own_order(tmp_path):
    """V1's file, at V1's path, keyed by ATS — not a second list that can drift."""
    boards = load_v1_companies(_companies(tmp_path, {"greenhouse": [
        {"token": "neocraft", "company": "Neocraft SA"},
        {"token": "acme", "company": "Acme AG"}], "lever": []}))
    assert [(b.token, b.company) for b in boards["greenhouse"]] == [
        ("neocraft", "Neocraft SA"), ("acme", "Acme AG")]
    assert boards["lever"] == ()


def test_one_malformed_entry_does_not_cost_the_others(tmp_path):
    """A sweep of eleven other sources must not stop for one bad line."""
    boards = load_v1_companies(_companies(tmp_path, {"greenhouse": [
        {"token": "neocraft", "company": "Neocraft SA"},
        {"token": "orphan"},                     # no company
        {"company": "Nameless AG"},              # no token
        {"token": "", "company": "Blank SA"},    # empty token
        "not-a-mapping",
    ]}))
    assert [b.token for b in boards["greenhouse"]] == ["neocraft"]


def test_an_ats_key_that_is_not_a_list_is_skipped(tmp_path):
    boards = load_v1_companies(_companies(tmp_path, {"greenhouse": "neocraft",
                                                     "lever": []}))
    assert "greenhouse" not in boards
    assert boards["lever"] == ()


def test_a_missing_company_file_is_an_empty_list_and_not_a_crash(tmp_path):
    """An operator who never wrote the file gets V1's behaviour: no ATS postings."""
    assert load_v1_companies(tmp_path / "absent.yaml") == {}


def test_a_company_file_that_is_not_a_mapping_is_ignored(tmp_path):
    assert load_v1_companies(_companies(tmp_path, ["neocraft"])) == {}
