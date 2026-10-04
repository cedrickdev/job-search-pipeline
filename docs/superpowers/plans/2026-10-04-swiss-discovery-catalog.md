# Swiss Discovery Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist Swiss saved-search discoveries and make their provenance, freshness and source health visible in the existing map/list without sending applications.

**Architecture:** Keep `DiscoveryOrchestrator` source-neutral. Add a catalog write boundary, owner-scoped run/membership records and a general-lane task handler; reuse PostgreSQL/PostGIS and the existing geo read/UI. A migration removes the unsafe one-source-per-opportunity assumption while preserving current rows.

**Tech Stack:** Python 3, FastAPI, Pydantic, SQLAlchemy asyncio, Alembic, PostgreSQL/PostGIS, Nuxt 4, Vue 3, TypeScript, Vitest and pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-swiss-discovery-catalog-design.md`

## Global Constraints

- CH only in this delivery. Do not add LinkedIn login, scraping or email-alert import.
- No mailbox password and no application submission in this feature or its tests.
- Public opportunity rows may be shared; search/run membership must be scoped to the authenticated owner.
- Unknown location means list-only, never a fabricated map coordinate.
- Partial sweep failure cannot expire previously seen offers or masquerade as a complete zero-result sweep.
- Preserve the existing uncommitted Phase 12.1 changes; stage and commit only this plan's exact files.
- Follow `docs/IMPLEMENTATION_PLAN.md`: finish this one gated increment, report checks/risks, request validation, then stop.

## Review Focus

1. Two distinct roles share an employer and title: retain two opportunities unless a strong target identity proves they are the same (Task 2 test).
2. A source URL is missing or changes while its external ID stays stable: preserve source identity and latest valid URL (Task 2 test).
3. A source reports zero after a timeout/403 elsewhere: show incomplete coverage and keep earlier observations (Task 3 test).
4. A crafted description contains HTML or a `javascript:` URL: escape text and refuse unsafe outbound links (Task 6 test).
5. A user supplies another user's search or run ID: respond as absent and reveal no report or membership (Task 5 test).

---

## File structure and interfaces

- `backend/app/discovery/catalog.py`: pure identity and search-filter decisions; no I/O.
- `backend/app/services/discovery_catalog.py`: sweep orchestration, persistence and report assembly.
- `backend/app/repositories/discovery_catalog.py`: owner-scoped run/membership repository contract and SQLAlchemy implementation, following existing session ownership.
- `backend/app/infrastructure/database/models.py`, `backend/app/infrastructure/database/mappers.py`, `backend/app/repositories/sqlalchemy_repositories.py`, and one new Alembic revision: source observations plus run/membership schema.
- `backend/app/tasks/handlers.py`: general-lane `OPPORTUNITY_DISCOVERY` handler.
- `backend/app/api/routes/discovery.py`: authenticated start, status and result routes; register beside existing routes.
- `backend/app/services/geo_search.py` and the opportunity geo repository: saved-search results intersect collected membership, without changing open public geo reads.
- `frontend/app/composables/useDiscovery.ts` and `frontend/app/pages/map.vue`: run action/status/health while reusing the map components and geo query.

API registration lives in `backend/app/api/router.py`. The branch contains unrelated Phase 12.1 changes; no broad refactor is part of this plan.

### Task 1: CH source boundary and deterministic profile filtering

**Files:** Modify `country_packs/ch/sources.yaml`; create `backend/app/discovery/catalog.py`; test `tests/test_v2_discovery_catalog.py`.

**Interfaces:** `matches_search(profile: SearchProfile, opportunity: Opportunity) -> bool` and `canonical_target(opportunity: Opportunity) -> str | None`. Only a verified ATS/job-detail target may provide a canonical target; company plus title does not.

- [ ] Write tests for a CH request with LinkedIn excluded, an inactive/no-CH profile, opportunity-type/keyword exclusion, and two same-title roles with different source targets. Assert the latter have different `canonical_target` values or `None`.
- [ ] Run `.venv/bin/pytest tests/test_v2_discovery_catalog.py -q` and confirm new tests fail for missing functions or active LinkedIn.
- [ ] Disable only the LinkedIn CH binding and implement pure decisions. Sketch the filter boundary as:

  ```python
  def matches_search(profile: SearchProfile, opportunity: Opportunity) -> bool:
      if not profile.is_active:
          return False
      if profile.opportunity_types and opportunity.opportunity_type not in profile.opportunity_types:
          return False
      if profile.contract_types and opportunity.contract_type not in profile.contract_types:
          return False
      if profile.workplace_modes and opportunity.workplace_mode not in profile.workplace_modes:
          return False
      if profile.posting_languages and opportunity.posting_language not in profile.posting_languages:
          return False
      text = f"{opportunity.title} {opportunity.description or ''}".casefold()
      if any(word.casefold() in text for word in profile.excluded_keywords):
          return False
      return not profile.title_keywords or any(
          word.casefold() in opportunity.title.casefold() for word in profile.title_keywords
      )
  ```

  `queries` drive source requests; country/radius/remote-area matching remains in the existing PostGIS geo query. Test the full composition, not just this pure predicate.
- [ ] Run the focused tests and existing `tests/test_v2_discovery_orchestration.py`; commit these exact files.

### Task 2: Conservative opportunity identity and provenance migration

**Files:** Add `backend/migrations/versions/rev_0022_swiss_discovery_catalog.py` after the current uncommitted revision 0021; modify `backend/app/infrastructure/database/models.py`, `backend/app/infrastructure/database/mappers.py`, and `backend/app/repositories/sqlalchemy_repositories.py`; tests `tests/test_v2_persistence_migrations.py`, `tests/test_v2_persistence_mappers.py`, `tests/test_v2_discovery_catalog_persistence.py`.

**Interfaces:** `CatalogOpportunityStore.observe(opportunity: Opportunity, *, canonical_target: str | None) -> OpportunityId`; read methods expose all source records for the canonical opportunity. Preserve historical source records during upgrade.

- [ ] Write migration and repository tests for repeated same-source observations, two verified URLs for one target, different same-title roles, missing URL with stable external ID, and concurrent duplicate inserts. Test that all old rows retain their source record after upgrade.
- [ ] Run the focused tests against the test Postgres service; verify failures at the old one-to-one/unique-fingerprint constraints.
- [ ] Add a transaction-safe observation path: select by `(source_key, external_id)` or normalized URL, use a canonical-target key only when verified, and attach a source record to the resolved opportunity. The core decision is:

  ```python
  existing = await store.find_source(source_key, external_id, normalized_url)
  target = existing or (await store.find_target(canonical_target) if canonical_target else None)
  opportunity_id = target.id if target else opportunity.id
  await store.upsert_observation(opportunity_id, opportunity.source)
  ```

  Database constraints, not this sketch alone, must settle races. Remove the unique company/title fingerprint constraint only after the new source/target uniqueness and compatibility migration are tested. Adapt the ORM relationship to multiple records while retaining the domain's preferred `source` field for existing callers; provide a separate provenance read for all records.
- [ ] Run migration upgrade/downgrade on disposable test data plus focused tests; commit only the migration, model, mapper, repository and tests.

### Task 3: Owner-scoped run and membership persistence

**Files:** Extend the Task 2 revision or add the next revision only if Task 2 was committed; create `backend/app/repositories/discovery_catalog.py`, `backend/app/services/discovery_catalog.py`; test `tests/test_v2_discovery_catalog_persistence.py` and `tests/test_v2_discovery_catalog_service.py`.

**Interfaces:** `run_profile(user_id: UserId, search_profile_id: SearchProfileId, *, trigger: Literal['MANUAL', 'MORNING']) -> DiscoveryRunId`; `get_run(user_id, run_id) -> DiscoveryRun | None`; `list_members(user_id, search_profile_id, *, limit, offset) -> tuple[OpportunityId, ...]`.

- [ ] Write tests for two-user ownership, profile deletion, repeated observations, restart persistence, partial failure, and unchanged last-seen after a missing result in a failed/incomplete run.
- [ ] Run the tests and verify they fail for the missing run/membership store.
- [ ] Add `discovery_runs` and `discovery_memberships` with user/search foreign keys, timestamps, status, trigger, aggregate counts, sanitized health/warning JSON and indexes on owner/search/seen time. Persist the report and memberships in bounded batches, checking ownership again in the worker. The transaction boundary must make a run either report its committed observations or an explicit failure; never claim success before the commit.
- [ ] Run focused tests and migration checks; commit exact files.

### Task 4: General-lane execution and manual/morning parity

**Files:** Modify `backend/app/tasks/handlers.py`, `backend/app/cli/run_worker.py`, `backend/app/api/dependencies.py`, `backend/app/discovery/orchestrator.py`; create `backend/app/cli/run_discovery.py`; tests `tests/test_v2_tasks_handlers.py`, `tests/test_v2_discovery_catalog_service.py`, `tests/test_v2_discovery_orchestration.py`.

**Interfaces:** `build_opportunity_discovery_handler(service_factory)` consumes only `user_id`, `search_profile_id` and trigger from task payload. `enqueue_discovery(user_id, search_profile_id, trigger)` uses existing `TaskKind.OPPORTUNITY_DISCOVERY` on `GENERAL`.

- [ ] Write a failing test proving the same `run_profile` service is called by manual and morning triggers, duplicate enqueues are idempotent, a worker rechecks profile activity/ownership, and one timed-out source leaves other source results intact.
- [ ] Run focused tests and confirm the handler is absent.
- [ ] Wire the handler into the existing general worker and add an explicit operator/morning CLI that enumerates active CH profiles for one user or a configured cohort, without enabling an implicit schedule. Task payloads contain IDs and trigger only. Use a stable idempotency key for the intended run window, not a new key on every retry. Bound each source's network/parse duration and report timeout as that source's failure; cap total requests and worker concurrency through existing discovery limits.
- [ ] Run task tests and a dry local CLI invocation with synthetic sources; commit exact files.

### Task 5: Authenticated API and collected-result geo read

**Files:** Create `backend/app/api/routes/discovery.py`; modify `backend/app/api/router.py`, `backend/app/api/schemas.py`, `backend/app/services/geo_search.py`, and `backend/app/repositories/sqlalchemy_repositories.py`; test `tests/test_v2_api_discovery.py`, `tests/test_v2_geo_search_repositories.py`.

**Interfaces:** `POST /api/v2/me/search-profiles/{id}/discovery-runs` queues manual collection; `GET /api/v2/me/discovery-runs/{id}` returns owner-scoped status/health; the existing saved-search geo route returns only collected members when a catalog scope is selected. Preserve the open geo route's behavior.

- [ ] Write API tests for owner and non-owner run/search IDs, inactive search, duplicate manual trigger, pagination, partial-source status and no-user-data leakage. Write geo tests proving an uncollected global opportunity is absent from a collected saved-search view and an unlocated member remains in the list.
- [ ] Run focused tests and confirm missing routes/catalog filter fail.
- [ ] Add response models with typed status/counts and sanitized warning codes. Resolve owner from `CurrentSession`, never request body. Add a bounded membership predicate to the saved-search SQL query; preserve PostGIS predicates, pagination and the public geo API.
- [ ] Regenerate `frontend/openapi.json` and generated API types using the repository's existing command; run API and geo tests; commit exact files.

### Task 6: Map controls, visibility and bilingual states

**Files:** Create `frontend/app/composables/useDiscovery.ts` and `frontend/app/utils/discovery-messages.ts`; modify `frontend/app/pages/map.vue`, `frontend/app/utils/endpoints.ts`, `frontend/app/types/v2.ts`; test `frontend/tests/nuxt/pages/map.spec.ts`, `frontend/tests/nuxt/composables/useDiscovery.spec.ts`, `frontend/tests/nuxt/utils/map-projection.spec.ts`.

**Interfaces:** `useDiscoveryRun(searchProfileId)` exposes `refresh`, `status`, `sourceHealth` and `lastUpdated`; no map component receives source credentials or raw HTML.

- [ ] Write failing component/composable tests for manual refresh, pending/partial/success/error states, prior results remaining visible on failure, safe external URL handling, French/English copy and an unresolved offer that stays list-only.
- [ ] Run `npm --prefix frontend test -- useDiscovery map` and confirm targeted failures.
- [ ] Add run controls and status to the existing map page. There is no current frontend-wide i18n setup: create a typed `fr`/`en` dictionary for these new discovery states and an explicit locale choice on this surface, persisted as a non-secret preference. Do not claim this local catalog translates the rest of V2. Poll with a bounded interval only while a run is active, then invalidate the saved-search query. Escape source text through Vue bindings, allow only `https:`/`http:` job links, and do not construct raw HTML. Keep map/list selection in the existing store.
- [ ] Run frontend tests, typecheck and build; commit exact files.

### Task 7: End-to-end verification and gated handoff

**Files:** Update `docs/COUNTRY_PACKS.md`, `docs/MAP_EXPLORER.md`, `docs/OPERATIONS.md`; add `tests/test_v2_discovery_catalog_integration.py` and a Playwright local map test if existing fixtures support it.

**Interfaces:** No new runtime interface. This task proves the previously defined ones across Docker, API and UI.

- [ ] Add a fixture-only end-to-end scenario: active CH search → synthetic source sweep → durable run/provenance → saved-search list/map → source warning. Assert zero application records or outbound submissions.
- [ ] Run that scenario against local disposable Postgres/PostGIS and the API/frontend; confirm the fixture cannot reach employer hosts.
- [ ] Run `.venv/bin/pytest tests/test_v2_discovery_catalog*.py tests/test_v2_geo_search_repositories.py -q`, `.venv/bin/ruff check backend tests`, `.venv/bin/mypy backend country_packs`, `npm --prefix frontend test`, `npm --prefix frontend run typecheck`, and `npm --prefix frontend run build`; run migration upgrade/downgrade and `git diff --check`.
- [ ] Document exact supported CH sources and known gaps, especially LinkedIn/jobs.ch, the still-English portions of V2, and local morning-run setup. Commit docs/tests. Report changed files, migrations, tests, limitations and source health; request validation of this increment and **stop** before training catalog or another phase.
