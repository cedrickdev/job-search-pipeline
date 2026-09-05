# V2 Country Packs and Source Plugins

Built in Phase 5. This document describes the layer that replaced
`core discovery code -> hard-coded Swiss sources` with:

```
country pack -> source registry -> capability-aware source plugins
```

Switzerland is the reference pack. Adding France, Germany or Cameroon later is a
new directory under `country_packs/` plus one registration call — no change to
the discovery algorithm.

## The invariant

No module in `backend/app/discovery/` names a job board, except
`adapters/v1_catalog.py` (which exists to name them) and `bootstrap.py` (which
composes). `country_packs/contracts.py` names no board at all: a pack refers to
sources by key.

`tests/test_v2_discovery_boundaries.py` enforces this by importing the modules and
inspecting them, so the invariant fails a test rather than a review.

V1 is untouched. The thirteen `pipeline/sources/*.py` modules are still the
transport, `pipeline/discover.py` still runs, and the SQLite database is still
there. Phase 5 wrapped V1; it rewrote nothing.

## Layout

```
backend/app/discovery/
  capabilities.py        SourceCapability: the 14 things a source can be asked for
  contracts.py           SourceMetadata, DiscoveryRequest/Result, SourceHealth,
                         DiscoveryWarning, DiscoveryMetrics, OpportunitySource
  failures.py            exception -> normalized health, and secret redaction
  normalization.py       a V1 posting row -> Opportunity, read through a pack
  registry.py            key -> source, and the selection rules
  requests.py            SearchProfile -> DiscoveryRequest
  orchestrator.py        one sweep: select, run concurrently, report
  bootstrap.py           composition; the only module that knows both halves
  adapters/v1_catalog.py which V1 module answers to which key
  adapters/v1_sources.py the two adapter shapes
backend/app/cli/discover.py  one sweep from the command line
country_packs/
  contracts.py           the typed pack: data and lookups, no behaviour
  loader.py              five YAML files -> one validated CountryPack
  registry.py            packs by ISO 3166-1 alpha-2 code
  errors.py              CountryPackErrorCode (11 members), one exception type
  ch/                    Switzerland: pack.py + five YAML files
```

Imports run one way only:

```
capabilities -> contracts -> {failures, normalization, registry, requests}
             -> orchestrator -> bootstrap -> adapters -> pipeline
```

## The pack contract

A pack is **data**. `country_packs/contracts.py` holds Pydantic models and pure
lookups; it contains no LLM call, no browser automation, no HTTP client and no
credential. Credentials are referenced by variable *name* only: `EnvVarName` is
constrained to `^[A-Z][A-Z0-9_]*$`, a shape no API key matches, so a pack file can
say "this source needs `JOOBLE_API_KEY`" without being able to hold its value.

Five YAML files, one per concern:

| File | Field | Holds |
| --- | --- | --- |
| `metadata.yaml` | `metadata` | ISO code, display name, default locale, locales, languages, currency, timezone, full-time weekly hours |
| `sources.yaml` | `sources` | which sources are enabled here, their priority and their expected capabilities |
| `opportunity_types.yaml` | `opportunity_types` | local vocabulary -> universal `OpportunityType` |
| `terminology.yaml` | `terminology` | workplace modes, contract types, languages, activity-rate labels |
| `eligibility.yaml` | `eligibility` | permit rules and which requirement kinds are in scope |

`load_pack(directory, expected_country="CH")` reads all five, validates them and
raises `CountryPackError` with a `CountryPackErrorCode`, the country and the
offending path on any problem: a missing file, malformed YAML, an unknown source
key, a duplicate binding, an unknown capability name, an unknown opportunity type,
an empty term mapping. Nothing is silently ignored (§16) and there are no defaults
for missing files — a pack either loads whole or fails loudly.

`CountryPackRegistry.register` refuses a second pack for the same country
(`COUNTRY_PACK_DUPLICATE_PACK`); `get` on an unknown country raises
`COUNTRY_PACK_NOT_FOUND` rather than returning `None`.

### What a binding may claim

