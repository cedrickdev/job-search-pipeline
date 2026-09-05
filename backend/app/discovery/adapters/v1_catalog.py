"""Which V1 module answers to which source key, and what it can honestly do.

This is the file the phase order's §8 has in mind when it says the registry must
not contain a hardcoded Swiss list: the list is *here*, in composition, and
`registry.py` never imports it. `bootstrap.py` calls `build_v1_sources()` once at
startup; the orchestrator only ever sees `OpportunitySource` objects.

**Every capability is claimed against a line of V1 code, not against a hope.**
That is §4's requirement ("assign only capabilities the implementation really
supports") and it is where most of the value of this file sits, because the
alternative — claiming what the *website* can do — makes the registry lie:

- `jobup` folds the location into its free-text `term`
  (`params={"term": f"{query} {location}"}`), so `LOCATION_SEARCH` is claimed and
  marked advisory. Leaving it out would stop the adapter sending the location and
  change V1's results; claiming it plainly would let a caller believe "Lausanne"
  was a filter.
- `migros`, `jobscout24` and `manpower` fetch one hardcoded listing URL and read
  neither argument, so they claim neither `KEYWORD_SEARCH` nor `LOCATION_SEARCH`
  and the adapter emits `KEYWORD_IGNORED` / `LOCATION_IGNORED` instead.
- `coop` is the only source whose location is a real filter — it is applied
  client-side on the canton attribute — while its own text relevance is loose
  enough (its docstring says so) that `KEYWORD_SEARCH` is the advisory one.
- Only `linkedin`, `indeed_ch` and `indeed` translate `lookback_days` into a
  request parameter (`f_TPR`, `fromage`), so only those three claim
  `INCREMENTAL_DISCOVERY`.
- Nothing claims `RADIUS_SEARCH`. `jobscout24`'s city page happens to aggregate a
  radius around Yverdon, but the radius is not a parameter, so it is not a
  capability (§11).
- Nothing claims `STRUCTURED_SALARY`. Two sources publish a real wage string
  (`jooble`'s `salary`, `indeed_ch`'s `salarySnippet`) and it is prose; three
  publish an activity rate in the same column, which is what `STRUCTURED_WORKLOAD`
  is for.
- Nothing claims `PAGINATION`. `linkedin` sends `start=0` and `wtj` asks for 50
  hits; no V1 module reads a next-page token, so no cursor can be minted.

`indeed` (fr.indeed.com) is registered too, with `countries=("FR",)`. V1 excluded
it with a nine-line comment inside its orchestrator; here the CH pack simply does
not bind it and the registry filters it out by country. It is the acceptance
criterion 8 demonstration that costs nothing: a source outside the swept country
is data, not an edit to the algorithm.
"""
from collections.abc import Mapping, Sequence
from typing import Final

from backend.app.discovery.adapters.v1_sources import (
    Clock,
    CompanyBoard,
    PackResolver,
    V1AtsSourceAdapter,
    V1FetchCallable,
    V1QuerySourceAdapter,
    V1SearchCallable,
    load_v1_companies,
    utc_now,
)
from backend.app.discovery.capabilities import SourceCapability
from backend.app.discovery.contracts import (
    OpportunitySource,
    RateLimitMetadata,
    SourceMetadata,
    SourceType,
)
from pipeline.sources import (
    ashby,
    coop,
    greenhouse,
    indeed,
    indeed_ch,
    jobscout24,
    jobup,
    jooble,
    lever,
    linkedin,
    manpower,
    migros,
    wtj,
)

# Local alias. This file names capabilities more often than every other module put
# together, and the fully qualified form turns each declaration into three lines
# of prefix.
Cap: Final = SourceCapability

# Priorities are *not* set here. A source's own `priority` stays at its default
# and the Country Pack orders the sweep (`country_packs/ch/sources.yaml`), because
# which board matters most is a country's judgement, not an implementation's.

