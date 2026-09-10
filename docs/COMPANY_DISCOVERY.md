# V2 Company Discovery

Built in Phase 6. This document describes the layer that replaced
`a company is a name printed on a posting` with:

```
company seeds -> discovery providers -> identity resolution -> Company
              -> career pages -> ATS detection -> discovery records
              -> opportunities and spontaneous-application capability
```

The point of the phase in one sentence: **an employer exists whether or not it is
hiring today**. A company with zero active opportunities is discovered, stored,
searchable and eligible for a spontaneous application; nothing in the system needs
an open posting to know the employer is there.

## The invariant

Three claims, each enforced by a test rather than by review
(`tests/test_v2_company_boundaries.py`):

- **No module in `backend/app/companies/` names a provider**, except `bootstrap.py`
  (which exists to compose) and the provider modules themselves. The orchestrator
  and the registry hold no list.
- **Nothing in `backend/app/companies/` opens a session or writes a row.** Adapters
  discover; application services decide what to persist (§17).
- **No provider makes a network request.** All three Phase 6 providers read a
  configuration file, an operator list, or the local opportunities table.

V1 is untouched. `config/companies.yaml` is still V1's file, read through V1's own
`load_v1_companies()`, and no company list is duplicated into Python.

## Layout

```
backend/app/companies/
  identity.py       normalization, identity signals, verdicts, comparison
  ats.py            Greenhouse/Lever/Ashby detection with typed confidence
  contracts.py      CompanyDiscoveryProvider, seeds, requests, results, warnings
  registry.py       key -> provider, and the selection rules
  resolution.py     a posting's company name -> a canonical company, or not
  orchestrator.py   one pass: select, run concurrently, report
  failures.py       Phase 5 health semantics, reused verbatim
  bootstrap.py      composition; the only module that names a provider
  providers/base.py what all three providers do identically
  providers/configured_ats.py       V1's ATS configuration as company evidence
  providers/stored_opportunities.py employers named by postings already stored
  providers/manual_seed.py          employers an operator asserts
backend/app/domain/company.py       Company, aliases, career sites, records
backend/app/services/company_discovery.py  ingest, resolve, link, persist
backend/app/services/company_directory.py  the read side, paginated
backend/app/api/routes/companies.py        three endpoints
backend/migrations/versions/rev_0004_phase_6_company_discovery.py
```

Imports run one way only:

```
identity -> ats -> contracts -> {registry, resolution, failures}
         -> orchestrator -> providers -> bootstrap
```

and the services depend on all of it while none of it depends on a service.

## Identity

A company is an identity, not a label. `Company` carries a canonical `name` (what
we display), `normalized_name` and `normalized_domain` (what we compare),
`website`, `careers_url`, `country`, `identity_status`, a `DetectedATS`, a
`SpontaneousApplicationChannel`, its `locations` and its timestamps. Nothing else:
§1 asks for the fields the workflow justifies and no more.

`normalized_name` and `normalized_domain` are **properties on the domain model**,
derived from `name` and from `website`/`careers_url`. They are columns in the
database because the resolver has to index them, but no code can set one to a value
that disagrees with the name beside it.

`identity_status` has three members and only two are reachable automatically:

| Status | Meaning |
| --- | --- |
| `SEEDED` | a name and a provenance, nothing corroborating it |
| `PROVISIONAL` | a second independent signal agreed — a domain, an ATS organization |
| `VERIFIED` | a human confirmed it; **no Phase 6 code path writes this** |

A company created from a claim with corroboration starts `PROVISIONAL`; one created
from a bare name starts `SEEDED` and is promoted to `PROVISIONAL` the first time a
later pass brings a domain or an ATS organization. Nothing demotes.

### Normalization is deterministic and removes no words

`normalize_company_name` (in `backend/app/domain/company.py`) applies, in order:
NFKD normalization, accent folding, case folding, deletion of *joining*
punctuation (`.`, `'`, `’`, `ʼ`, `´` — so `S.A.` becomes `sa`), replacement of
every other non-word character with a space, and whitespace collapse.

It **removes no word**. `Acme Switzerland` normalizes to `acme switzerland`, never
to `acme`: a country word is meaningful, and §3 forbids stripping it. There is no
stopword list, no acronym expansion and no fuzzy matching anywhere in the package —
no Levenshtein distance, no token-set ratio, nothing that could decide two names
are "close enough".

