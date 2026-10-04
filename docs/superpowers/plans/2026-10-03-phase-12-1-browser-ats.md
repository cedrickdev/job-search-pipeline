# Phase 12.1 Browser ATS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish and verify the existing V2 browser ATS path for an explicitly approved Swiss application, without enabling unattended live submissions.

**Architecture:** The API persists an owner-scoped browser-lane task; the browser worker re-checks authority through `ApplicationService`, executes a supported ATS driver in an account-isolated Playwright profile, and persists a typed outcome. A committed in-flight attempt must precede any possible click, and an uncertain outcome must never be retried automatically. Unknown sites and challenges are human hand-offs.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy 2/PostgreSQL, Python Playwright/Chromium, Nuxt 4/Vue 3, Docker Compose, pytest, Vitest.

**Spec:** `docs/superpowers/specs/2026-10-03-swiss-autopilot-design.md`

## Global Constraints

- Only Phase 12.1 is in scope. Discovery persistence, 85/100 autopilot policy UI, per-offer LLM documents, native model connections and FR/EN UI are later gated deliverables; this phase must not imply they are working.
- Initial country is `CH`; adding selectable countries requires separately validated Country Packs.
- No automated test may send a real application. Tests use local ATS fixtures and fake users/documents; a live send requires a later explicit activation gate.
- `JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED` defaults to `false` in every deployment. Fixture tests may enable an injected equivalent only while production ATS hosts are unreachable from that test.
- CAPTCHA, MFA, login walls, unknown required answers, sensitive fields and changed forms block only the affected application. No bypass, evasion, fabricated claim or blind retry.
- Preserve V1 and all pre-existing working-tree changes. The current branch already contains uncommitted browser-worker and fixture work; review it before editing, and stage only files changed by each task.
- The browser adapter stays `SUPPORTED_WITH_REVIEW` in this phase. A driver passing fixture tests does not become blanket `FULLY_SUPPORTED` authority.
- Secrets, raw page text, form values, cookies and candidate documents never enter task payloads, process arguments or logs.

## Review Focus

1. Two users on the same worker host must never share a browser profile or authenticated ATS session (Task 1 test).
2. A task with a forged target URL, another account's application id, or a profile different from the application's pinned profile must fail before page navigation (Task 2 test).
3. Missing form fingerprint, unknown required upload or a `COMPLETED` result without positive confirmation must never produce `SUBMITTED` (Task 2 test).
4. A process death after the irreversible click but before result persistence must leave a durable `SUBMISSION_STATE_UNKNOWN` trail, never an approved retry (Task 3 test).
5. Browser integration tests must run in a browser-capable CI/Docker environment rather than silently ship with all real-browser cases skipped (Task 4 test).

---

### Task 1: Account-isolated Chromium state

**Files:**
- Modify: `backend/app/application_engine/browser/runtime.py`
- Modify: `backend/app/application_engine/browser_worker.py`
- Modify: `backend/app/application_engine/task_dispatcher.py` only if the lock needs a per-account path while retaining V1 serialization
- Modify: `docker-compose.yml` and `docker/browser-worker.Dockerfile` only for private volume ownership
- Test: `tests/test_v2_browser_automation.py`

**Interfaces:**
- Consumes: `BrowserTask.user_id`, `browser_page(profile_dir=...)`, `browser_lock(lock_path=...)`.
- Produces: `profile_dir_for(user_id: UserId) -> Path`; no other user's profile is opened by `_drive(task)`.

- [ ] **Step 1: Write failing isolation tests.** Assert distinct, stable, private paths for two `UserId` values and assert `_drive` passes the task owner's path to `browser_page`. In a real-browser fixture, set a cookie in one account's profile, reopen it, then verify the second account sees no cookie.

```python
from uuid import UUID

from backend.app.domain.identifiers import UserId

def test_profile_paths_are_owner_scoped(tmp_path):
    first = profile_dir_for(UserId(UUID("11111111-1111-4111-8111-111111111111")), root=tmp_path)
    second = profile_dir_for(UserId(UUID("22222222-2222-4222-8222-222222222222")), root=tmp_path)
    assert first != second
    assert first.parent == second.parent == tmp_path
    assert first.stat().st_mode & 0o077 == 0
```

- [ ] **Step 2: Run the focused test and see it fail because the helper and isolation do not exist.** Run: `.venv/bin/python -m pytest tests/test_v2_browser_automation.py -k profile_paths -q`.
- [ ] **Step 3: Implement a UUID-derived, private profile directory.** Keep the shared V1 `browser_lock` if needed for compatibility, but never reuse V1's profile. The worker passes `profile_dir_for(task.user_id)` into `browser_page`; the browser worker container has write access only to its required state/artifact volumes.

```python
def profile_dir_for(user_id: UserId, *, root: Path = BROWSER_PROFILE_DIR) -> Path:
    directory = root / str(user_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory
```

