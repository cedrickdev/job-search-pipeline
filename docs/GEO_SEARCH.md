# V2 Geo Opportunity Explorer

Built in Phase 7. This document describes the layer that lets a search be a
*place*:

```
saved SearchAreas ─┐
API query string  ─┼─> GeoSearchQuery ─> PostGIS (ST_DWithin / ST_Distance)
                   │                       └─> OpportunityGeoResult / CompanyGeoResult
Location (no point) ─> Geocoder port ─> GeocodedPlace ─> enriched Location
                        └─> geocoding_cache (provider-aware, failures expire)
```

The point of the phase in one sentence: **"I cannot tell where this is" is a
value, not a crash and not a false 0 km**. A posting whose location is the string
`"Lausanne"` and nothing else is returned from a geo search, flagged
`UNRESOLVED`; a posting with no coordinates of its own is drawn at its employer's
office and flagged `COMPANY_FALLBACK`; a remote posting is `REMOTE`. A nullable
distance could not tell those three apart, and §12 requires that it must.

## The invariant

Five claims, each held by a test rather than by review:

- **Distance is PostGIS's answer, never Python's.** Nothing in
  `backend/app/domain/geo.py` or `backend/app/geo/` computes a distance.
  `GeoSearchQuery` describes what to ask; `ST_DWithin` decides membership and
  `ST_Distance` produces the number, from the same spheroid — so a query for
  everything within 100 km can never return a row shown as 101 km away (§15). A
  Haversine helper would immediately be a second, disagreeing answer.
- **Remote is a policy, never an inference.** A `RemotePolicy` has to be stated;
  `RemoteScope` is derived and deliberately reluctant to reach `REMOTE_ANYWHERE`.
  Bare "Remote" is not read as worldwide eligibility (§10). Whether the candidate
  may legally take a role is `WorkAuthorization`'s question and Phase 9's answer.
- **No candidate location is ever geocoded (§33).** The `LocationStore` seam has
  no method that reaches `candidate_profiles`; the SQL behind it selects from
  `opportunities` and `company_locations` only; `geocoding_cache` has no
  `user_id` and no route by which one could arrive. A home address normalized
  into a shared cache key is the leak, and the way to prevent it is to remove the
  path to it.
- **A geocoder never raises for a provider problem.** A timeout, a 502, a body
  that is not JSON and an empty result set are all a `GeocodingResult`, so one
  unreachable provider does not abandon the other 199 addresses in a batch (§4).
- **A geocoder carries no credential value.** `GeocoderSettings.api_key_env`
  holds the *name* of an environment variable, constrained to `^[A-Z][A-Z0-9_]*$`
  — a real token has lower-case letters, dashes or dots and simply will not fit,
  which makes "do not commit a key" a validation error rather than a review
  comment.

V1 is untouched, and so is the persisted shape of a `SearchArea`: the saved
representation stays the three-member discriminated union Phase 1 defined, and
everything the engine needs is *derived* from it.

## Layout

```
backend/app/domain/geo.py           the search vocabulary: query, policy, status,
                                    and the geocoding request/result pair
backend/app/geo/
  contracts.py    the Geocoder port, GeocoderSettings, the HttpGet seam, cache entry
  nominatim.py    the one adapter — OpenStreetMap Nominatim over HttpGet
  caching.py      CachingGeocoder (decorator) + InMemoryGeocodingCache
  failures.py     provider-problem → a safe, fixed-vocabulary detail string
  bootstrap.py    composition; the only module that names a provider class
backend/app/services/geo_search.py       the read side: three searches, one service
backend/app/services/geo_enrichment.py   the write side: one bounded resolving pass
backend/app/cli/geocode.py                `python -m backend.app.cli.geocode`
backend/app/repositories/
  sqlalchemy_repositories.py              search_geo on the opportunity/company repos
  sqlalchemy_location_store.py            the enrichment LocationStore, two tables only
  sqlalchemy_geocoding_cache.py           the geocoding_cache repository
backend/app/api/routes/geo.py             three GETs
backend/app/api/schemas.py                the geo query models and response models
backend/migrations/versions/rev_0005_phase_7_geo_search.py
```

Imports run one way only: `domain/geo` ← `geo/contracts` ← the adapters and
`geo/bootstrap`; the services depend on the domain and the repositories, and none
of the domain depends on a service.

