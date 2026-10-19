# Swiss Morning Run matching and document preparation — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development task by task. Checkboxes track work; this document is **pending user review**, not permission to execute it.

**Goal:** After a successful or partial Swiss discovery, durably assess each observed opportunity, record the policy decision, and prepare at most one evidence-checked CV and letter per eligible run/opportunity, with safe restart and visible progress.

**Architecture:** Extend the existing owner-scoped PostgreSQL task queue, not the Nuxt request or the in-memory `LocalAutopilotService`. Discovery enqueues one assessment task per persisted run member; assessment records matching and eligibility and enqueues one document-preparation task only for a currently actionable decision. A dedicated owner-scoped workflow ledger records immutable stage outputs and document version IDs, so a crash between the CV and letter resumes without regenerating the CV. The general worker alone executes these reversible stages; this phase never creates or queues an application or browser-submission task.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, PostgreSQL/PostGIS, Nuxt 4/Vue 3/TypeScript, pytest, Ruff, mypy, Vitest, Playwright, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-12-swiss-ats-morning-autopilot-design.md` (downstream matching/document slice only).

## Global constraints

- Implement this phase only. Browser preparation, unattended submission, source activation and final acceptance are later gated phases under `docs/IMPLEMENTATION_PLAN.md`.
- CH only. LinkedIn, JobCloud and Indeed remain disabled; a public ATS listing does not grant submission permission.
- User identity comes from the task owner/session. Opportunity membership, assessment, decisions and document versions must be checked against that owner at every transition.
- Re-evaluate current candidate profile, opportunity and policy before decisions; do not turn missing evidence or legal uncertainty into a positive eligibility finding.
- An LLM draft has no authority to decide or send. Keep provider-neutral generation and the evidence guard; reject unsafe claims and unavailable providers visibly.
- No real employer requests, LLM calls with real credentials, or application submissions in automated tests. Keep the browser live-send switch off.
- Preserve V1 and all existing uncommitted changes. Revisions 0026–0030 are currently untracked: revision 0031 must never be committed or deployed without that complete reviewed ancestry.

## File map and interfaces

- `backend/app/domain/morning_preparation.py`: typed stage and terminal-reason vocabulary; no persistence imports.
- `backend/app/repositories/morning_preparation.py`: owner-scoped ledger, run-member pagination, and atomic compare-and-set checkpoints. The ledger key is `(user_id, discovery_run_id, opportunity_id)`, with a unique constraint; it stores assessment/decision identity, two exact rendered version IDs, stage, fixed error code and timestamps, never raw evidence or credentials.
- `backend/migrations/versions/rev_0031_morning_preparation.py` and `backend/app/infrastructure/database/models.py`: ledger table, user/run/opportunity foreign keys and indexes. Confirm actual migration identifiers and table names before writing SQL.
- `backend/app/services/morning_preparation.py`: stage orchestration over `AssessmentService`, `ApplicationDecisionService`, `DocumentService` and the ledger; no browser or application service dependency.
- `backend/app/tasks/handlers.py`, `backend/app/cli/run_worker.py`, `backend/app/services/discovery_catalog.py` or its worker boundary: enqueue and handle `MATCHING` and `DOCUMENT_GENERATION` on the general lane, with typed, owner-scoped payloads and deterministic keys. Use the existing `TaskDispatcher`; keep discovery persistence and enqueue atomic or provide a tested reconciliation pass after a crash.
- `backend/app/api/routes/morning_run.py`, `backend/app/api/schemas.py`, `backend/app/api/dependencies.py`: add read-only, session-scoped per-run preparation progress; never present a document as a submitted application.
- `frontend/app/composables/useMorningRun.ts`, `frontend/app/pages/settings.vue` and the relevant opportunity/document view: consume those backend states and link to exact rendered documents, with FR/EN loading, partial, review and failure messages.

## Review focus

1. The discovery run returns `PARTIAL` after one ATS source fails: persisted members from the healthy source are still assessed, while source failure remains visible.
2. A duplicate Morning tick, repeated discovery handler or two workers racing for the same opportunity produce one ledger row, one assessment transition and at most one CV/letter version for that run.
3. Crash after the CV is committed but before the letter: retry reuses that exact rendered CV and generates only the missing letter; a rejected/unavailable document never appears `DOCUMENTS_RENDERED`.
4. A second account cannot read the ledger or reuse another account's task, profile, decision or document version, even when the opportunity ID is shared.
5. A profile/policy changed after discovery is rechecked; `SKIP`, `SAVE` and `REQUIRE_REVIEW` never produce an automatic submission or a misleading “ready to send” state.

---

### Task 1: Durable owner-scoped checkpoint and migration

**Files:** Create `backend/app/domain/morning_preparation.py`, `backend/app/repositories/morning_preparation.py`, `backend/migrations/versions/rev_0031_morning_preparation.py`; modify `backend/app/infrastructure/database/models.py` and its mapper if needed; test `tests/test_v2_morning_preparation_persistence.py`, `tests/test_v2_persistence_migrations.py`.

**Interfaces:** `get(user_id, run_id, opportunity_id) -> Preparation | None`; `start_or_get(...) -> Preparation`; `record_assessment(...)`; `record_document_version(..., document_type, version_id)`; `finish(...)`; each mutation is compare-and-set in one database transaction and refuses an owner or stage mismatch.

- [ ] Write failing domain/persistence tests for two users, duplicate start, concurrent stage transition, stable exact version IDs, invalid stage regression and upgrade/downgrade.
- [ ] Run the targeted tests and verify the failures are due to absent behavior.
- [ ] Add the minimal table, repository and domain state machine. A unique `(user_id, discovery_run_id, opportunity_id)` constraint and transaction-level locking enforce idempotency beyond process-local checks. No candidate text is placed in the ledger.
- [ ] Re-run targeted tests, Ruff and mypy. Inspect migration ancestry 0026–0030; do not stage or commit an orphan 0031.

### Task 2: Discovery-to-assessment handoff

**Files:** Modify `backend/app/tasks/handlers.py`, `backend/app/cli/run_worker.py`, and only the necessary discovery-run repository/service boundary; create `backend/app/services/morning_preparation.py`; test `tests/test_v2_morning_preparation_service.py`, `tests/test_v2_tasks_handlers.py`, `tests/test_v2_discovery_catalog_integration.py`.

**Interfaces:** `MATCHING` payload is `{discovery_run_id, opportunity_id}`; user is always `TaskRun.user_id`. Key is `morning-match:<owner>:<run>:<opportunity>`. The handler verifies that the opportunity belongs to that owner's completed/partial run, evaluates both axes, records a typed decision, and checkpoints it before enqueueing the next stage.

- [ ] Write failing tests for one CH member, a partial two-source run, zero members, duplicate handoff, forged foreign run/member and a disabled search. A run's matching task must never be enqueued before its member is persisted.
- [ ] Run red. Implement bounded member pagination and idempotent task enqueue in the same transaction as the stage transition, or implement explicit recovery of persisted members not yet enqueued; test a crash at that boundary.
- [ ] Write failing tests for `ELIGIBLE`, `INELIGIBLE`, `INCOMPLETE`, `REVIEW_REQUIRED`, low match, absent match and inactive/manual policy. Reuse `AssessmentService.evaluate` and `ApplicationDecisionService.decide`; only `PREPARE`/`AUTO_APPLY` intent may queue documents, and neither is authority to submit.
- [ ] Run green, then targeted regression, Ruff and mypy. Confirm no `APPLICATION_SUBMISSION` task is created.

### Task 3: Exactly-once-per-run document preparation

**Files:** Modify `backend/app/tasks/handlers.py`, `backend/app/cli/run_worker.py` and the narrow `DocumentService` interface only if needed; test `tests/test_v2_morning_preparation_documents.py`, `tests/test_v2_documents.py`, `tests/test_v2_worker_cli.py`.

**Interfaces:** `DOCUMENT_GENERATION` payload has the same owner/run/opportunity identity. Key is `morning-docs:<owner>:<run>:<opportunity>`. The handler obtains a ledger lock, rechecks the current decision and evidence, creates or reuses a single rendered CV checkpoint, then a single rendered letter checkpoint. `DOCUMENTS_RENDERED` requires both exact versions to exist, belong to the owner/pair and pass the guard.

- [ ] Write failing tests for CV-success/letter-failure/retry, duplicate worker delivery, rejected guard, insufficient evidence, provider outage, quota refusal, and changed candidate profile or policy. Assert persisted version counts and artifact IDs rather than mock call counts.
- [ ] Run red. Implement safe per-document commit/checkpoint boundaries. If a DB transaction rolls back after writing artifact bytes, retry may reuse the deterministic storage key; orphan cleanup is reported separately. Never call ordinary `DocumentService.generate` again for a checkpoint already rendered.
- [ ] Run green and the document, worker and quota regressions. A failed/rejected draft is shown as review/failure, not as a prepared PDF.

### Task 4: Truthful progress in API and FR/EN UI

**Files:** Modify `backend/app/api/routes/morning_run.py`, `backend/app/api/schemas.py`, `backend/app/api/dependencies.py`, `frontend/openapi.json`, `frontend/app/types/api.d.ts`, `frontend/app/composables/useMorningRun.ts`, `frontend/app/pages/settings.vue` and the relevant document/opportunity component; test `tests/test_v2_morning_run_api.py`, `frontend/tests/nuxt/composables/useMorningRun.spec.ts`, `frontend/tests/e2e/settings-morning-run.spec.ts`.

**Interfaces:** A read-only status for the current account returns per-run members with stage, match/eligibility status, fixed reason code and exact CV/letter version links. Queue acknowledgement stays `QUEUED`; document readiness stays distinct from `APPLICATION_PREPARED` or `CONFIRMED`.

- [ ] Write failing API and UI tests for queued, matched, skipped, review, one-document-only, rendered-both, provider failure and foreign-owner 404. Verify both locales, keyboard status announcement and 320/390/768/desktop layouts.
- [ ] Run red. Add only the necessary typed API response and Nuxt rendering; no decision logic or inferred success status in Nuxt.
- [ ] Run green, regenerate/check the OpenAPI contract, Nuxt typecheck/build and targeted Playwright fixtures.

### Task 5: Isolated rehearsal and phase handoff

**Files:** Create `docs/LOCAL_SWISS_MORNING_PREPARATION_VERIFICATION_2026-10-13.md`; update `docs/OPERATIONS.md` only if rehearsal exposes an inaccurate step.

**Interfaces:** Evidence report includes exact test counts, migration head, changed files, two-user/retry outcomes, source-approval state and remaining gates. It must not claim local production readiness or a successful application.

- [ ] Run full Python tests, Ruff, mypy, full Nuxt tests, typecheck/build, OpenAPI comparison, Playwright fixture suite, `docker compose config --quiet`, and raw `git diff --check`; report any check not run.
- [ ] Use a separately verified fresh Compose volume prefix and synthetic users. Migrate from empty PostgreSQL/PostGIS through 0031, process a fixture Greenhouse/Lever partial discovery, restart the worker after the CV checkpoint, and verify one CV plus one letter, exact owner-scoped status, and zero browser/application-submission tasks. Do not touch the candidate's existing database.
- [ ] Record that live Swiss ATS boards are still unapproved unless operator review and a bounded non-submitting live check have actually occurred. No real employer contact in this phase.
- [ ] Summarize implementation, changed files, migration ancestry, tests and risks; request explicit phase validation and **stop** before browser preparation or submission work.