`normalize_domain` keeps the host only: lower case, no scheme, no port, no path, no
trailing dot, no `www.` prefix.

Legal forms are the one country-aware step, and they are handled **without
mutating anything stored**. `identity.comparison_key(name, legal_suffixes)` strips
at most one trailing legal form — longest match first, so `société coopérative`
cannot be shadowed by `coopérative` — and never returns an empty string. The
suffix list comes from the country pack (`terminology.company_legal_suffixes`), the
algorithm lives in `identity.py`, and the result is computed on demand and never
persisted (§19: configuration in the pack, workflow in the service).

### Signals and verdicts

`identity.compare(left, right)` returns an `IdentityAssessment` carrying an
`IdentityVerdict` and the signals that produced it.

**Strong signals** — any one of them decides:

| Signal | Why it is strong |
| --- | --- |
| `EXTERNAL_ID` | the same provider's own identifier for the employer |
| `ATS_ORGANIZATION` | `boards.greenhouse.io/acme` belongs to one employer |
| `EMPLOYER_DOMAIN` | an employer-controlled host, not a shared platform |
| `CONFIRMED_ALIAS` | an operator wrote it down (`manual_seed`) |

**Supporting signals** — `NORMALIZED_NAME`, `LEGAL_FORM_KEY`, `KNOWN_ALIAS` — and
one tie-break, `COUNTRY_DOMAIN_PREFERENCE`, from
`metadata.company_domain_suffixes`. A tie-break, never a filter: a Swiss company on
a `.com` is ordinary, and refusing it would lose half the market.

The rules, in order:

1. a strong signal in common → `SAME_COMPANY`;
2. otherwise, two *different* employer domains → `DISTINCT`;
3. otherwise, names alone agreeing → `POSSIBLE_MATCH`;
4. nothing comparable → `INSUFFICIENT_EVIDENCE`.

`POSSIBLE_MATCH` is a real answer and it **never merges**. `Logitech`,
`Logitech Europe S.A.`, `LOGITECH` and `Logitech SA` all reduce to the same
comparison key, and that alone gets them a `POSSIBLE_MATCH` — a shortlist entry for
a human or for a later pass carrying a domain, not a link.

`EMPLOYER_DOMAIN` is only claimed for a host that is not a shared platform.
`SHARED_ATS_HOSTS` lists fourteen of them (`boards.greenhouse.io`,
`jobs.lever.co`, `jobs.ashbyhq.com` and their API and alternate hosts among them),
and `is_employer_domain(host)` is a suffix test against that set — otherwise every
company on Greenhouse would share one "domain" and merge into a single employer.

### Aliases

`CompanyAlias` records another label the same employer is published under: the
`alias` as the source spelled it, its `normalized_alias`, the `source_key` that
reported it, and a `first_seen_at`/`last_seen_at` window.

Its own table because an alias has provenance (§4). A board's own spelling and an
operator's confirmation are different kinds of claim, and only the second is a
`CONFIRMED_ALIAS` strong signal — which is exactly what
`confirmed_alias_sources={manual_seed}` in `api/dependencies.py` says.

The canonical `name` is **never overwritten** because another source used a
different label. A new label becomes an alias.

## The provider contract

```python
class CompanyDiscoveryProvider(Protocol):
    @property
    def metadata(self) -> CompanyProviderMetadata: ...

    async def discover(self, request: CompanyDiscoveryRequest) -> CompanyDiscoveryResult: ...

    async def healthcheck(self) -> ProviderHealth: ...
```

Deliberately **distinct from `OpportunitySource`** (§6). The two answer different
questions — "which employers exist?" versus "what is open right now?" — and one
protocol doing both would force every provider to invent postings and every source
to promise a company identity it cannot resolve. A single class may satisfy both
internally; the contracts stay separate.

The shapes a provider works with:

- `CompanySeed` — a name plus whatever came with it: `kind`, `external_id`,
  `website`, `careers_url`, `country`, `ats_platform`, `ats_organization_id`,
  `source_url`, `raw`. A seed is **not** a company.
- `DiscoveredCompany` — a seed plus optional enrichment (career sites, locations,
  detection, spontaneous-application signal). A validator refuses a claim that
  arrives without its evidence.