## The query

`GeoSearchQuery` is everything a geo repository needs, as one validated value
(§14). One object rather than a method with a dozen parameters, for two reasons
beyond readability: it makes the one-area API request and the multi-area saved
profile *the same shape*, so §27's "areas combine as OR, without duplicates" is
one implementation; and it can reject a contradiction where it is built rather
than producing an empty page nobody can explain.

The clauses combine deliberately:

| Clause | Combines as | Meaning |
| --- | --- | --- |
| `radii` | OR | inside any disc — `ST_DWithin` on a geography column |
| `countries` | OR | `location_country = 'CH'` against the canonical code, never a substring on a display string (§13) |
| remote branch | OR | under `INCLUDE_REMOTE`/`REMOTE_ONLY`, remote postings admitted by `remote_countries` |
| `bounds` | AND | a viewport narrows the union; it is the part of the result the screen can show, not another place to look |
| `opportunity_types`, `workplace_modes` | AND | ordinary narrowings within the union |

Validation (`_the_query_asks_for_somewhere`) refuses three shapes:

- **no scope** — no radius, no country, no bounds, and not `REMOTE_ONLY` — because
  a full-table scan is not a search;
- **`REMOTE_ONLY` beside geography** — remote work has no distance to a centre, so
  a radius, a country or a viewport beside it contradicts the policy (§3) rather
  than narrowing it;
- **`EXCLUDE_REMOTE` with `remote_countries`** — narrowing a branch the policy does
  not have.

`geo_query_for_areas` maps saved `SearchArea`s onto one query and is the only
place the remote policy is *judged*: all areas remote → `REMOTE_ONLY`; a remote
area beside a geographic one, or `workplace_modes` naming `REMOTE` →
`INCLUDE_REMOTE`; otherwise `EXCLUDE_REMOTE`. `remote_countries` collects the
countries the remote-only areas named and stays empty as soon as one named none —
an "anywhere" area must not be narrowed by a sibling that says "remote, in CH".

## Remote

`RemotePolicy` (`EXCLUDE_REMOTE` / `INCLUDE_REMOTE` / `REMOTE_ONLY`) is what the
search *wants*. `RemoteScope` is how far a posting actually *reaches*, derived by
`remote_scope_of` from the workplace mode and location so it cannot drift from
them:

| Scope | When |
| --- | --- |
| `HYBRID` | `HYBRID` mode — keeps a geographic anchor, judged by distance like on-site (§11) |
| `REMOTE_REGION_RESTRICTED` | `REMOTE` mode naming a city, region or postal code |
| `REMOTE_COUNTRY_RESTRICTED` | `REMOTE` mode naming only a country |
| `REMOTE_ANYWHERE` | `REMOTE` mode naming no geography at all |
| `None` | on-site, or a mode that is not remote |

Reading a city on a remote posting as a *regional* restriction is the
conservative direction and the intended one: boards print the hiring office's
city on remote roles, and treating that as less information than it is would be
the §10 mistake.

## Status and the company fallback

`GeoStatus` explains the distance a result has, or why it has none:

| Status | Meaning |
| --- | --- |
| `RESOLVED` | the posting geocoded its own coordinates |
| `COMPANY_FALLBACK` | no point of its own; placed at the employer's nearest site (§9, §31) — a weaker claim a map must not draw as a geocoded pin |
| `REMOTE` | remote; distance does not apply |
| `UNRESOLVED` | a location with text but no point — returned, not dropped, and not claimed to be 0 km away (§12) |

The fallback is computed in the query: `search_geo` outer-joins each posting to
its company's *nearest* site (the closest to a search centre when the query has
radii, otherwise the headquarters), and a posting with no point of its own
inherits that site's coordinates and distance, flagged `COMPANY_FALLBACK`. A
posting with its own point ignores the join.

## The geocoding port

```python
class Geocoder(Protocol):
    @property
    def provider(self) -> str: ...
    async def geocode(self, request: GeocodingRequest) -> GeocodingResult: ...
    async def probe(self) -> GeocoderProbe: ...
```