- [ ] **Step 4: Run the isolation tests, then the existing task-dispatcher tests.** Run: `.venv/bin/python -m pytest tests/test_v2_browser_automation.py -k profile -q` and `.venv/bin/python -m pytest tests/test_v2_application_engine.py -q`. No real ATS URL is visited.
- [ ] **Step 5: Commit only this task's reviewed files.** Use a targeted `git add` and inspect `git diff --cached --name-only` before `git commit -m "fix: isolate browser profiles by account"`.

### Task 2: Authoritative target and fail-closed form result

**Files:**
- Modify: `backend/app/application_engine/browser/context_loader.py`
- Modify: `backend/app/application_engine/browser/execute.py`
- Modify: `backend/app/application_engine/browser/execution.py`
- Modify: `backend/app/application_engine/browser_worker.py`
- Modify: `backend/app/application_engine/adapters/browser.py`
- Modify: `backend/app/application_engine/task_dispatcher.py`
- Test: `tests/test_v2_browser_automation.py`, `tests/test_v2_application_engine.py`

**Interfaces:**
- Consumes: `ApplicationContextLoader.load(user_id, application_id, target_url, correlation_id)`, `OpportunityRepository.get(opportunity_id)`, `DriverContext.expected_form_fingerprint`.
- Produces: a context only when the persisted opportunity's application URL and the task target agree, using `app.candidate_profile_id` rather than the user's default profile; `COMPLETED` submit is accepted only with `submission_confirmed=True`.

- [ ] **Step 1: Write tests for forged/missing targets, wrong profile, missing fingerprint, unrecognized required upload, and false completion.** A cross-owner id and a task URL differing from the persisted opportunity URL must return a typed pre-navigation refusal. The loader must fetch `app.candidate_profile_id`, not `get_default(user_id)`, and refuse a missing or foreign profile. A submit with no prepared fingerprint must not call `driver.submit`. A `BrowserTaskResult(COMPLETED, submission_confirmed=False)` must never map to `SubmissionOutcome.SUBMITTED`.

```python
def test_unconfirmed_completion_is_not_submitted():
    result = BrowserTaskResult(outcome=BrowserTaskOutcome.COMPLETED)
    mapped = _to_submission_result(result)
    assert mapped.outcome is not SubmissionOutcome.SUBMITTED
```

- [ ] **Step 2: Run the focused tests to confirm the current unsafe cases fail.** Run: `.venv/bin/python -m pytest tests/test_v2_application_engine.py tests/test_v2_browser_automation.py -k 'unconfirmed or target or fingerprint or upload' -q`.
- [ ] **Step 3: Bind navigation to persisted opportunity and profile data and refuse uncertainty.** Inject `SqlAlchemyOpportunityRepository(session)` into the worker loader, compare normalized scheme/host/path/query with the task URL before opening a page, fetch `profiles.get(user_id, app.candidate_profile_id)`, and require a non-empty prepared fingerprint for SUBMIT. Treat an unknown required file slot as `UPLOAD_UNRESOLVED` and an unconfirmed `COMPLETED` as `STATE_UNKNOWN`, never success.

```python
if task.kind is BrowserTaskKind.SUBMIT and not loaded.context.expected_form_fingerprint:
    return BrowserTaskResult(
        outcome=BrowserTaskOutcome.REQUIRES_HUMAN,
        human_required_reason=HumanRequiredReason.AMBIGUOUS_FORM,
        detail="the application form has not been safely prepared")
```

- [ ] **Step 4: Run all browser and application-engine unit tests.** Run: `.venv/bin/python -m pytest tests/test_v2_application_engine.py tests/test_v2_browser_automation.py -q`. Fixture submissions only; inspect the confirmation counter for exactly one click and zero clicks on refusals.
- [ ] **Step 5: Commit only this task's reviewed files.** Inspect the staged diff, then `git commit -m "fix: reject uncertain browser submissions"`.

### Task 3: Durable irreversible boundary

**Files:**
- Modify: `backend/app/services/applications.py`
- Modify: `backend/app/tasks/handlers.py`
- Modify: `backend/app/cli/run_worker.py`
- Create: `backend/app/application_engine/submission_reservation.py` for the immutable reservation identity
- Modify: `backend/app/application_engine/browser_worker.py` if the worker needs a reservation token
- Modify: `backend/app/domain/application.py` only if a typed reservation identity is required
- Test: `tests/test_v2_application_engine.py`, `tests/test_v2_tasks_handlers.py`, `tests/test_v2_persistence_application_engine.py`
- Migration: add one only if the current `submission_attempts` and `applications` schema cannot represent the committed reservation; never change an applied migration in place.

