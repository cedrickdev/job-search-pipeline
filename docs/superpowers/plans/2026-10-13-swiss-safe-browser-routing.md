# Swiss Safe Browser Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect prepared Swiss Morning Run dossiers to an auditable browser application and permit a simulated unattended send only for an exact, operator-approved Greenhouse or Lever form variant.

**Architecture:** Reuse the existing owner-scoped application service, durable task queue, and isolated browser worker. A default-empty operator catalog narrows the browser adapter's review-only ceiling for one exact prepared form; it never upgrades an ATS family globally. A browser-lane preparation task is enqueued atomically with the final document checkpoint, then creates a committed application, prepares it in a separate transaction, and conditionally queues the existing submission task. Live employer sends remain disabled in this phase.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL/PostGIS, Playwright Chromium fixtures, Nuxt 4/Vue 3/TypeScript, pytest, Ruff, mypy, Vitest, Playwright Test, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-13-swiss-safe-browser-routing-design.md`

## Global Constraints

- Implement this phase only, then test, report, request validation and stop under `docs/IMPLEMENTATION_PLAN.md`.
- CH only; LinkedIn, JobCloud and Indeed remain disabled; live Greenhouse/Lever source and form access remain unapproved.
- `JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED` remains `false` in packaged defaults and automated tests. Fixture-only injection must not contact employers.
- CAPTCHA, MFA, login walls, sensitive or unknown required questions, changed forms, unknown uploads or missing confirmation never become an automatic success.
- An LLM can draft typed answers but cannot activate a capability, change policy or authorize a click. Pinned rendered documents must match the Morning Run's exact version IDs.
- Preserve V1 and the current dirty checkout. Migration `0031` depends on currently untracked `0026`–`0030`; do not commit a new migration without reviewing and committing its full ancestry. For this checkout, task gates use targeted diff reviews in place of per-task commits until that ancestry is resolved.
- A queued task is not a submitted application. An ambiguous click or crashed in-flight attempt is `SUBMISSION_STATE_UNKNOWN`, never an automatic retry.

## Review Focus

1. A previously rendered CV followed by a newer manual CV version must still pin the Morning Run's exact CV, not the new latest version (Task 2 test).
2. A browser subprocess must see the committed application before form navigation; a crash after creation must redeliver without creating a second application (Task 3 test).
3. An approved fingerprint on a different job URL, driver version or employer must not gain unattended authority (Task 1 and Task 4 tests).
4. Two search profiles and two workers encountering the same job at the daily cap must create one application and at most one attempt (Task 4 test).
5. A policy pause or capability deactivation after queueing but before the fixture click must cause zero clicks; a pause racing after the last check remains a documented live-activation blocker (Task 4 test).

---

### Task 1: Default-deny ATS form capability catalog

**Files:**
- Create: `backend/app/application_engine/browser/form_capabilities.py`
- Create: `config/ats_approved_forms.yaml` (packaged default: `version: 1`, `forms: []`)
- Modify: `backend/app/core/settings.py` (operator override path, default packaged path)
- Test: `tests/test_v2_form_capabilities.py`

**Interfaces:**
- `load_form_capabilities(path: Path, *, today: date) -> FormCapabilityCatalog` rejects malformed or stale operator files and does no network I/O.
- `FormCapabilityCatalog.resolve(target_url: str | None, driver_key: str | None, fingerprint: str | None) -> FormCapabilityDecision` returns `FULLY_SUPPORTED` plus a catalog ID only for an exact approved HTTPS target, driver and fingerprint; otherwise `SUPPORTED_WITH_REVIEW` with a fixed reason code. `FormCapabilityCatalog.allows_questions(capability_id: str, questions: tuple[ApplicationQuestion, ...]) -> bool` checks the reviewed required-field and upload-slot sets during preparation and in the browser child before a click.
- Input fields: `id`, `country="CH"`, `ats` (`greenhouse`/`lever`), `employer`, `target_url`, `driver_key`, `form_fingerprint`, `allowed_required_fields`, `allowed_upload_slots`, `conditions_url`, `review_id`, `reviewed_at`, `access_decision` (`approved`/`hold`). No secrets or page bodies.

