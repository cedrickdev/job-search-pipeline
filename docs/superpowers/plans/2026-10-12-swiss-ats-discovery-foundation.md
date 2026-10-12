# Swiss ATS Discovery Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing Swiss V2 Morning Run discover, persist, and display offers from explicitly reviewed Greenhouse and Lever employer boards without sending an application.

**Architecture:** A versioned, operator-reviewed board catalog supplies only approved employer tokens to the existing V2 source registry. The current source-access gate checks the catalog at enqueue and worker execution, while the existing discovery worker persists normalized opportunities and source health. Nuxt consumes only persisted V2 state. This is the first implementation plan for the approved autopilot spec; matching, document orchestration, and safe submission each require a later plan and phase validation.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, PostgreSQL/PostGIS, Nuxt 4, Vue 3, TypeScript, Vitest, Playwright, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-12-swiss-ats-morning-autopilot-design.md`

## Global Constraints

- Implement this discovery foundation only. Stop and request user validation before planning or executing the next phase under `docs/IMPLEMENTATION_PLAN.md`.
- CH only; LinkedIn, JobCloud and Indeed sources remain inactive without separately verified permission.
- A public posting feed is not authorization to automate its application form. Do not enable browser submission in this plan.
- No real employer submission, CAPTCHA/MFA bypass, anti-bot evasion, or live-source request in automated tests.
- No arbitrary crawl from an LLM. Only operator-reviewed board tokens are selectable; the catalog is default-empty and does not inherit unreviewed V1 `config/companies.yaml` entries.
- Existing uncommitted work belongs to the user. Stage only this phase's reviewed files; do not change V1 compatibility behavior or commit secrets.
- Keep the existing default-empty `JOBSEARCH_DISCOVERY_ALLOWED_SOURCES` gate. A source key alone never suffices when its reviewed board list is empty.
- Greenhouse and Lever are two ATS feed families, not two independent Swiss job boards or evidence of complete CH market coverage.

## File map

- `backend/app/discovery/ats_board_catalog.py` owns strict parsing and validation of reviewed board metadata; it never performs HTTP.
- `backend/app/repositories/ats_request_budget.py` owns atomic PostgreSQL reservations per board and Swiss day before external requests; migration `rev_0030` creates its ledger. `backend/app/discovery/adapters/budgeted_ats.py` wraps the legacy synchronous fetch adapter with an async pre-request reservation.
- `config/ats_reviewed_boards.yaml` is the human-reviewed, default-empty V2 source of truth; `config/companies.yaml` remains the V1 list.
- `backend/app/discovery/bootstrap.py` injects reviewed boards into `build_v1_sources` via its existing `boards` argument.
- `backend/app/services/discovery_source_access.py` refuses a configured ATS source whose reviewed board set is empty or held, both before enqueue and again at worker execution.
- `backend/app/api/dependencies.py`, `backend/app/cli/run_discovery.py`, `backend/app/cli/morning_scheduler.py`, and `backend/app/cli/run_worker.py` obtain the same startup catalog snapshot through one loader; no entry point may construct a weaker policy.
- `docs/SWISS_SOURCE_REVIEW_2026-10-12.md` records exact employer-board decisions, links, terms, request budgets and review date. `docs/OPERATIONS.md` explains activation and the bounded dry-run.
- Existing `backend/app/services/discovery_catalog.py`, `backend/app/tasks/handlers.py`, `backend/app/services/geo_search.py`, and Nuxt map/Settings pages remain consumers; touch them only if an acceptance test demonstrates a gap.

## Review Focus

1. An allowed `greenhouse` key with zero approved boards must refuse enqueue and contact no external host (Task 2 test).
2. An approved board returned with a non-CH location must not become a CH map marker (Task 4 test).
3. Greenhouse succeeds while Lever fails: preserve the Greenhouse offers and report Lever as failed, not “zero jobs” (Task 4 test).
4. A repeated Zurich morning tick with the same employer posting must retain one owner-scoped opportunity and one run identity (Task 4 test).
5. A newly edited catalog while a worker is running must not silently widen that run's board set; restart/requeue policy must be explicit (Task 2 test).

---

### Task 1: Record reviewed board access and the empty baseline

**Files:**
- Create: `config/ats_reviewed_boards.yaml`
- Modify: `docs/SWISS_SOURCE_REVIEW_2026-10-12.md`, `docs/OPERATIONS.md`
- Test: `tests/test_v2_discovery_source_access.py`

**Interfaces:** Produces a review record for each proposed board: `source_key`, `token`, `company`, `country`, `provenance_url`, `reviewed_at`, `access_decision`, `request_budget_per_day`, and `conditions_url`. Only `access_decision: approved` may become runnable; absence defaults to no board. The review is engineering evidence, not legal advice.

- [ ] **Step 1: Capture the baseline.** Record `git status --short`, the current `config/companies.yaml` and CH bindings, `JOBSEARCH_DISCOVERY_ALLOWED_SOURCES`, and whether the existing local V2 scheduler is disabled. Run `python -m pytest tests/test_v2_discovery_source_access.py -q` and record the result.
- [ ] **Step 2: Inspect candidate employer boards.** Use official Greenhouse/Lever posting API documentation, the employer's published career page, and applicable access conditions. Record the exact CH posting location, board token and provenance without copying applicant data or making application submissions. A board with unclear collection/storage conditions remains `hold` and stays absent from the approved list.
- [ ] **Step 3: Write the default-empty review catalog.** Use this schema; record eligible real boards in the review document, but keep the runnable catalog empty until Task 3 has installed the durable request budget:

```yaml
version: 1
boards:
  greenhouse: []
  lever: []