**Interfaces:**
- Consumes: `ApplicationService.submit(...)`, the existing `lock_submission_budget`, `SubmissionAttempt`, `ApplicationState.SUBMITTING`, and the browser-lane handler's `TaskRun`.
- Produces: `reserve_submission(user_id, app_id, now) -> SubmissionReservation`, `execute_reserved(reservation) -> SubmissionResult`, and `finalize_submission(reservation, result, now) -> Application`; the worker owns three separate units of work.

- [ ] **Step 1: Add a crash-boundary regression test.** Reuse `_an_application`, `_seed_decision` and the repository fixtures in `tests/test_v2_persistence_application_engine.py`. Commit an `APPROVED` browser application, call `reserve_submission` in a fresh `session_scope`, and open another session to prove both its `SUBMITTING` state and in-flight attempt are visible **before** invoking a fake browser dispatcher. Simulate a crash without finalization, call `recover_in_flight`, and assert `SUBMISSION_STATE_UNKNOWN`; a redelivered task must not click. Add two concurrent reservations for the same account at its last permitted slot; exactly one may reserve.

```python
async with session_scope(factory) as session:
    reservation = await application_service(session).reserve_submission(
        USER, approved.id, now=NOW)
async with session_scope(factory, commit=False) as session:
    assert (await SqlAlchemyApplicationRepository(session).get(USER, approved.id)).state \
        is ApplicationState.SUBMITTING
    assert await SqlAlchemySubmissionAttemptRepository(session).list_for_application(
        USER, approved.id)
# Terminate here: no finalization. A fresh recovery pass must mark the committed
# in-flight application unknown, and a redelivered task must not invoke the browser.
```

- [ ] **Step 2: Run the regression test; confirm the current single-transaction submit fails the durability assertion.** Run: `.venv/bin/python -m pytest tests/test_v2_persistence_application_engine.py -k crash_after_click -q`.
- [ ] **Step 3: Split the submit transaction at the irreversible boundary.** The first transaction locks the owner budget, re-evaluates current policy/eligibility/approval/quota and pins, writes `SUBMITTING` plus the in-flight attempt, then commits. Only afterward may `execute_reserved` dispatch Playwright. A final transaction matches the exact attempt identity and records `SUBMITTED`, `REQUIRES_HUMAN`, `FAILED` or `STATE_UNKNOWN`; a crash leaves a committed in-flight row for `recover_in_flight` to mark unknown. A stale task cannot reserve again, and neither recovery nor queue retries may perform a second click.

```python
async with session_scope(factory) as session:
    reservation = await application_service(session).reserve_submission(
        user_id, application_id, now=clock())
# The reservation and attempt are committed before any browser call.
result = await browser_executor.execute_reserved(reservation)
async with session_scope(factory) as session:
    await application_service(session).finalize_submission(
        reservation, result, now=clock())
```

`SubmissionReservation` carries only `user_id`, `application_id` and `attempt_id`; `execute_reserved` loads candidate data from owner-scoped repositories. On redelivery, an application already `SUBMITTING` or terminal is not reserved again. Do not retain an uncommitted database session over the browser subprocess. Before the click, the worker reads the current policy and deployment kill switch again; if disabled, it returns a pre-click human/block result, and the finalizer records it. The later autopilot phase must define a stronger atomic pause-versus-click protocol before live unattended activation.

- [ ] **Step 4: Run service, persistence and worker-handler suites, including concurrency.** Run: `.venv/bin/python -m pytest tests/test_v2_application_engine.py tests/test_v2_persistence_application_engine.py tests/test_v2_tasks_handlers.py -q`. The result of every uncertain post-click path is terminal unknown, not retryable failure.
- [ ] **Step 5: Commit only the reviewed durability change and migration, if any.** Use `git diff --cached --check`, inspect staged names, then `git commit -m "fix: persist submission attempt before browser click"`.

### Task 4: Safe queue ingress and executable fixture path

**Files:**
- Modify: `backend/app/api/routes/applications.py`
- Modify: `backend/app/api/dependencies.py`
- Modify: `backend/app/api/schemas.py` if a queued response needs a typed task id
- Create: `backend/app/services/application_queue.py`
- Modify: `backend/app/tasks/handlers.py`
- Modify: `docker/browser-worker.Dockerfile`, `docker-compose.yml`
- Modify: `docs/APPLICATION_ENGINE.md`, `docs/PRODUCTION_DEPLOYMENT.md`
- Test: `tests/test_v2_api_applications.py`, `tests/test_v2_worker_cli.py`, `tests/test_v2_browser_automation.py`

**Interfaces:**
- Consumes: `PersistedTaskDispatcher.enqueue(TaskSpec)`, `TaskKind.APPLICATION_SUBMISSION`, `TaskLane.BROWSER`, `build_browser_handlers(...)`.
- Produces: `ApplicationQueueService.enqueue_approved(user_id: UserId, app_id: ApplicationId) -> TaskRunId` and `QueuedApplicationResponse(task_id: TaskRunId)`; one owner-scoped, idempotent queue operation for an approved application, disabled for live hosts until an explicit deployment feature flag and user policy permit it. Status remains readable through existing application/events endpoints.