`runtime_checkable`, structural, with a non-method member — so `isinstance`
works and the contract tests assert conformance the way they do for
`OpportunitySource`. There is **no `reverse_geocode`** (nothing asks a coordinate
what its address is) and **no batch method** (a provider free to implement one
privately should not force every adapter to write a loop).

The vocabulary both sides speak lives in `domain/geo.py`:

- `GeocodingRequest` — free text plus two hints, `country` and `language`, the
  intersection of what Nominatim, Google, Mapbox and HERE all accept. `country`
  is a real narrowing: "Lausanne" with `country="CH"` has one answer where the
  bare string is `AMBIGUOUS`.
- `GeocodingOutcome` — `MATCHED` / `AMBIGUOUS` / `NOT_FOUND` / `FAILED`.
  `AMBIGUOUS` (two `Lausanne`s) forbids turning the first hit into truth (§6) and
  requires ≥2 alternatives; `NOT_FOUND` and `FAILED` are kept apart because only
  `FAILED` (the absence of an answer) may be cached with an expiry.
- `GeocodedPlace` — point, formatted address, precision, confidence, the coarse
  components, and two provider identifiers. **No `raw_metadata` bag**: §6's "store
  only useful provenance" taken literally, so a provider blob — with the licence
  text, the four display names and possibly the request headers — has nowhere to
  land. `to_location` is the one conversion that stamps `GEOCODED` provenance,
  the geocoder key and the timestamp together, and carries the original `raw`
  through unchanged (§8: `Lausanne, VD, CH` is the evidence a re-run is judged
  against).
- `GeocodingResult` validates the payload against the outcome, so an adapter
  cannot return `MATCHED` with nothing or `AMBIGUOUS` with one candidate.

`geocoding_query_for(location)` decides what text to send, deterministically:
`raw` when the source gave one (a street and number survive nowhere else),
otherwise the structured components coarse-to-fine. A location with only
coordinates returns `None` — nothing to ask.

The one adapter is `NominatimGeocoder` over the `HttpGet` seam
(`(url, params) -> (status, json)`). A test injects a function returning a canned
payload and exercises parsing, classification and precision mapping end to end
with no live service and no `respx`.

## The enrichment pass

`GeoEnrichmentService.run(limit, country)` is the only place in Phase 7 that
resolves a location (§21). It walks a bounded batch of coordinate-less rows, asks
the port about each, and writes back the ones it can improve:

- **§7 — a stronger coordinate is never replaced.** A row that already carries a
  point is skipped, not re-asked; `Location.outranks` decides, so `MANUAL` and
  `SOURCE_PROVIDED` beat geocoded, and between two geocoded points confidence
  decides. This is what makes a second pass a no-op (§39) rather than a slow way
  to degrade data.
- **§22 — it runs when somebody runs the command.** Nothing is wired to startup, a
  scheduler or a request. A crash leaves untouched rows exactly as they were,
  because writes go through the caller's transaction.
- **The report names rows by id and table, never by address text** — a report gets
  pasted into tickets, and an address is candidate-adjacent data.

The CLI is `python -m backend.app.cli.geocode`:

| Flag | Effect |
| --- | --- |
| `--provider KEY` | provider key (default `nominatim`) |
| `--country XX` | ISO-3166-1 hint sent with each query (default `CH`) |
| `--limit N` | maximum rows to enrich (default 50) |
| `--list-providers` | print availability and exit without a request |
| `--healthcheck` | validate configuration without a request |

Exit codes: `0` complete, `1` unusable configuration (a bad or missing provider —
prints the error, makes no request), `3` incomplete (at least one `FAILED`,
i.e. retry after fixing availability). Configuration is environment-only —
`GEOCODER_USER_AGENT` (required; Nominatim's terms demand a distinguishing
User-Agent), `GEOCODER_CONTACT_EMAIL` (optional), `GEOCODER_API_KEY` (optional,
and the *name* of another variable, never the key). See `.env.example`.

## The cache

`CachingGeocoder` decorates any `Geocoder`; the CLI backs it with
`SqlAlchemyGeocodingCacheRepository` over the `geocoding_cache` table so a nightly
run does not ask a rate-limited public geocoder the same thousand questions every
night (§23). Adapter tests use `InMemoryGeocodingCache` instead — same protocol.