```

- [ ] **Step 4: Document the activation procedure.** Set `JOBSEARCH_DISCOVERY_ALLOWED_SOURCES=greenhouse,lever` only after Task 3 enforces the request budget and both catalog entries have `approved` decisions; restart the API and worker so both read the same catalog version; use the CLI's bounded non-submitting discovery mode; inspect source health before enabling the resident schedule. If no CH boards qualify, report the gate as unverified and do not set the env allow-list.
- [ ] **Step 5: Verify and commit only documentation/configuration.** Run `git diff --check`; inspect `git diff --cached --name-only` before committing. Do not include any private token, applicant data, or unrelated dirty file.

### Task 2: Make the reviewed catalog a fail-closed runtime input

**Files:**
- Create: `backend/app/discovery/ats_board_catalog.py`, `tests/test_v2_ats_board_catalog.py`
- Modify: `backend/app/discovery/bootstrap.py`, `backend/app/services/discovery_source_access.py`, `backend/app/api/dependencies.py`, `backend/app/cli/run_discovery.py`, `backend/app/cli/morning_scheduler.py`, `backend/app/cli/run_worker.py`
- Test: `tests/test_v2_discovery_source_access.py`, `tests/test_v2_discovery_adapters.py`

**Interfaces:** `load_reviewed_boards(path: Path) -> ReviewedAtsCatalog` returns immutable `boards: Mapping[str, tuple[ReviewedBoard, ...]]`, `reviews` metadata and `version: int`. `ReviewedBoard` provides the existing `CompanyBoard` protocol (`token`, `company`) plus review metadata. `SourceAccessPolicy.from_env(catalog: ReviewedAtsCatalog)` and `sources_for(profile)` require a nonempty approved board tuple for ATS keys; existing non-ATS source checks remain unchanged. `build_source_registry(..., boards=catalog.boards)` is the V2 composition point. The catalog argument is mandatory so no caller can accidentally bypass board review.

- [ ] **Step 1: Write red catalog tests** for empty default, approved Greenhouse/Lever entry, duplicate `(source_key, token)`, unknown source, unsupported country, malformed URL, zero/negative request budget, stale/absent review timestamp, and `hold` entry exclusion. Assert an invalid catalog fails startup rather than silently broadening access:

```python
catalog = load_reviewed_boards(path)
assert catalog.boards["greenhouse"] == ()
assert catalog.boards["lever"] == ()
```

- [ ] **Step 2: Run red.** `python -m pytest tests/test_v2_ats_board_catalog.py -q` must fail because the new loader does not exist, not because a network request escaped.
- [ ] **Step 3: Implement the pure loader** using the existing YAML dependency, `Path`, frozen dataclasses/Pydantic validation, exact `CH` country, `approved`/`hold` enum, bounded board token length and strict HTTPS provenance/conditions URLs. Do not embed an API key in a URL or error. Preserve the full review record while exposing only approved `CompanyBoard` instances to `build_source_registry`.

```python
@dataclass(frozen=True)
class ReviewedBoard:
    token: str
    company: str
    source_key: Literal["greenhouse", "lever"]
    country: Literal["CH"]
    provenance_url: str
    conditions_url: str
    access_decision: Literal["approved", "hold"]
    reviewed_at: date
    request_budget_per_day: int