- `CompanyDiscoveryRequest` — `country`, `provider_keys`, `limit` (≤ 500). It
  **carries no seeds**: see [Sharing](#sharing).
- `CompanyDiscoveryResult` — the companies, the warnings, a **required**
  `ProviderHealth`, and `completed_at`.

`discover` returns rather than raises. `providers/base.py` wraps `_seeds` in an
`except Exception` and turns the failure into `UNAVAILABLE` health; the
orchestrator contains it a second time. A provider is therefore safe to call from a
test or a CLI without a try block.

### The five seed kinds

`CompanySeedKind` — `OPPORTUNITY`, `CONFIGURED`, `ATS_ORGANIZATION`, `WEBSITE`,
`MANUAL` — is §7's list. There is no crawler kind, because there is no crawler.

### The three providers

| Key | Priority | Reads | Capabilities |
| --- | --- | --- | --- |
| `manual_seed` | 5 | an operator's list, empty in Phase 6 | country filter, website, career site, ATS, location, spontaneous signal, healthcheck |
| `configured_ats` | 10 | `config/companies.yaml` through V1's loader | ATS, career site, healthcheck |
| `stored_opportunities` | 20 | the local `opportunities` table | country filter, ATS, career site, location, healthcheck |

None declares a country, so all three serve every country. None declares a
credential — the field exists and `failures.py` carries it across so that the day
one does, redaction covers it without a code change.

`configured_ats` turns each configured Greenhouse/Lever/Ashby organization into a
seed with `external_id = "{platform}:{token}"` and a `LIKELY` detection: a
configuration file is a strong hint and not an observation. This is §8 — V1's
existing ATS configuration reused as company evidence, not copied.

`stored_opportunities` groups stored postings by normalized company name, uses the
normalized name as its `external_id`, and reads the ATS from the postings'
application URLs. It is the provider behind §27: `Opportunity(company_name=
"Logitech")` becomes a seed, which becomes a company, which the posting is then
linked to.

`manual_seed` is registered with an empty list on purpose. A provider that is
present and reports `NOTHING_CONFIGURED` is legible; a provider that is absent
because nothing is configured looks like a bug.

### What a provider may not do

Write a row, open a session, hold a clock other than the injected one, fetch a URL,
call an LLM, or shrink its answer silently. A result narrower than the question
says why: `LIMIT_TRUNCATED`, `SEED_SKIPPED`, `NOTHING_CONFIGURED`.

## The registry

`CompanyProviderRegistry` maps `provider_key` → provider, refuses a duplicate key
(`ProviderRegistryError(DUPLICATE_PROVIDER)`), raises `UNKNOWN_PROVIDER` rather
than returning `None`, and orders deterministically by `(priority, provider_key)` —
so two passes over the same registry visit the providers in the same order.

`providers_for(country=…, required_capabilities=…, provider_keys=…,
include_disabled=…)` is the selection: country compatibility, capability filtering,
the enabled flag, and an explicit key list when a caller wants one. The registry
also records health (`record_health`, `health_for`, `health()`) and can name the
providers that are currently unusable.

It **contains no provider list**. `bootstrap.build_company_provider_registry` is
the only module in the package that names a class, and
`api/dependencies.company_discovery_service` is the only place that composes the
whole thing.

## Orchestration

`CompanyDiscoveryOrchestrator.run(request)` selects the eligible providers, runs at
most `MAX_CONCURRENT_PROVIDERS = 4` at a time behind a semaphore, and returns a
`CompanyDiscoveryReport` carrying every result, every warning, the health of each
provider, and whether the pass was complete.

`asyncio.gather`, never a `TaskGroup`: a `TaskGroup` cancels its siblings when one
task raises, which is precisely the behaviour §26 forbids. One provider failing
must not cancel the others' findings.

`companies` on the report is **not deduplicated**. Two providers reporting the same
employer is normal and interesting, and collapsing them in the report would hide
which providers agreed. Deduplication is identity resolution's job, downstream.

## Health and failures

Phase 5's semantics, reused rather than reimplemented.
`backend/app/companies/failures.py` translates a `CompanyProviderMetadata` into the
`SourceMetadata` shape `backend.app.discovery.failures.classify_failure` reads, and
returns what it returns. Nine lines of code, and the point is that there is exactly
**one** implementation of "read an exception, write a safe sentence" — a second
classifier, however careful, is a second place for a credential to leak.

So no raw exception is ever serialized. What is stored and displayed is a
normalized `ProviderFailureCode` and a redacted detail string, with credential
values blanked by name.

## Warnings

`CompanyDiscoveryWarningCode`: `NO_PROVIDER_SELECTED`,
`COUNTRY_FILTER_NOT_SUPPORTED`, `LIMIT_TRUNCATED`, `SEED_SKIPPED`,
`PARTIAL_RESULTS`, `NOTHING_CONFIGURED`. A provider-level warning always names its
provider, because the reader has to know which file to go and fix.

`NOTHING_CONFIGURED` matters more than it looks: a fresh deployment with an empty
`config/companies.yaml` is a **healthy** provider with nothing to say. Conflating
that with a failure would make a working system look broken.

## ATS detection

`backend/app/companies/ats.py` detects the three platforms already supported —
Greenhouse, Lever, Ashby — from URLs and from configuration. Eight known board
hosts (both live Greenhouse board hosts, both Lever hosts, both Ashby hosts and the
API hosts that turn up in exported configuration), an organization token bounded to
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, and template URLs for rebuilding the public
board address from a platform and an organization.

Only these three platforms. Phase 6 does not attempt to recognise every ATS on the
market, and it introduces no Playwright crawling: detection reads hosts, path
structures and configured organization identifiers.

### Detection is not verification

`DetectionStatus` has three members and the distinction is §10's:

| Status | What earns it |
| --- | --- |
| `CONFIRMED` | a URL on a known board host with a readable organization token |
| `LIKELY` | a configured organization identifier |
| `UNKNOWN` | nothing that identifies a platform |

`detect_from_url` rejects `embed` and `www` as organization tokens — they are path
noise, not employers. "The HTML contains the word Greenhouse" is not evidence and
there is no code path that treats it as such.

`DetectedATS` is all-or-nothing: a platform without a status, a detector or
evidence cannot be constructed, its status is never `UNKNOWN`, and a `CONFIRMED`
detection must name the organization identifier — without one, nothing can fetch
the board it claims to have confirmed. The database repeats the rule in
`ck_companies_ats_complete_or_absent`.

Evidence codes: `ATS_BOARD_URL`, `ATS_CONFIGURED_ORGANIZATION`,
`MANUAL_OPERATOR_ASSERTION`.

## Career pages

`CareerSite` is one careers endpoint of one company: `url`, `kind`
(`CAREERS_PAGE`, `ATS_BOARD`, `SPONTANEOUS_APPLICATION`), `platform`,
`source_key`, `verification_status`, `discovered_at`, `last_checked_at`.

Records rather than a column, because a company legitimately has several (§11).
`Company.careers_url` survives as the **preferred** endpoint, which §11 explicitly
permits — an `ATS_BOARD` first, since that is the one a source plugin can read.

An `ATS_BOARD` must name its platform
(`ck_company_career_sites_ats_board_names_its_platform`): a board nothing can
identify the platform of is a board no source plugin can read.

`last_checked_at` is nullable and **Phase 6 never writes it**. Nothing in this
phase fetches a URL, so a timestamp here would claim a check that never happened.
The column exists for the phase that does fetch; the UI renders the null as
"checked never".

## Spontaneous applications

`SpontaneousApplicationSupport` is `SUPPORTED` / `NOT_SUPPORTED` / `UNKNOWN`, and
the whole channel may also be absent — which is a fourth state, "nobody has
looked", and not the same statement as `UNKNOWN`.

Nothing is inferred. **"No active jobs" is not evidence that unsolicited
applications are welcome** (§12), and no code path derives one from the other. A
decided verdict must carry evidence and an observer; `NOT_SUPPORTED` with an
application URL is a contradiction and is refused. The boolean
`accepts_spontaneous_applications` that Phase 1 already had is kept in agreement
with the verdict by a domain validator and by
`ck_companies_spontaneous_support_matches_flag`.

The screens render `UNKNOWN` as unknown, never as "no": a verdict nobody has formed
and an employer that refuses unsolicited applications are different facts, and only
the second is a reason not to write.

## Opportunity to Company

`resolution.resolve(claim, stored, pack)` takes a claim — a posting's company name
plus whatever else the posting carries — and a shortlist of stored candidates
(`find_candidates`, at most `MAX_CANDIDATES = 25`), and returns a
`CompanyResolution`:

| Outcome | When | What is written |
| --- | --- | --- |
| `MATCHED` | exactly one candidate shares a strong signal | `Opportunity.company_id` |
| `AMBIGUOUS` | two candidates share strong signals, or the best evidence is a `POSSIBLE_MATCH` | nothing; the candidates are reported |
| `UNRESOLVED` | no candidate is comparable | nothing |

A `CompanyResolution` can only carry a `company_id` when the outcome is `MATCHED` —
a model invariant, so an ambiguous result cannot be mistaken for a link.

Two properties the phase order asks for by name:

- **`company_name` is never overwritten.** The posting keeps the string the board
  published; `company_id` is added beside it. That string is provenance (§13).
- **Resolution is idempotent.** Running it again over the same postings links the
  same rows and creates nothing. `_already_known` short-circuits a posting that is
  already linked.

### Ambiguity

An ambiguous posting stays unlinked, and that is the correct outcome (§14). An
unlinked posting is a question someone can answer later; a wrongly merged employer
is a corruption that spreads into every match, every application and every CV
generated from it.

The API reports `ambiguous` as a count in the pass result, and the UI shows it as
an outcome rather than an error — "we refused to merge two employers on a guess"
is what it looks like from outside.

There is **no automatic merge**. No destructive merge primitive is exposed, no
low-confidence decision merges anything, and **no LLM participates in identity**
(§25): Phase 6 works with no LLM configured at all.

## Persistence

Revision `0004`, `revises 0003`. Additive: thirteen columns added to `companies`,
three tables created, nothing dropped and no column retyped. A database at 0003
keeps every company, posting, evaluation, account and session it had.

Three of §15's four candidate tables were created. `company_external_ids` was
**not**: the external identity Phase 6 actually produces is an ATS organization,
which is a partial unique index on `companies`, and a fourth table holding one kind
of row would be a join for nothing. §15 asks for the smallest normalized schema.

### The new columns on `companies`

`normalized_name`, `normalized_domain`, `country`, `identity_status`,
`ats_platform`, `ats_organization_id`, `ats_status`, `ats_detected_by`,
`ats_evidence`, `spontaneous_support`, `spontaneous_url`,
`spontaneous_observed_by`, `spontaneous_evidence`.

`normalized_name` is added nullable, **backfilled by running the normalizer**, and
then made `NOT NULL`. The obvious SQL guess — `lower(btrim(name))` — is wrong:
`Logitech Europe S.A.` normalizes to `logitech europe sa`, not
`logitech europe s.a.`. A comparison column that disagrees with the name beside it
is worse than a failed migration, because every lookup by name would silently miss
the rows it was written for. The revision restates the two helper functions rather
than importing them, for the reason 0003 gives: a revision is an immutable record,
and its output must not change when the domain module is next edited. A row whose
name normalizes to nothing fails the migration with the offending names, because
inventing a name for an employer is the fabrication the standards forbid.

Neither comparison column is unique. Deciding that `Migros`, `Migros SA` and
`MIGROS Vaud` are one employer is evidence-based work; a unique index would be the
database making exactly the decision `resolve` refuses to make.

### The three new tables

| Table | Grain | Uniqueness |
| --- | --- | --- |
| `company_aliases` | one label of one company | `(company_id, normalized_alias)` |
| `company_career_sites` | one careers endpoint of one company | `(company_id, url)` |
| `company_discovery_records` | one provider's sighting of one company | `(provider_key, external_id)` |

All UUID primary keys, all timestamps `TIMESTAMPTZ` set by the database clock, all
constraints named, all reachable through `alembic upgrade head` from an empty
database. Enums are `VARCHAR(32)` plus a CHECK, never a native PostgreSQL `ENUM`:
widening a CHECK is an ordinary migration, while adding a member to a native enum
is DDL that cannot share a transaction with a table rewrite.

`company_discovery_records.company_id` is nullable with `ON DELETE SET NULL`. Both
halves matter: §14 leaves an ambiguous seed unlinked, and the sighting we keep is
what stops the next pass rediscovering and re-refusing it; and a future merge of two
duplicate employers must not delete the provenance that revealed the duplication.

### Idempotence by construction

Every derived id is a UUID5 over the natural key
(`backend/app/domain/identifiers.py`):

```
company_alias_id(company_id, normalized_alias)
career_site_id(company_id, url)
company_discovery_record_id(provider_key, external_id)
```

So a second pass over the same seeds *recomputes the same primary keys* and updates
those rows. The unique constraints are the backstop, not the mechanism. §23 is
therefore a property of the schema and the id functions, not a convention the
services have to remember — and the tests assert it by running a pass twice and
counting rows.

Updates are additive. `_update` fills gaps and promotes; it does not replace a
canonical name, downgrade an identity status, or overwrite a `CONFIRMED` detection
with a `LIKELY` one from another platform.

## Provenance

`CompanyDiscoveryRecord` answers §5's question — "how did we discover this
company?" — with `provider_key`, `external_id`, `seed_kind`, `company_id`,
`company_name` as the provider spelled it, `source_url`, `discovered_at`,
`confidence` and `raw`.

`raw` never holds a secret. The domain model refuses a payload with a
credential-shaped key before it can reach the database — fourteen forbidden
substrings, covering authorization, cookie, token, key, secret and password shapes.
No CHECK mirrors that list, deliberately: a substring list that will grow, copied
into SQL, is a second copy that silently disagrees.

`raw` also never leaves the backend. `CompanyDiscoveryRecordResponse` has no `raw`
field (§29), so the detail screen could not display one if it tried.

## The API

Three operations, both routers requiring an authenticated session:

| Operation | Notes |
| --- | --- |
| `GET /api/v2/companies` | five filters — `text`, `country`, `ats_platform`, `spontaneous_support`, `has_opportunities` — plus `limit` (1–100, default 20) and `offset` |
| `GET /api/v2/companies/{id}` | the company with its aliases, careers endpoints and provenance; 404 for an unknown id |
| `POST /api/v2/company-discovery/run` | run a pass, persist what it found, link the postings |

Pagination is explicit and bounded **at the signature**, so an out-of-range value
is a 422 that names the parameter rather than a clamp the client cannot see.
`CompanyDirectoryService` clamps as well, for every non-HTTP caller. No endpoint
returns an unbounded list.

`spontaneous_support` is an enum and not a boolean because "show me the employers
nobody has checked" is a real question a boolean could not ask.

`has_opportunities=false` is the acceptance criterion of the phase (§28) expressed
as a filter: an employer with nothing posted is in the directory like any other,
and this is how you ask for exactly those.

The pass returns `200`, not `201`: it is idempotent by design, so a second
identical call creates nothing and "Created" would be a lie about the common case.
What happened is in the body — `created`, `matched`, `ambiguous`, `company_ids`,
the link counts, the per-provider `health` and the `warnings`. A provider that
failed is reported there, never raised.

## Sharing

**A company is a shared fact.** There is no `user_id` on `companies` or on any of
the three new tables (§21), and no company endpoint filters by session.

Authentication is still required — the directory is not public — but authorization
has nothing to scope here: two accounts asking the same question get the same
answer, which is the point. An employer is a fact about the world, not a row
belonging to whoever discovered it first. Adding an owner column to make the read
*look* scoped would be a lie about who the data belongs to.

Two consequences worth stating:

- **The cache keys carry no account.** `frontend/app/composables/useCompanies.ts`
  keys on the filters only (`companies:list:…`, `companies:detail:…`), because
  there is no per-user variant of the answer to invalidate.
- **A discovery pass accepts no seeds.** `POST /company-discovery/run` takes a
  country, a provider selection and two ceilings — and deliberately no way to add
  an employer. A request body that carried a seed would let any account write into
  every other account's directory. New seeds are configuration.

The user-owned side of the system is unchanged: opportunities, evaluations,
profiles and sessions remain scoped, and `Opportunity.company_id` points at shared
data from a row that is not shared.

## Country pack integration

A pack contributes **configuration**, never an algorithm (§19):

| Field | Used for |
| --- | --- |
| `terminology.company_legal_suffixes` | building a comparison key; a suffix match alone never merges |
| `metadata.company_domain_suffixes` | a tie-break between otherwise equal candidates, never a filter |
| `sources` | which sources exist in the country, unchanged from Phase 5 |
| `terminology` | the vocabulary normalization already used for postings |

The Swiss pack lists the forms a registered Swiss name ends with, across the four
language regions — `sa`, `sàrl`, `ag`, `gmbh`, `spa`, `srl`, `genossenschaft`,
`verein`, `stiftung` and the English forms Swiss boards print verbatim for foreign
parents. Punctuation is dropped before matching, so `sa` also covers `S.A.`.

Deliberately **absent**: `group`, `holding`, `suisse`, `switzerland`, `schweiz`.
All meaningful words. Stripping them would turn `Acme Switzerland` into `Acme`,
which §3 forbids by name.

Validation is in `country_packs/contracts.py`: suffixes must already be normalized,
must not repeat, and a domain suffix must look like a suffix (`^(\.[a-z0-9-]+)+$`)
so a pack cannot write a whole host where an ending belongs.

## Frontend

Two intentionally simple pages, existing V2 session and CSRF plumbing, generated
OpenAPI types, no business logic:

- `/companies` — the filters, the page, the pager, and a `Run discovery` button
  that posts the pass and reports what it did;
- `/companies/[id]` — the facts, and what each one rests on: the ATS badge with its
  status and evidence, the spontaneous-application verdict with its observer, the
  careers endpoints with their provenance, the aliases, the discovery records.

No map, no coordinates, no scoring. The nav link is session-gated because the pages
are guarded, not because the data is private.

Covered by `frontend/tests/nuxt/pages/companies.spec.ts` and four Playwright flows
in `frontend/tests/e2e/companies.spec.ts` — the reachability of the directory, the
`has_opportunities=false` filter through a real round trip, the CSRF header on the
pass, and the guard sending an anonymous visitor to `/login`.

## Tests

No test in the default suite touches a live ATS, a live job board or an LLM.
Providers read fixtures; the ATS detector is a pure function.

| Suite | Covers |
| --- | --- |
| `tests/test_v2_company_identity.py` | normalization, domain comparison, legal suffixes, signals, verdicts, similar names *not* merging |
| `tests/test_v2_company_ats.py` | the three platforms, unknown sites, ambiguous evidence, `CONFIRMED` vs `LIKELY` |
| `tests/test_v2_company_resolution.py` | matched, ambiguous, unresolved, `company_name` preserved, repeated resolution idempotent |
| `tests/test_v2_company_providers.py` | the registry, capability and country filtering, failure isolation, no network |
| `tests/test_v2_company_persistence.py` | idempotent writes, the named CHECK and UNIQUE refusals, the cascades and the unlinked sighting, the shortlist and directory queries |
| `tests/test_v2_persistence_migrations.py` | the upgrade from 0003, the backfill, a clean run from an empty schema to head, the rollback |
| `tests/test_v2_persistence_schema.py` | the table inventory, the named constraints, no `user_id` on a shared table |
| `tests/test_v2_company_services.py` | ingestion outcomes, linking, spontaneous states, additive updates |
| `tests/test_v2_api_companies.py` | auth required, pagination, 404 semantics, the safe response subset |
| `tests/test_v2_company_boundaries.py` | no provider named outside bootstrap, no session inside the package, no `user_id` on the shared tables |

## Known risks

- **The backfill is frozen at revision 0004.** If `normalize_company_name` ever
  changes, existing rows keep the form 0004 computed and a new revision has to
  re-backfill. The round-trip test in `tests/test_v2_persistence_migrations.py` is
  what turns that from a hope into a failure.
- **`stored_opportunities` groups by normalized name.** Two genuinely different
  employers with the same normalized name become one seed. They resolve to at most
  one company only when a strong signal agrees; otherwise the result is
  `AMBIGUOUS`, which is the safe failure — but the seed itself is coarser than the
  identity layer downstream of it.
- **`POSSIBLE_MATCH` accumulates.** Employers that differ only by a legal form and
  have no domain on file stay separate until a pass brings a domain or an operator
  writes an alias. That is the intended trade, and it means the directory will
  contain visible near-duplicates.
- **Ambiguity is unbounded.** Nothing yet lets an operator resolve an ambiguous
  seed from the UI; the pass just reports the count each time.
- **`last_checked_at` is always null.** Nothing verifies a careers URL still
  answers, so a board that has been taken down looks exactly like one that works.

## Not in Phase 6

Arbitrary web crawling. Search-engine scraping. Geocoding and radius queries — the
`CompanyLocation` rows Phase 6 writes are textual, and a coordinate is only kept
when a source already provided one, with its provenance. Map UI. Company
recommendation scoring. Candidate/company matching. CV generation. LLM-based
company research or LLM-driven identity decisions. Autonomous applications. The
browser worker, Redis and background task infrastructure. Additional countries.
Any ATS beyond Greenhouse, Lever and Ashby. Destructive automatic merging.