The key is `(provider, country_hint, normalized_query)`.
`normalize_geocoding_query` is far gentler than `normalize_company_name` — it
case-folds and collapses whitespace and nothing else, because dropping
punctuation would fold `Route 9` into `Route9`. `country_hint` is `''` (not NULL)
for "no hint", so the common unhinted question cannot be stored twice under
PostgreSQL's distinct-NULLs rule. Only a `FAILED` entry expires; every answered
outcome is permanent, because an address does not move and re-asking spends a
budget on questions already answered.

## Persistence

Revision `0005`, `revises 0004`, additive throughout. The geography columns and
their four GiST indexes were created back in revision `0002`, precisely so this
phase would not alter a table under load. What `0005` adds is everything *around*
the coordinates:

- **Five columns on each of the three located tables** (`opportunities`,
  `company_locations`, `candidate_profiles`): `location_provenance`,
  `location_precision`, `location_confidence`, `location_geocoder`,
  `location_geocoded_at`. The two enum columns are `NOT NULL` with a server
  default (`SOURCE_PROVIDED` / `UNKNOWN`), which is what makes the revision
  non-rewriting: PostgreSQL 11+ fills an added `NOT NULL` column from its default
  without touching the heap, so the backfill *is* the `ADD COLUMN`. Every
  pre-existing row means "the source provided what is there, nothing claims a
  precision" — which is exactly that default.
- **A coherence CHECK per table** (`ck_<table>_location_provenance_coherent`), the
  domain validator `Location._the_provenance_describes_coordinates_that_exist`
  restated: a precision needs a point, a `GEOCODED` row needs a point and a
  geocoder, and only a `GEOCODED` row may carry geocoding metadata at all — the
  last clause matters most, because that metadata can veto a later write, and a
  stale confidence would silently make a location unimprovable.
- **Two btree indexes on `location_country`** (`opportunities`,
  `company_locations`) for the whole-country branch, which is not spatial and so
  GiST cannot serve. Honestly stated in the migration: on a single-country
  dataset PostgreSQL correctly ignores them; they earn their keep once a second
  Country Pack exists. Nothing is indexed for `workplace_mode` — no dataset makes
  a three-member enum selective.
- **`geocoding_cache`** — id (uuid5 over the three key values, so a re-ask updates
  rather than inserts), the key columns, `outcome`, `place`/`alternatives` as
  JSONB, `detail`, `checked_at`, `expires_at`, timestamps, and named CHECKs
  including `only_a_failure_expires` written as an equivalence
  (`(outcome = 'FAILED') = (expires_at IS NOT NULL)`) so both halves of §23 hold
  at once. No `user_id`.

Enums are `VARCHAR(32)` + CHECK, never a native PostgreSQL `ENUM`: widening a
CHECK is an ordinary migration; adding a member to a native enum is DDL that
cannot share a transaction with a table rewrite.

`downgrade` drops the cache and the provenance columns but keeps every point — a
posting geocoded in Phase 7 survives as the coordinates 0002's columns already
held. The provenance is what is lost, so a re-upgrade re-runs the enrichment pass
to say who produced them.

## The API

Three GETs, all behind an authenticated session — the geo surface exposes the
whole shared corpus, and a public map is a Phase 8 decision nobody has made:

| Operation | Scope |
| --- | --- |
| `GET /api/v2/geo/opportunities` | postings admitted by one query, closest first |
| `GET /api/v2/geo/companies` | employers with a site in scope, each once, at their nearest match |
| `GET /api/v2/me/search-profiles/{id}/opportunities` | one saved search run as a geo query |

**The query is a model, not a fistful of `Query(...)` parameters.**
`GeoSearchParams` validates the whole query string in one place and builds the
domain `GeoSearchQuery` once (`model_validator(mode="after")`), so a contradiction
the domain forbids is a 422 *where the request was parsed* rather than an empty
page. The parameters:

- `radius` — repeatable, `lat,lng,km[,label]`;
- `bounds` — `north,south,east,west` (a viewport);
- `country`, `opportunity_type`, `workplace_mode` — repeatable enums/codes;
- `remote` — `exclude` (default) / `include` / `only`, mapped to `RemotePolicy`;
- `remote_country` — repeatable;
- `limit` (1–200, default 50) and `offset` — bounded at the signature, so an
  out-of-range value names the parameter rather than being silently clamped.