A `SourceBinding` carries `source_key`, `enabled`, `priority`,
`expects_capabilities` and `config_env_vars`. `expects_capabilities` is a
**check**, not a claim: `verify_pack_expectations` (in `bootstrap.py`) compares it
against what the registered source actually declares and fails at startup if the
pack expects something the implementation does not support. A pack cannot grant a
capability, only assert one.

## The source contract

```python
class OpportunitySource(Protocol):
    @property
    def metadata(self) -> SourceMetadata: ...

    async def discover(self, request: DiscoveryRequest) -> DiscoveryResult: ...

    async def healthcheck(self) -> SourceHealth: ...
```

Three members, none of which mentions HTTP, HTML, Algolia, an ATS or a browser.
`pipeline/sources/greenhouse.py` reads a JSON API, `pipeline/sources/coop.py`
parses HTML, `pipeline/sources/welcometothejungle.py` posts to an Algolia index;
behind this Protocol they are indistinguishable.

Two properties are worth stating explicitly:

- **`discover` returns rather than raises.** A source reports its own failure as a
  `DiscoveryResult` carrying a non-HEALTHY `SourceHealth`, because that is the only
  shape that can also carry the postings it *did* collect. An adapter that lets an
  exception out is a bug, and the orchestrator contains it anyway (§14).
- **`metadata` is a property, not an attribute**, so that both adapter shapes
  satisfy it. An adapter storing a plain `self.metadata` type-checks against a
  property in a Protocol; the reverse is not true.

The Protocol is runtime-checkable and has a non-method member, so `isinstance`
works and `issubclass` raises `TypeError`. Tests assert with `isinstance`.

### Identity

`SourceMetadata.source_key` is identity: `^[a-z][a-z0-9_]*$`, at most 40
characters, stable across refactors. Identity is never derived from a class name
(§5) — `V1QuerySourceAdapter` serves ten different keys. Metadata also carries
`display_name`, `countries`, `capabilities`, `advisory_capabilities`,
`source_type`, `enabled`, `priority`, `rate_limit`, `requires_credentials`,
`credential_env_vars`, `documentation_url` and `notes`. It holds no secret.

### The two adapter shapes

`adapters/v1_sources.py` has one base class and two subclasses, because V1 has two
kinds of source module:

- `V1QuerySourceAdapter` wraps a `search(query, location, lookback_days)` function.
  `_plan` builds the cross product of *only the dimensions the source claims*: a
  source without `LOCATION_SEARCH` gets one request per keyword, not per pair.
- `V1AtsSourceAdapter` wraps a board-listing function driven by
  `config/companies.yaml`. With no board configured it makes **no request at all**
  and reports HEALTHY without a warning: an empty company list is a configuration
  state, not a failure.

Both bound the work: `MAX_REQUESTS_PER_SWEEP = 24` per source per sweep, with a
`LIMIT_TRUNCATED` warning when the plan is cut. V1 is synchronous, so each request
runs through `asyncio.to_thread`, sequentially within one source.

## Capabilities

`SourceCapability` has 14 members, in four groups:

- **query shape** — `KEYWORD_SEARCH`, `LOCATION_SEARCH`, `RADIUS_SEARCH`,
  `REMOTE_FILTER`, `OPPORTUNITY_TYPE_FILTER`, `COMPANY_FILTER`
- **traversal** — `PAGINATION`, `INCREMENTAL_DISCOVERY`
- **what comes back typed** — `STRUCTURED_SALARY`, `STRUCTURED_WORKLOAD`,
  `STRUCTURED_LOCATION`
- **downstream use** — `DIRECT_APPLY_URL`, `ATS_METADATA`, `HEALTHCHECK`

Two honesty rules govern the model (§4):

1. **Declare only what the implementation really does.** A capability is a promise
   the orchestrator relies on when it decides whether a request can be served, so
   an over-claim silently drops a user's filter.