# HEALTHCHECK is claimed where an unsolicited probe is cheap and welcome — a
# documented API, a JSON endpoint, a static page — and withheld from the four
# endpoints behind anti-bot protection (`jobup`, `linkedin`, `indeed`,
# `indeed_ch`). Probing those would spend a request that is likely to be refused
# for reasons unrelated to whether the source works, and the next sweep answers
# the same question for free. `V1SourceAdapter.healthcheck` documents what a
# caller sees in that case.
QUERY_SOURCES: Final[tuple[tuple[SourceMetadata, V1SearchCallable], ...]] = (
    (SourceMetadata(
        source_key="jobup",
        display_name="jobup.ch",
        source_type=SourceType.JOB_BOARD,
        countries=("CH",),
        capabilities=frozenset({Cap.KEYWORD_SEARCH, Cap.LOCATION_SEARCH}),
        advisory_capabilities=frozenset({Cap.LOCATION_SEARCH}),
        rate_limit=RateLimitMetadata(
            notes="Server-rendered page behind an AWS WAF; no published limit, so "
                  "none is declared rather than guessed."),
        notes="Reads the page's __INIT__ state blob; the location is folded into "
              "the free-text term and only affects ranking.",
    ), jobup.search_jobs),
    (SourceMetadata(
        source_key="indeed_ch",
        display_name="Indeed Switzerland (French)",
        source_type=SourceType.AGGREGATOR,
        countries=("CH",),
        capabilities=frozenset({Cap.KEYWORD_SEARCH, Cap.LOCATION_SEARCH,
                                Cap.INCREMENTAL_DISCOVERY}),
        notes="ch-fr.indeed.com. `q`, `l` and `fromage` are real request "
              "parameters, which is why the lookback window is honoured here.",
    ), indeed_ch.search_jobs),
    (SourceMetadata(
        source_key="jobscout24",
        display_name="JobScout24",
        source_type=SourceType.JOB_BOARD,
        countries=("CH",),
        capabilities=frozenset({Cap.STRUCTURED_WORKLOAD, Cap.HEALTHCHECK}),
        notes="One static per-city listing page (Yverdon-les-Bains); neither the "
              "keyword nor the location reaches the request. The activity rate is "
              "published as a tag and lands in V1's `salary` column.",
    ), jobscout24.search_jobs),
    (SourceMetadata(
        source_key="migros",
        display_name="Migros Vaud careers",
        source_type=SourceType.COMPANY_CAREER_SITE,
        countries=("CH",),
        capabilities=frozenset({Cap.STRUCTURED_WORKLOAD, Cap.HEALTHCHECK}),
        notes="One cooperative's vacancies, from a static listing page. A career "
              "site answering for a single employer, so it reads no keyword and no "
              "location; the percentage next to each title is the activity rate.",
    ), migros.search_jobs),
    (SourceMetadata(
        source_key="coop",
        display_name="Coop careers (Prospective)",
        source_type=SourceType.COMPANY_CAREER_SITE,
        countries=("CH",),
        capabilities=frozenset({Cap.KEYWORD_SEARCH, Cap.LOCATION_SEARCH,
                                Cap.STRUCTURED_WORKLOAD, Cap.DIRECT_APPLY_URL,
                                Cap.HEALTHCHECK}),
        advisory_capabilities=frozenset({Cap.KEYWORD_SEARCH}),
        notes="Public JSON API. The location is a genuine filter — applied on the "
              "canton attribute — while the API's own text relevance is loose "
              "enough that the keyword is a hint. `sza_apply_link` is where one "
              "applies, and `sza_pensum.max` is the activity rate.",
    ), coop.search_jobs),
    (SourceMetadata(
        source_key="manpower",
        display_name="Manpower Switzerland",
        source_type=SourceType.STAFFING_AGENCY,
        countries=("CH",),
        capabilities=frozenset({Cap.HEALTHCHECK}),
        notes="One static city listing page (Lausanne). The employer shown is "
              "usually the agency rather than the end client, which Phase 6's "
              "company canonicalization will have to account for.",
    ), manpower.search_jobs),
    (SourceMetadata(
        source_key="wtj",
        display_name="Welcome to the Jungle",
        source_type=SourceType.SEARCH_INDEX,
        capabilities=frozenset({Cap.KEYWORD_SEARCH, Cap.LOCATION_SEARCH,
                                Cap.HEALTHCHECK}),
        advisory_capabilities=frozenset({Cap.LOCATION_SEARCH}),
        notes="Public Algolia index. Keyword and location are concatenated into "
              "one query string, so the location reranks and does not filter. No "
              "country is declared: the index is international.",
    ), wtj.search_jobs),
    (SourceMetadata(
        source_key="linkedin",
        display_name="LinkedIn (guest search)",
        source_type=SourceType.JOB_BOARD,
        capabilities=frozenset({Cap.KEYWORD_SEARCH, Cap.LOCATION_SEARCH,
                                Cap.INCREMENTAL_DISCOVERY}),
        rate_limit=RateLimitMetadata(
            notes="Answers 403/429 to unauthenticated bursts. No published limit "
                  "to declare; expect DEGRADED or UNAVAILABLE sweeps."),
        notes="Guest endpoint, best-effort by nature. `f_TPR` carries the lookback "
              "window. Deliberately claims no HEALTHCHECK.",
    ), linkedin.search_jobs),
    (SourceMetadata(
        source_key="jooble",
        display_name="Jooble",
        source_type=SourceType.AGGREGATOR,
        capabilities=frozenset({Cap.KEYWORD_SEARCH, Cap.LOCATION_SEARCH,
                                Cap.HEALTHCHECK}),
        requires_credentials=True,
        credential_env_vars=("JOOBLE_API_KEY",),
        rate_limit=RateLimitMetadata(
            notes="Free API tier is quota-limited per key; the quota is not "
                  "published as a rate, so no number is declared."),
        documentation_url="https://jooble.org/api/about",
        notes="The only documented, key-based API in the set, and the only source "
              "that can report MISCONFIGURED. Its API key travels inside the URL, "
              "which is why `failures.redact_secrets` exists.",
    ), jooble.search_jobs),
    (SourceMetadata(
        source_key="indeed",
        display_name="Indeed France",
        source_type=SourceType.AGGREGATOR,
        countries=("FR",),
        capabilities=frozenset({Cap.KEYWORD_SEARCH, Cap.LOCATION_SEARCH,
                                Cap.INCREMENTAL_DISCOVERY}),
        notes="fr.indeed.com. Registered but outside every current pack: no CH "
              "sweep can select it, because it serves FR and Switzerland has no "
              "binding for it. See this module's docstring.",
    ), indeed.search_jobs),
)