@dataclass(frozen=True)
class ReviewedAtsCatalog:
    version: int
    boards: Mapping[str, tuple[ReviewedBoard, ...]]
    reviews: tuple[ReviewedBoard, ...]
```
- [ ] **Step 4: Write red access tests** proving env `greenhouse` with empty catalog raises `DiscoverySourceAccessDenied`; an approved token plus env selects it; a `hold` entry is not selected; unknown/disabled source is still refused; worker-side policy re-evaluation blocks a task queued before catalog removal and worker restart. Also pin the Review Focus hot-reload rule: API and worker use a startup snapshot and require restart after catalog edits.
- [ ] **Step 5: Wire every V2 entry point.** Keep the V1 loader and `config/companies.yaml` untouched. Inject `catalog.boards` through `build_source_registry`; pass a loaded catalog to `SourceAccessPolicy.from_env` in API, per-user CLI, scheduler and general worker. A task does not carry permission merely because it was queued earlier. Use one resolved config path in all processes and document restart coordination. The existing API error code stays `discovery_source_unapproved`.

```python
catalog = load_reviewed_boards(ATS_REVIEWED_BOARDS_PATH)
source_policy = SourceAccessPolicy.from_env(catalog)
registry = build_source_registry(packs=packs, boards=catalog.boards)
# sources_for(profile) raises when greenhouse/lever is selected but its
# reviewed board tuple is empty, even when its source key is in the env list.
```
- [ ] **Step 6: Run green and commit.** `python -m pytest tests/test_v2_ats_board_catalog.py tests/test_v2_discovery_source_access.py tests/test_v2_discovery_adapters.py -q`, then `ruff check backend/app/discovery/ats_board_catalog.py backend/app/services/discovery_source_access.py` and `mypy backend`. Commit only exact Task 2 files after `git diff --check`.

### Task 3: Enforce a durable per-board request budget

**Files:**
- Create: `backend/app/repositories/ats_request_budget.py`, `backend/app/discovery/adapters/budgeted_ats.py`, `backend/migrations/versions/rev_0030_ats_request_budget.py`, `tests/test_v2_ats_request_budget.py`
- Modify: `backend/app/discovery/bootstrap.py`, `backend/app/discovery/contracts.py`, `backend/app/discovery/failures.py`, `backend/app/cli/run_worker.py`
- Test: `tests/test_v2_persistence_migrations.py`, `tests/test_v2_discovery_adapters.py`

**Interfaces:** `AtsRequestBudget.reserve(source_key: str, board_token: str, swiss_date: date, limit: int) -> bool` atomically increments one PostgreSQL row `(source_key, board_token, swiss_date)` only below `limit`. `BudgetedAtsSourceAdapter` implements `OpportunitySource`: for each approved board it awaits a reservation, then invokes a one-board `V1AtsSourceAdapter.discover`; it combines successful opportunities and normalized source health. `healthcheck` makes a charged request or returns an unprobed status. Denial/database failure emits fixed `SOURCE_BUDGET_EXHAUSTED`/`SOURCE_BUDGET_UNAVAILABLE` warning codes and makes no HTTP request. Reservations count attempts even if the upstream call fails. V1 direct fetch functions remain unchanged. No V2 ATS adapter with a nonempty board list may be composed without this budget port.

- [ ] **Step 1: Write red concurrent repository tests**: two workers racing for the final slot yield exactly one `True`; the next Swiss day opens a new bucket; limits `0` and negative values refuse; a database outage refuses without contacting the source. Assert persisted count, not only return values:

```python
allowed = await asyncio.gather(
    budget.reserve("greenhouse", "fixture-board", swiss_day, 1),
    budget.reserve("greenhouse", "fixture-board", swiss_day, 1),
)
assert allowed.count(True) == 1
```

- [ ] **Step 2: Run red.** `python -m pytest tests/test_v2_ats_request_budget.py -q` must fail on the missing repository/migration, not because PostgreSQL is unavailable.
- [ ] **Step 3: Implement the ledger and adapter wrapper.** Add a unique constraint on `(source_key, board_token, swiss_date)` and use a single atomic upsert/conditional update under PostgreSQL. Inject the budget port into `BudgetedAtsSourceAdapter` in the V2 registry composition; each board's `request_budget_per_day` comes from the reviewed catalog. Use `ZoneInfo("Europe/Zurich")` to derive the day. Reuse the existing one-board V1 fetch and normalization, combine results deterministically, and keep the failure/metrics contract accurate. A denied reservation produces no network call:

```python
reserved = await self._budget.reserve(
    self.metadata.source_key, board.token, swiss_day, board.request_budget_per_day
)
if not reserved:
    return DiscoveryResult(
        health=SourceHealth(
            source_key=self.metadata.source_key,
            status=SourceHealthStatus.UNAVAILABLE,
            checked_at=self._clock(),
            reason=SourceFailureCode.SOURCE_BUDGET_EXHAUSTED,
            detail="daily ATS request budget exhausted",
        )
    )