- [ ] **Step 1: Add API and worker tests.** A user can queue only their own `APPROVED` application; duplicate queue requests return the same task id; anonymous/cross-owner requests fail; a paused policy or disabled live flag cannot create a live-send task. The fixture end-to-end test exercises API enqueue → browser-lane handler → fake/fixture executor → one confirmed or unknown outcome, never a live board.

```python
async def test_duplicate_queue_is_one_task(client, owner_session, approved_application):
    first = await client.post(
        f"/api/v2/applications/{approved_application.id}/queue",
        headers=owner_session.csrf_headers)
    second = await client.post(
        f"/api/v2/applications/{approved_application.id}/queue",
        headers=owner_session.csrf_headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
```

- [ ] **Step 2: Run the focused API test to observe the missing queue endpoint.** Run: `.venv/bin/python -m pytest tests/test_v2_api_applications.py -k duplicate_queue -q`.
- [ ] **Step 3: Implement queue ingress and browser-capable verification.** The route authenticates the owner, checks the current application state and deployment live-send flag, writes one `TaskSpec` in the request transaction, and returns `202` plus task id. The worker re-checks policy at execution time. Build a test image with the Python Playwright version-matched Chromium and system dependencies (`playwright install --with-deps chromium` in Docker, using official Playwright guidance); run fixture tests there with an environment switch that fails instead of skipping when Chromium is unavailable. Inject a fixture-only driver registry into the integration test so local HTTP fixtures are accepted without weakening production ATS hostname rules. Do not add browser packages to the API image.

```python
class BrowserSubmissionDisabled(Exception):
    """The deployment has not enabled live browser submission."""


class ApplicationQueueService:
    def __init__(self, applications: ApplicationRepository,
                 dispatcher: TaskDispatcher, *, live_enabled: bool) -> None:
        self._applications = applications
        self._dispatcher = dispatcher
        self._live_enabled = live_enabled

    async def enqueue_approved(self, user_id: UserId,
                               app_id: ApplicationId) -> TaskRunId:
        app = await self._applications.get(user_id, app_id)
        if app is None:
            raise ApplicationNotFound(app_id)
        if app.state is not ApplicationState.APPROVED:
            raise ApplicationNotActionable(app.state, "queue")
        if not self._live_enabled:
            raise BrowserSubmissionDisabled()
        return await self._dispatcher.enqueue(TaskSpec(
            kind=TaskKind.APPLICATION_SUBMISSION,
            idempotency_key=f"application-submission:{app.id}",
            user_id=user_id, payload={"application_id": str(app.id)},
            max_attempts=1))

@router.post("/applications/{application_id}/queue", status_code=202)
async def queue_application(application_id: ApplicationId, current: CurrentSession,
                            dispatcher: BrowserLaneTasks) -> QueuedApplicationResponse:
    task_id = await dispatcher.enqueue_approved(current.user.id, application_id)
    return QueuedApplicationResponse(task_id=task_id)
```

Define `BrowserLaneTasks = Annotated[ApplicationQueueService, Depends(application_queue_service)]` in `api/dependencies.py`, and add `QueuedApplicationResponse` to `api/schemas.py`. Map `BrowserSubmissionDisabled` to a typed HTTP 409 `application_browser_disabled` without echoing internals. Read `JOBSEARCH_BROWSER_LIVE_SUBMISSIONS_ENABLED` through typed settings, defaulting to `false` in `.env.example` and Docker Compose. The test-only fixture path injects `live_enabled=True` while routing exclusively to local fixture hosts; production remains false until a separate activation review.

- [ ] **Step 4: Verify the complete local fixture path and container configuration.** Run backend unit/persistence/API tests, `docker compose config`, a browser-test container with Chromium present, Ruff and mypy for changed modules, then frontend tests/build if the API contract or UI changed. Fail the phase if browser tests are all skipped or if a real URL is contacted.
- [ ] **Step 5: Update the support matrix and hand-off.** Document which ATS fixtures passed, which real-site behaviors remain unverified, the live-send feature flag, rollback/kill switch and any migration. Commit only task files after staged-diff review.

## Phase 12.1 completion gate

Report implemented behavior, changed files, migrations, checks (including skipped tests), and residual risk. The release remains local/dry-run by default; do not call it production-ready or activate unattended live submissions. Ask the user to validate Phase 12.1, then stop. Separate, approved plans must connect Swiss discovery persistence, policy-driven 85/10 autopilot, LLM documents, first-class OpenAI/Anthropic connections, bilingual UI, production hardening and later country packs.