# The ATS boards. `COMPANY_FILTER` is the capability that separates these three
# from the ten above: they answer for one employer per request, and the employers
# come from `config/companies.yaml` rather than from the search request.
ATS_SOURCES: Final[tuple[tuple[SourceMetadata, V1FetchCallable], ...]] = (
    (SourceMetadata(
        source_key="greenhouse",
        display_name="Greenhouse job boards",
        source_type=SourceType.ATS_BOARD,
        capabilities=frozenset({Cap.COMPANY_FILTER, Cap.ATS_METADATA,
                                Cap.DIRECT_APPLY_URL, Cap.HEALTHCHECK}),
        documentation_url="https://developers.greenhouse.io/job-board.html",
        notes="`absolute_url` is the board page carrying the application form, so "
              "DIRECT_APPLY_URL is claimed.",
    ), greenhouse.fetch_jobs),
    (SourceMetadata(
        source_key="lever",
        display_name="Lever job boards",
        source_type=SourceType.ATS_BOARD,
        capabilities=frozenset({Cap.COMPANY_FILTER, Cap.ATS_METADATA,
                                Cap.HEALTHCHECK}),
        documentation_url="https://github.com/lever/postings-api",
        notes="`hostedUrl` is the posting page; the application form lives one path "
              "below it, so DIRECT_APPLY_URL is not claimed.",
    ), lever.fetch_jobs),
    (SourceMetadata(
        source_key="ashby",
        display_name="Ashby job boards",
        source_type=SourceType.ATS_BOARD,
        capabilities=frozenset({Cap.COMPANY_FILTER, Cap.ATS_METADATA,
                                Cap.DIRECT_APPLY_URL, Cap.HEALTHCHECK}),
        documentation_url="https://developers.ashbyhq.com/reference/postingapi",
        notes="Publishes `compensationTierSummary`, which is human text rather than "
              "a range, so STRUCTURED_SALARY is not claimed.",
    ), ashby.fetch_jobs),
)


def source_metadata() -> tuple[SourceMetadata, ...]:
    """Every wrapped source's metadata, without building an adapter.

    Asking what a source can do should not require a pack resolver, a company list
    or a clock: the capability tests and the documentation table in
    docs/COUNTRY_PACKS.md read this, and a pack's `expects_capabilities` is checked
    against it at startup.
    """
    return (tuple(metadata for metadata, _ in QUERY_SOURCES)
            + tuple(metadata for metadata, _ in ATS_SOURCES))


def build_v1_sources(
    *,
    packs: PackResolver,
    boards: Mapping[str, Sequence[CompanyBoard]] | None = None,
    clock: Clock = utc_now,
) -> tuple[OpportunitySource, ...]:
    """The thirteen wrapped V1 sources, ready for `SourceRegistry.register_all`.

    `boards` defaults to `config/companies.yaml` — V1's own file, read once here —
    and is injectable so a test can hand over two fake employers without touching
    the operator's list, and so a future deployment can put that list in the
    database without this module changing.

    Nothing in here makes a request: constructing an adapter binds a callable and
    a piece of metadata, and every V1 module is imported at module scope anyway.
    """
    company_boards = load_v1_companies() if boards is None else boards
    sources: list[OpportunitySource] = [
        V1QuerySourceAdapter(metadata=metadata, search=search, packs=packs,
                             clock=clock)
        for metadata, search in QUERY_SOURCES
    ]
    sources.extend(
        V1AtsSourceAdapter(metadata=metadata, fetch=fetch,
                           boards=company_boards.get(metadata.source_key, ()),
                           packs=packs, clock=clock)
        for metadata, fetch in ATS_SOURCES
    )
    return tuple(sources)
