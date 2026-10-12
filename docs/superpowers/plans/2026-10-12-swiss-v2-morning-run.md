# Swiss V2 Morning Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give one Swiss candidate an owner-scoped, scheduled or on-demand V2 discovery run whose multi-source results and failures are visible in Settings and the map.

**Architecture:** PostgreSQL owns a single CH morning schedule per user. One scheduler process evaluates due schedules in `Europe/Zurich` and uses the existing idempotent V2 discovery enqueue service; the general worker remains the only collector. Nuxt reads typed V2 run state and opportunities, while the V1 runner remains separate and visibly legacy.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2/Alembic, PostgreSQL/PostGIS, Nuxt 4/Vue 3/TypeScript, Vitest, Playwright, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-12-swiss-v2-morning-run-design.md`

## Global Constraints

- Implement only this morning-run repair; LLM wiring, application sending, final acceptance, and document quality are later gated repairs.
- Read the V2 documentation under `docs/` before changing architecture; preserve all pre-existing uncommitted files and never stage them by accident.
- CH only; LinkedIn stays disabled, `jobs.ch` is not added, and source presence is not evidence of permission or health.
- No real application submission, real LLM credential, external ATS, or employer contact in automated tests.
- A listing URL is not a verified application form. CAPTCHA/MFA/login/unknown fields always hand off to a person.
- Use session-derived ownership on every API read/write. The schedule does not grant submission authority.
- `JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED=false` throughout local tests.
- Do not claim multi-jobboard operation until two reviewed board/aggregator sources return valid Swiss listings in a bounded, authorized live dry-run.

## File map

`backend/app/domain/morning_schedule.py` owns schedule validation and due-time logic; `backend/app/repositories/morning_schedule.py` owns SQL access; revision `0029` adds the single-owner table. `backend/app/services/morning_run.py` owns batch enqueue and owner-scoped status assembly; `backend/app/api/routes/morning_run.py` exposes it; `backend/app/cli/morning_scheduler.py` ticks without a second queue. `frontend/app/composables/useMorningRun.ts` owns client state; `frontend/app/pages/settings.vue` renders it; the map keeps its existing V2 geo reads. Do not put schedule rules in Nuxt or V1 `server/scheduler.py`.

## Review Focus

- Swiss DST fallback: two UTC instants on the same Swiss date produce one MORNING task id.
- A schedule disabled after enqueue stops future enqueue, but does not pretend an already queued task was cancelled.
- One failed source preserves completed results and reports PARTIAL/source health.
- Another user cannot read or enqueue using this user's similarly named saved search.
- A listing URL without an `application_url` cannot create an automatic submission.
- A 202 enqueue response yields “queued”, never “completed”, in both locales.

---

### Task 1: Audit source eligibility and capture the baseline

**Files:**
- Create: `docs/SWISS_SOURCE_REVIEW_2026-10-12.md`
- Read: `docs/*.md`, `country_packs/ch/sources.yaml`, `backend/app/discovery/adapters/v1_catalog.py`, `pipeline/sources/*.py`
- Test: `tests/test_v2_source_registry.py`, `tests/test_v2_discovery_catalog.py`

**Interfaces:** Consumes the existing CH Country Pack. Produces an operator-reviewed list of source keys eligible for a bounded live dry-run; the document stores URLs/dates/conditions, never credentials or copied page bodies.

- [ ] **Step 1: Record the starting state.** Run `git status --short`, `python -m pytest tests/test_v2_source_registry.py tests/test_v2_discovery_catalog.py -q`, and read the V2 architecture/operations/source/application docs. Identify ignored or untracked work before editing.
- [ ] **Step 2: Review each candidate board/aggregator.** For `jobup`, `indeed_ch`, `jobscout24`, and `jooble`, record the official access terms, official API or permission route, request limits, current adapter transport, review date, and a decision of `approved`, `hold`, or `needs permission`. Do not run live collection for `hold` or `needs permission`.
- [ ] **Step 3: Write the reviewed-source table.** Use this exact shape so a missing permission is visible:

```markdown
| Source key | Official terms / API reference | Access decision | Rate limit | Review date | Reason |
| --- | --- | --- | --- | --- | --- |
```

- [ ] **Step 4: Check the gate.** If fewer than two job-board/aggregator sources are `approved`, stop live source testing, report the gap and request authorized integration access. Continue fixture-only implementation only if the user accepts that the multi-jobboard milestone remains unapproved.

### Task 2: Persist and validate one V2 morning schedule per user

**Files:**
- Create: `backend/app/domain/morning_schedule.py`
- Create: `backend/app/repositories/morning_schedule.py`
- Create: `backend/migrations/versions/rev_0029_morning_schedules.py`
- Modify: `backend/app/infrastructure/database/models.py`
- Test: `tests/test_v2_morning_schedule.py`, `tests/test_v2_morning_schedule_persistence.py`, `tests/test_v2_persistence_migrations.py`

**Interfaces:** `MorningSchedule(user_id: UserId, enabled: bool, local_time: time, cadence: Literal['daily','weekdays'], updated_at: datetime)`. Repository methods `get(user_id) -> MorningSchedule | None`, `upsert(schedule) -> MorningSchedule`, and `list_due(now: datetime, limit: int, offset: int) -> tuple[MorningSchedule, ...]` always bind owner or a bounded scheduler page. `is_due(schedule, now)` is pure and converts UTC to `Europe/Zurich`.

- [ ] **Step 1: Write failing domain tests** for `08:00` Zurich due/not due, weekends, both DST fold instants, disabled schedules, and invalid time/cadence. The production rule each test must break is `is_due`'s timezone/cadence predicate:

```python
assert is_due(schedule, datetime(2026, 10, 25, 7, 0, tzinfo=UTC))
assert not is_due(schedule.model_copy(update={"enabled": False}),
                  datetime(2026, 10, 25, 7, 0, tzinfo=UTC))
```

- [ ] **Step 2: Run red:** `python -m pytest tests/test_v2_morning_schedule.py -q`. Expected: import or missing-rule failure, not an unrelated fixture error.
- [ ] **Step 3: Implement domain rule and migration** with `morning_schedules.user_id` as FK to `users.id` and unique PK, `enabled`, validated `local_time`, `cadence`, `updated_at`; default absent schedule to disabled in the service, not a hidden database default. The predicate converts once via `now.astimezone(ZoneInfo('Europe/Zurich'))` and checks local weekday/time:

```python
def is_due(schedule: MorningSchedule, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo("Europe/Zurich"))
    return (schedule.enabled
            and (schedule.cadence == "daily" or local.weekday() < 5)
            and local.time().replace(tzinfo=None) >= schedule.local_time)
```
- [ ] **Step 4: Write failing persistence tests** proving owner-scoped get/upsert, a second user's 404-equivalent absence, pagination, and reversible upgrade/downgrade. Ensure `0029` revises `0028` without editing the existing untracked migration.
- [ ] **Step 5: Run red then green:** `python -m pytest tests/test_v2_morning_schedule.py tests/test_v2_morning_schedule_persistence.py tests/test_v2_persistence_migrations.py -q`. Expected: failures before repository wiring, then all pass.
- [ ] **Step 6: Verify migration ancestry before committing.** Revision `0028` is currently untracked user work. Do not silently stage it. If it remains untracked, leave `0029` uncommitted, report the dependency, and ask how to incorporate the earlier migration; otherwise commit only this task's exact files after `git diff --check` and targeted Ruff/mypy.

### Task 3: Expose owner-scoped V2 trigger, schedule, and status

**Files:**
- Create: `backend/app/services/morning_run.py`
- Create: `backend/app/api/routes/morning_run.py`
- Modify: `backend/app/api/router.py`, `backend/app/api/schemas.py`, `backend/app/api/dependencies.py`
- Modify: `backend/app/cli/run_discovery.py`
- Generated contract: `frontend/openapi.json`, `frontend/app/types/api.d.ts`
- Test: `tests/test_v2_morning_run_service.py`, `tests/test_v2_morning_run_api.py`, `tests/test_v2_discovery_catalog_integration.py`

**Interfaces:** `enqueue_active_ch(user_id, trigger: Literal['MANUAL','MORNING']) -> tuple[MorningRunItem, ...]`, where `MorningRunItem` has `search_profile_id`, `run_id`, and initial `QUEUED` state. `GET/PUT /api/v2/me/morning-schedule`, `POST /api/v2/me/morning-runs` (202), and `GET /api/v2/me/morning-runs/latest` use only `CurrentSession.user.id`; the latest response includes one V2 run state per active CH search plus source-health/warnings when terminal. CLI delegates to the same batch service rather than duplicating search selection.

- [ ] **Step 1: Write failing service tests** for zero eligible searches, two owned active CH searches, inactive/foreign search exclusion, same-day duplicate task IDs, and different Swiss days. Assert the returned identities and persisted queue rows, not mock call counts:

```python
first = await service.enqueue_active_ch(owner_id, trigger="MORNING")
again = await service.enqueue_active_ch(owner_id, trigger="MORNING")
assert [item.run_id for item in first] == [item.run_id for item in again]
assert len(first) == 2
```

- [ ] **Step 2: Run red:** `python -m pytest tests/test_v2_morning_run_service.py -q`.
- [ ] **Step 3: Implement batch service** by calling existing `DiscoveryEnqueueService.enqueue_discovery` inside one DB unit of work. Reuse `PersistedTaskDispatcher` idempotency. A search disabled between selection and enqueue is rechecked by that service. A `MANUAL` trigger does not mutate `MorningSchedule`. Its core loop should retain the profile/task mapping:

```python
for profile in await searches.list_for_user(user_id, active_only=True, limit=100):
    if "CH" not in countries_in_scope(profile):
        continue
    run_id = await enqueue.enqueue_discovery(user_id, profile.id, trigger=trigger)
    items.append(MorningRunItem(search_profile_id=profile.id, run_id=run_id,
                                status="QUEUED"))
```
- [ ] **Step 4: Write failing API tests** for session ownership, CSRF, invalid `HH:MM`/cadence, queued-vs-completed distinction, source warnings, and unknown run IDs. Use two users and assert the foreign user's data is absent:

```python
assert other_client.get('/api/v2/me/morning-runs/latest').json()['runs'] == []
assert owner_client.post('/api/v2/me/morning-runs').status_code == 202
```

- [ ] **Step 5: Run red then green:** `python -m pytest tests/test_v2_morning_run_api.py tests/test_v2_morning_run_service.py -q`. Add schemas/router/dependencies and retain existing `/me/search-profiles/{id}/discovery-runs` compatibility. The latest status must include queued task rows, not only completed `DiscoveryRun` rows.
- [ ] **Step 6: Re-run** `python -m pytest tests/test_v2_discovery_catalog_integration.py tests/test_v2_api_surface.py -q`; regenerate and check the OpenAPI TypeScript contract using `npm run gen:api` and `npm run check:api` from `frontend/`.
- [ ] **Step 7: Commit only task files** after checking the diff for accidental V1 behavior changes.

### Task 4: Add one durable Swiss scheduler process

**Files:**
- Create: `backend/app/cli/morning_scheduler.py`
- Modify: `docker-compose.yml`, `docs/OPERATIONS.md`
- Test: `tests/test_v2_morning_scheduler.py`, `tests/test_v2_morning_scheduler_compose.py`

**Interfaces:** `tick(factory, now: datetime, page_size: int = 100) -> int` pages due schedules and calls the Task 3 batch service with `MORNING`. CLI `python -m backend.app.cli.morning_scheduler --once` performs one tick; default service loops at a bounded interval. Docker `morning-scheduler` uses the general runtime image and workers profile, never the browser image or V1 scheduler.

- [ ] **Step 1: Write failing tests** for two scheduler replicas at the same instant, DST fallback, weekday cadence, disabled schedule after a queued task, a scheduler restart later that day, and pagination past 100 users. Assert one persisted task per user/profile/Swiss date.
- [ ] **Step 2: Run red:** `python -m pytest tests/test_v2_morning_scheduler.py -q`.
- [ ] **Step 3: Implement tick and loop** with `asyncio` cancellation propagation, bounded pages, UTC-aware clock, per-user transaction, and structured counts without user IDs or secrets in logs. Do not store `last_ran_at` as the sole idempotency guard: the existing task key is authoritative. The tick must call the same batch service as the API:

```python
for schedule in due_page:
    if is_due(schedule, now):
        await morning_runs.enqueue_active_ch(schedule.user_id, trigger="MORNING")
```
- [ ] **Step 4: Add Compose service and docs**: command `python -m backend.app.cli.morning_scheduler`, workers profile, API-equivalent database environment, no Docker socket or LLM secret. Document one-shot dry-run/real tick, worker dependency, and how to inspect a queued/partial result.
- [ ] **Step 5: Run green:** `python -m pytest tests/test_v2_morning_scheduler.py tests/test_v2_morning_scheduler_compose.py -q`; run `docker compose config --quiet` and a disposable-database `--once` smoke test. Never use the candidate's live database for this check.
- [ ] **Step 6: Commit only task files** after Ruff/mypy on new Python and `git diff --check`.

### Task 5: Replace misleading V1 morning controls with truthful V2 controls

**Files:**
- Create: `frontend/app/composables/useMorningRun.ts`
- Modify: `frontend/app/pages/settings.vue`, `frontend/app/utils/endpoints.ts`, `frontend/app/types/v2.ts`
- Test: `frontend/tests/nuxt/composables/useMorningRun.spec.ts`, `frontend/tests/nuxt/pages/settings.spec.ts`, `frontend/tests/e2e/settings-morning-run.spec.ts`

**Interfaces:** The composable consumes Task 3's schedule, trigger and latest-status endpoints and returns `saveSchedule`, `runNow`, `runs`, `busy`, and typed errors. The Settings screen shows V2 controls, per-search status/source health, and a link to `/map?profile=<id>`; existing V1 settings remain explicitly under a Legacy label without a “full V2 pipeline” claim.

- [ ] **Step 1: Write failing Vitest cases**: clicking “Run V2 discovery” posts `/api/v2/me/morning-runs`, never `/api/runs/full`; HTTP 202 renders “Queued”/“En attente”; terminal PARTIAL shows the failing source while healthy results remain available; an API error shows a localized alert and no success toast. Include 320 px layout and keyboard-visible action labels.
- [ ] **Step 2: Run red:** `npm test -- --run tests/nuxt/composables/useMorningRun.spec.ts tests/nuxt/pages/settings.spec.ts` from `frontend/`.
- [ ] **Step 3: Implement endpoint names and composable** using the typed API client; poll only queued/running items and stop on terminal states or component disposal. Distinguish network failure from zero matches. Keep `useDiscoveryRun` on the map and invalidate its relevant V2 query after completion. Its trigger should retain the server's 202 status:

```ts
async function runNow(): Promise<MorningRunBatch> {
  const queued = await apiPost<MorningRunBatch>(V2_ENDPOINTS.morningRuns)
  runs.value = queued.runs
  return queued
}
```
- [ ] **Step 4: Implement Settings UI** with independent V2 and Legacy sections. Save V2 schedule through its endpoint; do not map V1 `schedule_enabled` to it implicitly. Use FR/EN labels, `role="status"` for progress and `role="alert"` for failure, clear disabled/busy states, and mobile stacking.
- [ ] **Step 5: Run green:** the targeted Vitest command, `npm run typecheck`, and `npx playwright test tests/e2e/settings-morning-run.spec.ts --project=chromium` from `frontend/`. Fixture routing must reject every unexpected external request.
- [ ] **Step 6: Commit only task files** after verifying the existing V1 Settings tests still pass or updating their explicit Legacy assertions.

### Task 6: Prove the local multi-source path and report limitations

**Files:**
- Create: `docs/LOCAL_MORNING_RUN_VERIFICATION_2026-10-12.md`
- Modify: `docs/OPERATIONS.md` only if the smoke run exposes incorrect operator steps
- Test: `frontend/tests/e2e/local-candidate-workflow.spec.ts`, `tests/test_v2_discovery_catalog_integration.py`, `tests/test_v2_source_registry.py`

**Interfaces:** Produces an evidence report, not a production-readiness declaration. It records fixture outcomes, owner isolation, Docker worker/scheduler status, live dry-run source identities and health (only when Task 1 approved their use), and every skipped/unavailable check.

- [ ] **Step 1: Run full checks:** `python -m pytest -q`, `ruff check backend tests`, `mypy backend`, `npm test`, `npm run typecheck`, `npm run build`, `npm run check:api`, and the targeted Playwright scenarios. Record exit codes and counts; do not reuse historical results.
- [ ] **Step 2: Run a disposable local Docker rehearsal:** migrate an empty database, create two synthetic users and CH searches, start `worker` and `morning-scheduler --once`, inspect queued/terminal states, and assert no browser submission task or employer request occurred. Keep all credentials synthetic.
- [ ] **Step 3: If Task 1 approved at least two sources, run a bounded non-submitting live discovery dry-run** and record source keys, timestamp, request limits, valid CH result counts, warnings, and source health. Do not save candidate data to a third-party site. If access is not authorized or either source fails, mark this criterion unverified and stop short of the multi-jobboard claim.
- [ ] **Step 4: Review six Review Focus cases** against their automated tests, including `tests/test_v2_browser_automation.py` for a listing without a supported form. Inspect the mobile Settings and map in both locales, and confirm queue acknowledgement never appears as a completed candidature.
- [ ] **Step 5: Write and commit the verification report** with exact test results, migrations, changed files, risks, and unresolved blockers. Stop after this phase and request user validation under `docs/IMPLEMENTATION_PLAN.md`; do not start the LLM repair automatically.