- [ ] **Step 1: Write failing parser/resolver tests.** A catalog with one fixture entry resolves its exact tuple, but a changed URL path, query, driver or fingerprint, stale review, duplicate ID, non-HTTPS URL, credentials in URL, missing file and empty packaged catalog all deny unattended authority.

```python
decision = catalog.resolve("https://jobs.lever.co/fixture/role/apply", "lever/1", "sha256:fixture")
assert decision.safety_level is AdapterSafetyLevel.FULLY_SUPPORTED
assert catalog.resolve("https://jobs.lever.co/other/role/apply", "lever/1", "sha256:fixture").capability_id is None
```

- [ ] **Step 2: Run** `.venv/bin/pytest -q tests/test_v2_form_capabilities.py` and confirm the missing module is the failure.
- [ ] **Step 3: Implement a strict immutable catalog.** Canonicalize with `urllib.parse.urlsplit`, reject userinfo, fragments, redirects-as-authority and noncanonical URLs; use exact normalized origin/path/query equality, not suffix or regex matching. Validate CH/ATS/driver consistency, review freshness (at most 180 days), duplicate IDs and field-name allowlists. The default file is empty. Never log raw file content.

```python
@dataclass(frozen=True)
class FormCapabilityDecision:
    safety_level: AdapterSafetyLevel
    capability_id: str | None
    reason_code: str
```

- [ ] **Step 4: Re-run the new test, Ruff and mypy.** Review only these paths with `git diff`; do not activate a live entry.

### Task 2: Scoped authority and exact document pins in the application service