A malformed `radius` or `bounds` is a **redacted** 422 (`{type, loc, msg}` only):
the rejected value is never echoed, so a bad coordinate cannot be reflected back
(§Security). The window echoed in the body is the one the query validated, so a
client pages against the `limit`/`offset` it sent (§17).

The saved-search route takes only `bounds` (the viewport Phase 8 sends when the
map pans), `limit` and `offset`; its scope is the profile's areas. It does not
check ownership itself — it hands the id and the session user to the service,
whose repository scopes the load, and a profile belonging to another account
comes back absent and becomes the same 404 as one that never existed
(§Security: ids must not be enumerable) — the arrangement `me.py` already uses.

An anonymous caller is refused with 401 *before* the query is parsed, because
FastAPI resolves the session dependency first: a missing cookie short-circuits a
malformed radius, so the surface never leaks which queries are well formed.

## Sharing

The two open reads are over the **shared corpus**, like the company directory
(§Sharing there): authenticated because the surface is guarded, but not
owner-scoped, because a posting near Lausanne is the same fact for every account.
The saved-search read *is* owner-scoped — a profile is user-owned data. Neither
`geocoding_cache` nor any located shared table has a `user_id`, and the
enrichment pass never reads a candidate profile, so no candidate address enters a
shared row or a shared cache key.

## Tests

No test in the default suite touches a live geocoder or a live job board. The
Nominatim adapter is exercised through the `HttpGet` seam with canned payloads;
the search and enrichment services run against fakes; the migration and schema
tests run against a real PostgreSQL/PostGIS.

| Suite | Covers |
| --- | --- |
| `tests/test_v2_geo_search_repositories.py` | `search_geo` over PostGIS — radius/country/bounds/remote combinations, the company fallback, matched radii, ordering, distance from the same predicate |
| `tests/test_v2_api_geo.py` | the three endpoints — auth required, the query-model 422s (redacted, no echo), pagination, the cross-user 404 on the saved-search read |
| `tests/test_v2_geocoding_cli.py` | the CLI — provider composition, `--healthcheck`, `--list-providers`, the exit codes, configuration errors |
| `tests/test_v2_persistence_migrations.py` | upgrade 0004→0005, the server-default backfill, a clean run to head, the rollback that keeps points |
| `tests/test_v2_persistence_schema.py` | the added columns, the coherence CHECKs, the cache table and its constraints, no `user_id` on a shared table |
| `tests/test_v2_api_surface.py` | the three geo operations under the prefix, authenticated, safe, carrying no credential field |

## Known risks

- **The enrichment pass is single-provider.** Only `nominatim` is registered. The
  port and the settings are provider-neutral, but adding Google or Mapbox is a new
  adapter, not a configuration change — deliberately, since the phase needs only
  one to prove the seam.
- **`COMPANY_FALLBACK` is only as good as the site list.** A posting placed at its
  employer's nearest office is on the map at a real place, but not *its* place; a
  company with no located site leaves its coordinate-less postings `UNRESOLVED`.
- **Ambiguity is left to the operator.** `AMBIGUOUS` and `NOT_FOUND` are recorded
  as outcomes and the row stays unresolved; nothing yet lets someone disambiguate
  from a UI, so a genuinely ambiguous address needs a re-run with a narrower
  `--country` or a manual point.
- **`FAILED` entries are not swept.** A failure expires so it can be re-asked, but
  nothing deletes an expired row; the table grows by the count of distinct failed
  questions until a future maintenance pass prunes it.
- **The country indexes buy nothing on today's single-country dataset.** They are
  added now because an `ALTER TABLE` on a populated table later is dearer than a
  btree on a two-character column today — a bet on the second Country Pack, stated
  rather than hidden (§25).

## Not in Phase 7

The interactive map frontend (Phase 8) — this phase ships the API and the data,
not the MapLibre view. Routing, isochrones or travel-time queries. Reverse
geocoding. A live geocoder in the default test suite. Any provider beyond
Nominatim. Work-authorization eligibility — whether a candidate may legally take a
remote role is Phase 9's question, and Phase 7 only decides whether the posting is
discoverable. Scheduled or request-triggered enrichment; the pass is a CLI an
operator runs. The background worker, Redis and the browser worker.