return await self._one_board_adapters[board.token].discover(request)
```

- [ ] **Step 4: Run green and review migration ancestry.** Run the budget tests plus `tests/test_v2_persistence_migrations.py` and `tests/test_v2_discovery_adapters.py`; run Ruff/mypy. Revision `0030` depends on `0029`, which was untracked in the 2026-10-12 baseline. Never commit an orphan migration: first verify that the prerequisite revision is committed or explicitly coordinate its inclusion with the user.
- [ ] **Step 5: Commit exact Task 3 files** only when migration ancestry is complete and `git diff --check` passes. Otherwise leave the migration uncommitted and report this phase as blocked on ancestry rather than claiming readiness.

### Task 4: Prove CH normalization, partial source results and idempotency

**Files:**
- Modify only if red tests expose defects: `backend/app/discovery/adapters/v1_sources.py`, `backend/app/discovery/normalization.py`, `backend/app/services/discovery_catalog.py`, `backend/app/tasks/handlers.py`
- Test: `tests/test_v2_discovery_adapters.py`, `tests/test_v2_discovery_catalog_integration.py`, `tests/test_v2_morning_run_service.py`

**Interfaces:** Existing `DiscoveryEnqueueService.enqueue_discovery(user_id, search_profile_id, trigger)` and worker results remain the only discovery route. The persisted `Opportunity` has a stable source posting ID, company, original URL, country/location confidence, and source-health record. No browser task is enqueued by this phase.

- [ ] **Step 1: Write failing fixture tests** for a Greenhouse CH posting, a Lever CH posting, a non-CH posting, malformed/missing location, duplicated source ID, repeated MORNING tick, and Lever timeout after Greenhouse success. Use fixture HTTP responses only and assert database rows/health codes, not mock call counts:

```python
assert {row.source_key for row in stored_sources} == {"greenhouse", "lever"}
assert all(row.country == "CH" for row in swiss_results)
assert len({row.id for row in repeated_run_results}) == len(repeated_run_results)
assert browser_task_count == 0
```

- [ ] **Step 2: Run red.** `python -m pytest tests/test_v2_discovery_adapters.py tests/test_v2_discovery_catalog_integration.py tests/test_v2_morning_run_service.py -q`; require the new assertions to fail for a named normalization/health behavior.
- [ ] **Step 3: Apply the narrowest fix** in the existing normalization or persistence boundary. Preserve original listing and verified apply URL as distinct fields; do not invent coordinates or treat a generic employer HQ as the job location. A source with no reviewed boards is refused before enqueue with `discovery_source_unapproved`, not reported as a healthy empty market.

```python
# The existing `opportunity_from_posting` remains the sole V1→V2 converter.
# For ATS feeds, a CH sweep keeps explicit/verified CH locations; an explicit
# foreign country is excluded; unknown locations remain list-only/reviewable.
# The existing source URL and application URL fields stay distinct.
opportunity = opportunity_from_posting(
    posting, metadata=metadata, pack=ch_pack, fetched_at=clock()
)
assert opportunity.source.source_url is not None
```
- [ ] **Step 4: Run green and commit.** Re-run the three suites, `ruff check backend tests`, `mypy backend`, and the relevant PostgreSQL integration tests. Verify repeated Zurich dates and two-user isolation. Commit only exact changed code/test files.

### Task 5: Show source coverage truthfully in the existing V2 UI

**Files:**
- Modify only if tests expose defects: `frontend/app/pages/settings.vue`, `frontend/app/components/map/MapList.vue`, `frontend/app/components/map/OpportunityMap.vue`, `frontend/app/composables/useMorningRun.ts`
- Test: `frontend/tests/nuxt/composables/useMorningRun.spec.ts`, `frontend/tests/e2e/settings-morning-run.spec.ts`, `frontend/tests/e2e/local-candidate-workflow.spec.ts`

**Interfaces:** Nuxt consumes the existing owner-scoped V2 status and geo endpoints. It must distinguish `QUEUED`, `RUNNING`, `PARTIAL`, `FAILED`, `COMPLETED`, the existing `discovery_source_unapproved` 409 error, and an empty but healthy result. Its map renders only geocoded CH opportunities; the list retains unlocated CH offers.

- [ ] **Step 1: Write failing FR/EN component and browser tests** for empty approved catalog, a 202 queued trigger, two-source partial coverage, valid offer without coordinates, real geo markers, and API outage. Assert the backend-derived source name/count and accessible status text; reject an optimistic “sent” message:

```ts
expect(screen.queryByText(/candidature envoyée|application sent/i)).toBeNull()
expect(screen.getByRole('status').textContent).toMatch(/en attente|queued/i)
```

- [ ] **Step 2: Run red.** From `frontend/`, run `npm test -- --run tests/nuxt/composables/useMorningRun.spec.ts`, then `npx playwright test tests/e2e/settings-morning-run.spec.ts --project=chromium`. Unexpected API or external requests must fail the test route.
- [ ] **Step 3: Wire truthful statuses** through the typed V2 API client and localized UI, without business logic in Nuxt. The `discovery_source_unapproved` error explains that no reviewed CH ATS board is active. An unlocated offer remains in the list and does not receive fabricated map coordinates. At 320, 390, 768 and 1440 px, controls remain operable by keyboard and touch.
- [ ] **Step 4: Run green and commit.** Repeat targeted Vitest/Playwright, `npm run typecheck`, `npm run build`, and `npm run check:api` if the backend API shape changed. Commit only exact Task 4 files.

### Task 6: Isolated local rehearsal and phase handoff

**Files:**
- Create: `docs/LOCAL_SWISS_ATS_DISCOVERY_VERIFICATION_2026-10-12.md`
- Modify: `docs/OPERATIONS.md` only if the rehearsal reveals wrong operator instructions
- Test: all Task 2–5 suites, `tests/test_v2_browser_automation.py` as a non-submit regression

**Interfaces:** Produces an evidence report with test counts, migration state, source access decisions, fixture counts, live dry-run status, UI status, known limits, and exact file list. It does not claim application readiness.

- [ ] **Step 1: Run final checks** on the final tree: `python -m pytest -q`, `ruff check backend tests`, `mypy backend`, Nuxt Vitest, typecheck/build, OpenAPI contract check, Playwright fixture suite, `docker compose config --quiet`, and `git diff --check`. Record exact outputs and any unavailable check.
- [ ] **Step 2: Rehearse in an isolated Compose project** with a fresh verified `JOBSEARCH_VOLUME_PREFIX`, synthetic users and CH searches, and fake Greenhouse/Lever responses. Confirm worker terminal status, source health, owner isolation, list/map data, and zero browser/application tasks. Never mount or mutate the existing candidate database for this rehearsal.
- [ ] **Step 3: Run a bounded live discovery check only if both sources were reviewed and approved** in Task 1. Record employer board keys, timestamp, request count/budget, CH result counts, warnings and storage terms. If approval or source response is missing, write `UNVERIFIED` and make no live request; do not relabel fixture success as live coverage.
- [ ] **Step 4: Publish the phase report and stop.** Include implemented behavior, changed files, migration `0030` and its ancestry, tests/checks, risks, and the exact remaining gates for document generation and safe Playwright submission. Ask the user to validate this phase before writing or executing the next plan.