**Files:**
- Modify: `backend/app/application_engine/contracts.py` (`AdapterPreparation.driver_key`)
- Modify: `backend/app/application_engine/adapters/browser.py` (preserve `BrowserTaskResult.driver_key`)
- Modify: `backend/app/domain/application.py`, `backend/app/infrastructure/database/models.py`, `backend/app/infrastructure/database/mappers.py` (persist prepared driver key and matched capability ID)
- Modify: `backend/app/services/applications.py` (inject catalog resolver, exact versions)
- Modify: `backend/app/application_engine/browser/context_loader.py`, `backend/app/application_engine/browser/execute.py` (pre-navigation driver identity check)
- Create: `backend/migrations/versions/rev_0032_swiss_browser_routing.py` (application driver field plus Task 3's new task kind; its final contents are reviewed with Task 3)
- Test: `tests/test_v2_application_engine.py`, `tests/test_v2_persistence_application_engine.py`, `tests/test_v2_browser_automation.py`

**Interfaces:**
- `ApplicationService.prepare(user_id, app_id, *, now, required_versions: Mapping[CandidateDocumentType, DocumentVersionId] | None = None) -> Application`; only the Morning Run handler supplies `required_versions`.
- `ApplicationService` receives a `FormCapabilityCatalog` or default-deny resolver. Its gate input is the resolved per-application safety level, not a global edit to `BrowserApplicationAdapter.capabilities`.
- The persisted `Application.form_driver_key`, `form_capability_id` and `form_fingerprint` are rechecked against the catalog by the browser child before navigation; a changed driver or fingerprint requires human review.

- [ ] **Step 1: Write failing tests.** Cover changed latest CV, foreign/invalid version IDs, missing driver key, a catalog match on exact prepared form, default catalog review-only, changed target and wrong driver before `page.goto`, and deactivation before `reserve_submission` / `load_reserved_execution`.

```python
required_versions = {
    CandidateDocumentType.RESUME: checkpoint.resume_version_id,
    CandidateDocumentType.COVER_LETTER: checkpoint.cover_letter_version_id,
}
prepared = await service.prepare(owner, app.id, now=now, required_versions=required_versions)
assert {pin.document_type: pin.version_id for pin in prepared.pinned_documents} == required_versions
```

- [ ] **Step 2: Run** the focused tests and confirm that the current service instead pins the latest version and keeps the browser ceiling at `SUPPORTED_WITH_REVIEW`.
- [ ] **Step 3: Implement the smallest service and persistence changes.** Pass `driver_key` through the typed browser preparation result; persist it with the fingerprint and capability ID. Resolve catalog authority only after structural preparation, reviewed question-set validation and answer validation, and re-resolve at reservation and reserved execution. Required versions must be rendered, evidence-guarded, owned by the app's profile and opportunity, and equal to the checkpoint IDs. The browser child compares the detected driver key with the persisted key before any navigation. Do not modify the global browser adapter safety level.

```python
effective_safety = form_catalog.resolve(
    context.opportunity.application_url, app.form_driver_key, app.form_fingerprint,
).safety_level if app.channel is ApplicationChannel.BROWSER else adapter.capabilities.safety_level
```

- [ ] **Step 4: Run** the application, persistence and browser fixture suites; run Ruff/mypy. Review the `0032` application-column upgrade/downgrade against `0031` before the next task extends that migration.

### Task 3: Durable Morning Run to browser preparation handoff

**Files:**
- Modify: `backend/app/domain/task.py`, `backend/app/infrastructure/database/models.py`, `backend/migrations/versions/rev_0032_swiss_browser_routing.py` (add `APPLICATION_PREPARATION` to the browser lane and constraints)
- Modify: `backend/app/services/morning_documents.py` (enqueue in the same transaction as final letter checkpoint)
- Create: `backend/app/services/morning_application.py` (commit create, then prepare in a distinct session; idempotent recovery)
- Modify: `backend/app/tasks/handlers.py`, `backend/app/cli/run_worker.py` (browser-lane preparation handler and composition)
- Test: `tests/test_v2_morning_application.py`, `tests/test_v2_tasks_handlers.py`, `tests/test_v2_worker_cli.py`, `tests/test_v2_persistence_migrations.py`

**Interfaces:**
- `TaskKind.APPLICATION_PREPARATION` belongs to `TaskLane.BROWSER`; key `morning-application:<owner>:<run>:<opportunity>` and payload `{discovery_run_id, opportunity_id}`.
- `MorningApplicationService.prepare(user_id: UserId, run_id: DiscoveryRunId, opportunity_id: OpportunityId, *, now: datetime) -> Application | Preparation` verifies owned CH run membership and `DOCUMENTS_RENDERED` checkpoint, commits `ApplicationService.create`, then calls `ApplicationService.prepare` with exact version IDs in a second unit of work. If another run already prepared, queued or finished an application for that opportunity, reuse it and never repin a newer dossier after approval.
- A `reconcile_ready` batch scans already-rendered owner checkpoints and idempotently enqueues missing preparation tasks; it does not submit.

- [ ] **Step 1: Write failing integration tests.** Final letter and browser task commit atomically; duplicate document handler/reconciler produces one task; a forged owner/member fails before browser navigation; the browser child reads a committed application; a crash after `create` redelivers to the same application; the browser lane never accepts a general-lane kind.

```python
assert lane_for_kind(TaskKind.APPLICATION_PREPARATION) is TaskLane.BROWSER
assert queued.payload == {"discovery_run_id": str(run_id), "opportunity_id": str(opportunity_id)}
assert await applications.get_by_idempotency_key(owner, key) == first_application
```

- [ ] **Step 2: Run** the new integration tests and migration test to observe the missing kind/service/schema behavior.
- [ ] **Step 3: Enqueue `TaskSpec` via the document-stage session, then wire the browser handler.** `MorningDocumentsService` gets an injected `PersistedTaskDispatcher` built from that same session; it enqueues immediately after the letter checkpoint and before commit. The handler uses separate committed sessions for `create` and `prepare`, because a subprocess cannot read an uncommitted application. Serialize preparation for the same application with a database row lock before changing state; a second run reuses a prepared or terminal application rather than repinning it. On redelivery, inspect persisted state/pins before deciding whether reversible preparation is still needed. Reconciliation uses bounded keyset pagination and the same deterministic key.

```python
await dispatcher.enqueue(TaskSpec(
    kind=TaskKind.APPLICATION_PREPARATION,
    idempotency_key=f"morning-application:{user_id}:{run_id}:{opportunity_id}",
    user_id=user_id,
    payload={"discovery_run_id": str(run_id), "opportunity_id": str(opportunity_id)},
    max_attempts=2,
))
```
- [ ] **Step 4: Complete migration `0032`.** Update the `task_kind` and lane-kind check constraints for `APPLICATION_PREPARATION`; downgrade refuses or explicitly reconciles rows of that kind before dropping the value. Verify upgrade/downgrade on a fresh isolated PostGIS database and run the worker/handler tests.

### Task 4: Conditional queueing and final browser safety gate

**Files:**
- Modify: `backend/app/services/morning_application.py`, `backend/app/services/application_queue.py`, `backend/app/services/applications.py`
- Modify: `backend/app/tasks/handlers.py`, `backend/app/application_engine/browser/execute.py`, `backend/app/cli/run_worker.py`
- Test: `tests/test_v2_morning_application.py`, `tests/test_v2_application_engine.py`, `tests/test_v2_browser_automation.py`, `tests/test_v2_persistence_application_engine.py`

**Interfaces:**
- After reversible `prepare`, an `APPROVED` browser application can enqueue the existing `APPLICATION_SUBMISSION` task only through `ApplicationQueueService.enqueue_approved`; that service rereads the policy, CH target, exact capability and live switch. `READY_FOR_REVIEW` / `REQUIRES_HUMAN` never enqueue.
- `load_reserved_execution` and the browser child recheck the capability and form identity before a click. The existing reservation/attempt IDs remain the only irreversible audit authority.

- [ ] **Step 1: Write failing tests.** Two searches for one target and concurrent workers share one app/attempt and one daily slot; default live-off gives zero submit tasks/clicks; policy pause, target edit, catalog hold, changed fingerprint, CAPTCHA/MFA/sensitive question, exhausted cap and missing pinned PDF all give zero clicks. A confirmed fixture gives one click/receipt; post-click lost confirmation or crash yields terminal unknown and zero retry clicks.

```python
assert await count_attempts(owner, application.id) == 1
assert fixture_submit_clicks == 1
assert unknown.state is ApplicationState.SUBMISSION_STATE_UNKNOWN
assert await worker.run_until_idle() == 0  # no automatic second click
```

- [ ] **Step 2: Run** these tests against the current review-only wiring; verify they fail for the expected missing scoped route rather than for fixture setup.
- [ ] **Step 3: Add conditional queueing and rechecks.** The browser-lane preparation handler calls the queue service only when the application is `APPROVED` and the injected live switch is true; no direct `TaskSpec(APPLICATION_SUBMISSION)` shortcut. The queue service reloads capability/policy; the submission worker rechecks at reservation, reserved execution and inside the browser child before click. A pre-click refusal is audited without a click. Never retry an uncertain result.

```python
if application.state is ApplicationState.APPROVED and live_enabled:
    await queue_service.enqueue_approved(user_id, application.id)
```
- [ ] **Step 4: Run** fixture Chromium, concurrency and worker tests. Explicitly test a pause between queued and reserved states and a deactivation between reservation and page action. Record the non-atomic final pause/click window as a live-activation blocker; do not set the production switch to true.

### Task 5: Owner-scoped progress and bilingual UI

**Files:**
- Modify: `backend/app/services/morning_run.py`, `backend/app/repositories/morning_run.py`, `backend/app/api/routes/morning_run.py`, `backend/app/api/schemas.py` (application/task projection with fixed blockers)
- Modify: `frontend/app/composables/useMorningRun.ts`, `frontend/app/pages/settings.vue`, `frontend/app/pages/applications.vue`
- Regenerate: `frontend/openapi.json`, `frontend/app/types/api.d.ts`
- Test: `tests/test_v2_morning_run_api.py`, `frontend/tests/nuxt/composables/useMorningRun.spec.ts`, `frontend/tests/nuxt/pages/settings.spec.ts`, `frontend/tests/e2e/settings-morning-run.spec.ts`, `frontend/tests/e2e/applications.spec.ts`

**Interfaces:**
- Read-only `/api/v2/me/morning-runs/latest` exposes `application_id`, actual application state, capability/queue state and fixed blocker code per owner/run member. Application events remain the source of truth for confirmation; the Morning Run response links to them and the exact document versions.

- [ ] **Step 1: Write failing API/UI tests.** Two users sharing an opportunity cannot see each other's application; queued is announced as queued, not sent; disabled live switch, review required, CAPTCHA, unknown and confirmed each have distinct FR/EN text and screen-reader status. Cover 320, 390, 768 and desktop widths with no horizontal overflow.

```python
assert owner_payload["preparations"][0]["application_id"] == str(owner_application.id)
assert foreign_payload["preparations"] == []
```

- [ ] **Step 2: Run** targeted Python/Vitest/Playwright tests and confirm the missing projection/copy is the cause of failure.
- [ ] **Step 3: Project owner-scoped application and task rows without an N+1 query.** Keep `preparations_truncated` semantics. Add typed FR/EN labels, exact PDF links and a link to the existing application audit trail. Poll while a preparation/submit task is active, stop on terminal failure, and keep button states keyboard accessible.
- [ ] **Step 4: Regenerate and compare OpenAPI/types; run targeted tests, Nuxt typecheck and production build.** A 202 response never renders as submitted.

### Task 6: Isolated acceptance, documentation and phase handoff

**Files:**
- Create: `tests/test_v2_morning_browser_integration.py` (synthetic owner-to-browser restart)
- Create: `docs/LOCAL_SWISS_BROWSER_ROUTING_VERIFICATION_2026-10-13.md`
- Modify: `docs/APPLICATION_ENGINE.md`, `docs/PRODUCTION_DEPLOYMENT.md`, `docs/OPERATIONS.md` (only verified operator steps and unactivated capability status)

**Interfaces:** One isolated test database, local Greenhouse and Lever fixture pages, fixture-only capability catalog and fake or local-browser confirmation. No live board, employer, LLM credential or real submission.

- [ ] **Step 1: Add the synthetic full-flow test before marking the phase complete.** Drain discovery → match → exact documents → committed application → browser preparation → fixture-only queue → browser outcome for both ATS variants; inject an interruption after application creation and after reservation. Assert one application, one CV/letter pair, at most one attempt, no cross-user reads, and no second click after unknown.
- [ ] **Step 2: Build a fresh, separately named PostgreSQL/PostGIS Compose project/volume; verify its mount identity before migrations.** Migrate empty DB through `0032`, run the fixture workflow, inspect persisted task and application states and verify no real host request. Never mount, reset or delete the candidate's existing database volume.
- [ ] **Step 3: Run** the full Python suite, Ruff, mypy, Nuxt tests/typecheck/build, OpenAPI comparison, full Playwright suite, Docker browser fixture, Compose config, migration upgrade/downgrade and raw `git diff --check`. Record exact counts and every skipped/unavailable check.
- [ ] **Step 4: Write the verification report with changed files, `0026`–`0032` ancestry, tests, live source/form approval status, unresolved pause/click race, operator kill switch and unknown-attempt reconciliation.** Keep packaged capability catalog empty and live switch false. Stop this phase, request explicit validation, and do not claim local production readiness from fixtures.