2. **A source need not implement every capability.** Six of the fourteen —
   `RADIUS_SEARCH`, `REMOTE_FILTER`, `OPPORTUNITY_TYPE_FILTER`, `PAGINATION`,
   `STRUCTURED_SALARY` and `STRUCTURED_LOCATION` — are claimed by no wrapped V1
   source today. They exist because the *request* model can express them and a
   source must be able to say "not me", which is what produces a warning instead of
   a silently ignored filter.

Between "yes" and "no" there is a third answer: `advisory_capabilities`, for a
source that accepts the parameter and treats it as a ranking hint rather than a
filter. Welcome to the Jungle takes a location and returns results near it,
loosely; declaring `LOCATION_SEARCH` advisory keeps the adapter passing the value
(so V1's results do not change) while `CAPABILITY_ADVISORY_ONLY` tells the caller
the answer was not filtered.

### The 13 wrapped sources

Generated from `adapters/v1_catalog.source_metadata()`. `*` marks an advisory
capability. An empty country column means the source is not country-restricted and
any pack may bind it.

| Key | Type | Countries | Capabilities |
| --- | --- | --- | --- |
| `jobup` | JOB_BOARD | CH | KEYWORD_SEARCH, LOCATION_SEARCH* |
| `indeed_ch` | AGGREGATOR | CH | INCREMENTAL_DISCOVERY, KEYWORD_SEARCH, LOCATION_SEARCH |
| `jobscout24` | JOB_BOARD | CH | HEALTHCHECK, STRUCTURED_WORKLOAD |
| `migros` | COMPANY_CAREER_SITE | CH | HEALTHCHECK, STRUCTURED_WORKLOAD |
| `coop` | COMPANY_CAREER_SITE | CH | DIRECT_APPLY_URL, HEALTHCHECK, KEYWORD_SEARCH*, LOCATION_SEARCH, STRUCTURED_WORKLOAD |
| `manpower` | STAFFING_AGENCY | CH | HEALTHCHECK |
| `wtj` | SEARCH_INDEX | — | HEALTHCHECK, KEYWORD_SEARCH, LOCATION_SEARCH* |
| `linkedin` | JOB_BOARD | — | INCREMENTAL_DISCOVERY, KEYWORD_SEARCH, LOCATION_SEARCH |
| `jooble` | AGGREGATOR | — | HEALTHCHECK, KEYWORD_SEARCH, LOCATION_SEARCH |
| `indeed` | AGGREGATOR | FR | INCREMENTAL_DISCOVERY, KEYWORD_SEARCH, LOCATION_SEARCH |
| `greenhouse` | ATS_BOARD | — | ATS_METADATA, COMPANY_FILTER, DIRECT_APPLY_URL, HEALTHCHECK |
| `lever` | ATS_BOARD | — | ATS_METADATA, COMPANY_FILTER, HEALTHCHECK |
| `ashby` | ATS_BOARD | — | ATS_METADATA, COMPANY_FILTER, DIRECT_APPLY_URL, HEALTHCHECK |

## The registry

```python
registry.sources_for(country="CH", required_capabilities={SourceCapability.KEYWORD_SEARCH})
```

Four independent, subtractive filters:

- **country** — the adapter must serve it. A source declaring no country serves all
  of them, which is what makes `greenhouse` reusable by every pack.
- **pack** — when given, the country's bindings decide. A source the pack does not
  bind, or binds with `enabled: false`, is out even though it is registered and
  healthy: that is how an operator silences a board for one country only.
- **required_capabilities** — *reliably* supported, not merely claimed. An advisory
  claim is not an answer to "I need this filter applied", and treating it as one is
  how a search silently returns the wrong city.
- **source_keys** — the caller's allow-list, straight off
  `DiscoveryRequest.source_keys`.

A globally disabled source is skipped unless `include_disabled=True`. Nothing
matching returns an empty tuple rather than raising: an empty source set is a
legitimate outcome that the orchestrator turns into `NO_SOURCE_SELECTED`.

Ordering is deterministic: the pack's priority when it states one, the adapter's
otherwise, with `source_key` as tie-break — never registration order. Two runs of
the same sweep query the same sources in the same order, which is what makes a
truncating `limit` reproducible.

`register` refuses a duplicate key (`DUPLICATE_SOURCE`) and `get` on an unknown key
raises `UNKNOWN_SOURCE`. The two remaining codes belong to startup: at composition
time `verify_pack_expectations` rejects a pack that binds a source serving only
other countries (`COUNTRY_NOT_SERVED`) or that expects a capability the
implementation does not claim (`CAPABILITY_NOT_CLAIMED`). Both would otherwise look
like a binding that had taken effect.

The registry also holds the sweep's health view: `record_health`, `health_for`,
`health` and `unusable_source_keys`, all in deterministic key order. This is
**in-process only**, last-write-wins — Phase 5 adds no table, and a stale row would
be worse than no row.

## Composition

`bootstrap.build_discovery()` is the one call a process makes at startup. It returns
a `Discovery` NamedTuple — packs, registry, orchestrator — and the order it works in
matters: build the packs, register the sources, **verify**, and only then hand back
an orchestrator. A deployment either starts with a coherent configuration or does
not start.

`bootstrap.py` is the only module in `backend/app/discovery/` that imports both
`country_packs.ch` and `adapters/`, which is what keeps the dependency arrow one-way
everywhere else. Adding France is one line in `build_country_packs` plus a
`country_packs/fr/` directory.

`verify_pack_expectations` checks **enabled bindings only**. A disabled binding may
name a source that does not exist yet — that is how an operator parks a board they
intend to add, and refusing it would make the pack harder to write than the code it
configures.

## Running a sweep from the command line

`python -m backend.app.cli.discover` is the V2 counterpart of
`python -m pipeline.discover`, and the first caller that exercises the whole
architecture. It builds nothing itself: composition comes from `bootstrap`, the
sweep from the orchestrator, and the CLI owns only the arguments, the printed
report and the exit status.

Three modes:

- `--list-sources` — what is registered, what each source claims and in which order
  Switzerland sweeps them. Entirely offline.
- `--healthcheck` — probe the sources that welcome a probe, and report what the
  others already know.
- default — sweep, then print postings per source, the health of every source asked
  and every warning.

Exit status, so a scheduler can read one number: `0` the sweep is complete, `1` it
could not run (no pack for that country, or a composition error), `3` it ran but the
answer is not the whole market — a source was unavailable, a radius went unhonoured,
a limit truncated a board. `3` exists so that case cannot be read as `0`; hiding
failed source health is a documented non-goal.

Nothing it prints can carry a credential: `SourceHealth.detail` is composed from a
fixed vocabulary and redacted before it exists, and the only environment variable
the CLI prints is a *name*.

`python -m pipeline.discover` still works, unchanged.

## SearchProfile to DiscoveryRequest

`requests.discovery_requests_for(profile, country=…)` is the single mapper (§10).
There is no second preference model: the `SearchProfile` written in Phase 4 is the
input, and `countries_in_scope(profile)` says which countries to call it for.

For one country it returns one request covering all non-radius areas together — a
country-wide scope and a remote-only scope are the same query with different
post-filters — plus one request per radius area, because each centre is its own
search. A profile that does not reach the country yields an empty tuple: a real
answer the caller reports, rather than an invented country-wide sweep.

It lowercases source keys (`SourceKey` is lower-case by contract, while a profile
may have been typed by a human), drops blank keywords, clamps the lookback to
`MAX_LOOKBACK_DAYS = 7` and defaults to `DEFAULT_LOOKBACK_DAYS = 3` and
`DEFAULT_LIMIT = 100`. `DiscoveryRequest` itself enforces the outer bounds
(`lookback_days` 1–90, `limit` 1–1000) and refuses an incoherent pair: a
remote-only request that also excludes remote work.

## Orchestration

`sweep(request)` does four things: select through the registry, run the selected
sources concurrently, merge the results, and report. There is no
`run_jobup(); run_coop(); ...` anywhere — adding a source means wrapping it,
registering it and enabling it in a pack (§13).

A `SweepReport` carries the opportunities, merged `DiscoveryMetrics`, the health
records **sorted worst-first** so the operator reads the problems before the
successes, and `is_complete`. That flag is deliberately strict: it is true only when
no source was unusable, no source raised a warning and the sweep itself raised
none — the one question a caller must be able to ask before treating a sweep as the
state of the market. An empty selection is not a silent success either: the sweep
returns an explicit report carrying a `NO_SOURCE_SELECTED` warning.

## Concurrency and isolation

`MAX_CONCURRENT_SOURCES = 4`, enforced with an `asyncio.Semaphore` and an
`asyncio.gather`. Deliberately **not** a `TaskGroup`: a task group cancels its
siblings when one task raises, which is the opposite of §14 — one board being down
must not cost the results of the three that answered.

`_discover` catches `Exception`, never `BaseException`, so `CancelledError` and
`KeyboardInterrupt` still propagate and a sweep remains interruptible. Anything
else becomes a typed failure attributed to the source that produced it: the other
sources' results are untouched, and no partial state is written anywhere, because
an adapter returns its result instead of mutating shared state.

Within a source, requests are sequential and capped at `MAX_REQUESTS_PER_SWEEP`.
No Redis and no worker process were added for discovery (§14).

## Health

Four normalized statuses — `HEALTHY`, `DEGRADED`, `UNAVAILABLE`, `MISCONFIGURED` —
and `is_usable` is true for the first two: a degraded source returned real
postings, and half an answer is data.

`MISCONFIGURED` exists to separate "wait for the board to come back" from "set one
variable". Nine `SourceFailureCode` members say which: `SOURCE_UNAVAILABLE`,
`SOURCE_RATE_LIMITED`, `SOURCE_FORBIDDEN`, `SOURCE_NOT_FOUND`, `SOURCE_TIMEOUT`,
`SOURCE_MISCONFIGURED`, `SOURCE_PARSE_FAILED`, `SOURCE_PARTIAL_FAILURE`,
`SOURCE_ADAPTER_ERROR`. `SOURCE_PARSE_FAILED` is worth its own code because a board
that redesigned its page is a scraper to repair, not an outage that fixes itself;
`SOURCE_ADAPTER_ERROR` means "do not blame the board, this is our bug".

Every record carries `source_key`, `checked_at`, optional `latency_ms` (0 is a
measurement, `None` is absence) and, when not HEALTHY, a reason code. The model
enforces both directions: a non-HEALTHY record without a reason is invalid, and so
is a HEALTHY record with one — "healthy, because…" cannot be written.

### Secrets never reach a report

`pipeline/http_fetch.py` raises `FetchError(f"{url}: {last_error}")` and
`pipeline/sources/jooble.py` fetches `https://jooble.org/api/{key}`. The obvious
implementation of "report why the source failed" therefore writes a live API key
into a health record, and from there into whatever dashboard reads it.

Two defences in `failures.py`:

1. `classify_failure` reads an exception for its *shape* — an adapter-declared kind,
   a declared variable name, an HTTP status line, a timeout, a parse error — and
   composes `detail` from a **fixed table**. There is no code path from `str(exc)`
   to a report, and every `SourceFailureCode` must have a table entry or
   `classify_failure` raises.
2. `redact_secrets` runs over the composed sentence anyway: query-string
   credentials by parameter name, path credentials by character shape, and the
   value of every variable the source declared, read from the environment. It also
   collapses whitespace and truncates to `MAX_DETAIL_LENGTH`, so a detail is one
   short line and a traceback cannot arrive sideways.

### What `healthcheck()` means

Three answers, and the third needs care:

- a declared credential that is not set → `MISCONFIGURED`, **no request**;
- `HEALTHCHECK` claimed → one probe, and its outcome;
- `HEALTHCHECK` not claimed → `HEALTHY`, meaning *nothing is known to be wrong*,
  because no request was made.

`linkedin` is the deliberate case: probing a hostile endpoint spends an unsolicited
request to learn what the next sweep reports anyway. A caller that needs the
distinction reads `metadata.supports(HEALTHCHECK)`, and **a dashboard should render
that case as "not probed", not as a green light.**

## Warnings

A `DiscoveryWarning` is how a source says what it did *not* do. Eleven codes:

| Code | Says |
| --- | --- |
| `CAPABILITY_NOT_SUPPORTED` | the request asked for something this source cannot do |
| `CAPABILITY_ADVISORY_ONLY` | the parameter was passed but only ranks, it does not filter |
| `RADIUS_NOT_SUPPORTED` | see Radius below |
| `KEYWORD_IGNORED` | the keyword never reached the query |
| `LOCATION_IGNORED` | the location never reached the query |
| `LOOKBACK_IGNORED` | the source decides its own window |
| `CURSOR_IGNORED` | the source cannot resume |
| `LIMIT_TRUNCATED` | the plan was cut to stay within budget |
| `PARTIAL_RESULTS` | some requests failed; what came back is real but incomplete |
| `POSTING_SKIPPED` | a row could not be normalized into an `Opportunity` |
| `NO_SOURCE_SELECTED` | the sweep had nothing to run |

Warnings are attributed: a result may carry an unattributed warning or one naming
itself, never one naming another source. `_request_warnings` in the orchestrator
adds the sweep-level ones; the adapters add their own.

The distinction that matters for reading a report: a **warning** means the answer is
narrower than the question, a **failure** means there is no answer. V1's sources
cannot report which filters a board actually honoured, so a warning is the honest
form of "we asked, we cannot prove it applied".

## Radius

No wrapped source claims `RADIUS_SEARCH`, and Phase 5 does no geocoding (§11). The
policy is explicit and tested rather than implied:

- the radius **label** travels as a place name, because that is the only text a
  board can search on;
- the `RadiusConstraint` (centre + km) travels too, unhonoured, so the report can
  say so;
- the source emits `RADIUS_NOT_SUPPORTED`, and the sweep emits its own warning when
  the request carried a radius no selected source could serve;
- nothing is silently widened to a country-wide search and nothing pretends to have
  filtered by distance.

`RadiusConstraint.radius_km` is bounded to `0 < km ≤ 500`. Phase 7 owns real radius
semantics, in the database, against geocoded coordinates.

## Workload

A Swiss posting states an activity rate: "Vendeur/euse 60-80%". `parse_workload`
turns that into a `WorkloadRange(min_percent, max_percent)` — and reads it only
from fields entitled to state one:

| Field | Trusted when |
| --- | --- |
| title | always — a title is a curated phrase, not prose |
| salary column | the source claims `STRUCTURED_WORKLOAD`, or the text contains one of the pack's `activity_rate_labels` |
| contract column | the text contains an activity-rate label |
| description | never |

The description is excluded because "20% de rabais collaborateur" is a staff
discount, and a naive percentage scan would file it as a part-time role.

`min_weekly_hours` is deliberately **not** derived from
`PackMetadata.full_time_weekly_hours`. For CH that value is `42.0`, which is an
operator's convention rather than a legal figure; converting "80%" into "33.6 h"
here would publish the convention as a fact the employer never stated. A consumer
that wants hours has the pack and can call `weekly_hours_for_percent` itself.

## Eligibility

Phase 5 provides **hooks and data only** — no engine, no verdict. Phase 9 owns
matching and eligibility.

`eligibility.yaml` declares `requirements_in_scope` (for CH:
`WORK_AUTHORIZATION`, `PERMIT_HOURS_CAP`, `MINIMUM_AGE`, `LANGUAGE_MINIMUM`) and one
`PermitRule` per permit — CH, C, B, B_STUDENT, L, G — each mapping to a
`WorkAuthorizationStatus` and optionally to an hours cap. `B_STUDENT` carries
`15.0` weekly hours.

`EligibilityMetadata` validates that every requirement kind declared in scope is
actually backed by data, so a pack cannot claim to handle a requirement it has no
rules for.

**These figures are operator-maintained configuration, not legal advice.** They are
plausible defaults for the reference pack and must be reviewed against current
cantonal and federal rules before anyone relies on them.

## Switzerland, the reference pack

`country_packs/ch/` — code `CH`, display name Switzerland, default locale `fr-CH`,
locales `fr-CH`/`de-CH`/`it-CH`/`en-CH`, currency `CHF`, timezone
`Europe/Zurich`, `full_time_weekly_hours: 42.0`.

Twelve source bindings, all enabled, priorities from 10 to 82 (lower runs first):
`jobup`, `indeed_ch`, `jobscout24`, `migros`, `coop`, `manpower`, `wtj`,
`linkedin`, `jooble`, `greenhouse`, `lever`, `ashby`. `indeed` is deliberately
**absent** — it is the FR-facing aggregator, and binding it here would sweep the
wrong market. `jooble` declares `config_env_vars: [JOOBLE_API_KEY]`: the name, never
the value.

The vocabulary, all four languages in one file per concern: **62** opportunity-type
terms mapping onto **9** universal `OpportunityType` members (APPRENTICESHIP,
FREELANCE, FULL_TIME, GRADUATE, INTERNSHIP, PART_TIME, STUDENT_JOB, TEMPORARY,
WORK_STUDY), **17** workplace-mode terms, **22** contract-type terms, **18**
language terms and **9** activity-rate labels. Eligibility adds **6** permits and a
minimum working age of 15.

### Universal enum, local words

`WORK_STUDY` stays the universal type (§2). *Alternance* is not added to the enum
because one country says it that way; it is a term in `opportunity_types.yaml` that
maps onto `WORK_STUDY`. The same holds for *apprentissage*, *stage*, *emploi
étudiant*, *temporaire*, *taux d'activité* and permits B/C: Swiss words live in the
pack, and the generic domain layer never learns them.

Matching is punctuation- and accent-folded and whole-term based, so
`Lehrstelle`, `lehrstelle` and `Lehrstelle/Apprentissage` all classify, while
`stage` inside `stagediagnostik` does not.

## Adding a source

1. Implement the transport, or keep an existing V1 module as the transport.
2. Declare `SourceMetadata`: a stable `source_key`, the countries it serves, and
   **only** the capabilities it really honours — advisory ones separately.
3. Register it in `adapters/v1_catalog.py` (for a V1 module) or in your own catalog
   consumed by `build_source_registry`.
4. Bind it in the packs that should use it, with a priority and, if you want the
   startup check, `expects_capabilities`.
5. Add tests: normalization of a fixture payload, the warnings it emits for
   parameters it cannot honour, and its three `healthcheck` answers. **No live
   network calls in unit tests** (§18).

Nothing in `orchestrator.py`, `registry.py` or `contracts.py` changes.

## Adding a country

1. Create `country_packs/xx/` with `pack.py` and the five YAML files.
2. Fill the vocabulary in the languages actually used there, and map every local
   term onto an existing universal `OpportunityType`. If a genuinely new *universal*
   category appears, that is a domain change and its own discussion.
3. Bind the sources that serve the country — including existing country-neutral
   ones such as the three ATS boards.
4. Register the pack in `build_country_packs`.
5. Declare the new package and its YAML in `pyproject.toml` —
   `[tool.setuptools.packages] packages` and
   `[tool.setuptools.package-data] "country_packs.xx" = ["*.yaml"]`. Without the
   second line the wheel ships the loader without its content and `load_pack`
   raises `COUNTRY_PACK_FILE_MISSING` from an installed environment while working
   perfectly from a source checkout.
6. Add tests mirroring `tests/test_v2_country_packs.py`.

Again: no orchestration change. That is acceptance criterion 8, and
`tests/test_v2_discovery_boundaries.py` is what keeps it true.

## Provenance

V1's provenance is preserved, not flattened (§15). Every `Opportunity` carries an
`OpportunitySourceRecord` with `source_key`, `external_id`, `source_url`,
`fetched_at` and `raw` — a snapshot of every column the source returned that was
*not* copied into a typed field, under the source's own column names. Normalization
is lossy and revisable, so the un-normalized text has to survive for traceability
and for re-running a better parser later.

`raw` holds public posting metadata only; no credential, cookie or session token
goes near a domain object.

Identity is derived, not random: `discovered_opportunity_id` is a `uuid5` of
`opportunity:{source_key}:{external_key}`, where `external_key` is the source's own
id when it publishes one and the URL otherwise. A sweep that runs twice an hour
therefore meets the same posting and produces the same id, instead of turning one
vacancy into twelve rows a day. Cross-source deduplication is a different question
answered by `dedup_fingerprint` (company + title), which V1 already computes and
Phase 6 will sharpen.

A posting that cannot be normalized is skipped with a `POSTING_SKIPPED` warning and
counted in `DiscoveryMetrics.postings_skipped`; it never becomes a half-built
`Opportunity`.

## Tests

```
tests/test_v2_country_packs.py           loading, validation, terminology, mapping
tests/test_v2_source_capabilities.py     the capability model and SourceMetadata
tests/test_v2_source_registry.py         selection by key, country, capability, priority
tests/test_v2_discovery_contracts.py     the request/result/health validators
tests/test_v2_discovery_adapters.py      each wrapped source through the contract
tests/test_v2_discovery_normalization.py posting row -> Opportunity, through a pack
tests/test_v2_discovery_orchestration.py sweeps, isolation, warnings, empty selection
tests/test_v2_discovery_health.py        classification, redaction, the four records
tests/test_v2_discovery_boundaries.py    the invariant at the top of this document
tests/test_v2_domain_purity.py           the domain layer imports no framework
```

```bash
.venv/bin/python -m pytest -q tests/test_v2_country_packs.py \
  tests/test_v2_source_capabilities.py tests/test_v2_source_registry.py \
  tests/test_v2_discovery_*.py tests/test_v2_domain_purity.py
```

No test in this set touches the network. Sources are driven by fixtures and fakes,
and the one end-to-end secret-leak test raises a real V1 `FetchError` carrying a
key-shaped URL through a real adapter and asserts the serialized result contains
neither the key nor the host.

## Known risks

- **No per-source timeout.** A V1 request that hangs holds its concurrency slot
  until `requests`' own socket timeout fires. The semaphore bounds fan-out, not
  duration; a deadline per source is the natural follow-up.
- **`full_time_weekly_hours: 42.0` is a convention**, not a legal figure, and
  anything derived from it inherits that status.
- **Health is in-process only.** `record_health` is not persisted, so a restarted
  process has no memory of the last sweep's outages and no trend is available.
- **Truncation is coarse.** `MAX_REQUESTS_PER_SWEEP = 24` cuts the *plan*, and the
  `LIMIT_TRUNCATED` warning says it happened but not which keyword/location pairs
  were dropped.
- **V1 fragility is inherited.** The wrapped modules remain the transport, so a WAF
  on jobup or a 403/429 from LinkedIn still ends in `UNAVAILABLE` — now reported
  per source instead of failing the run, which is the improvement Phase 5 makes.
- **Redaction is heuristic for unknown shapes.** A credential that is pure letters
  and short is caught only through the declared `credential_env_vars` pass, which is
  why declaring them matters.
- **mypy cache order.** Run the strict gate on a clean cache
  (`rm -rf .mypy_cache && .venv/bin/mypy backend country_packs`). A preceding bare
  `mypy` run populates the shared cache through `follow_imports = "silent"` and
  replays V1 errors into the strict run. CI runs the gates in an order where this
  does not arise, so no configuration was changed.

## Not in Phase 5

| Concern | Phase |
| --- | --- |
| Company discovery, ATS detection from arbitrary career sites, company canonicalization | 6 |
| Geocoding and database-backed radius semantics | 7 |
| Map UI | 8 |
| Matching and the eligibility engine that consumes the pack's metadata | 9 |
| Provider-neutral LLM platform | 11 |
| Countries beyond Switzerland, each as its own gated delivery | 17 |

Phase 5 also added no Redis, no worker process and no new database table: discovery
runs in the API process, and persistence of sweep results stays where Phase 2 put
it.
