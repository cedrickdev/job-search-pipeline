# Local End-to-End Autopilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the Swiss V2 workflow usable end to end on a local machine: discover an opportunity, evaluate match and eligibility, generate evidence-grounded documents, apply the user's policy and prepare or queue a guarded browser submission.

**Architecture:** Reuse the existing `AssessmentService`, `ApplicationDecisionService`, `DocumentService`, `ApplicationService` and `ApplicationQueueService`; add one owner-scoped orchestration service rather than duplicating their rules. The orchestration returns a typed report for each opportunity and never calls the irreversible browser boundary unless the persisted policy, daily cap, rendered documents and explicit local live-submission setting all permit it.

**Tech Stack:** FastAPI, SQLAlchemy async repositories, existing V2 domain services, Nuxt 4/Vue 3/TypeScript, Vitest, pytest and Playwright fixtures.

**Spec:** `docs/superpowers/specs/2026-10-03-swiss-autopilot-design.md` and `docs/IMPLEMENTATION_PLAN.md`.

## Global Constraints

- Switzerland only in this increment; LinkedIn and jobs.ch remain unsupported.
- Tests use synthetic opportunities and never submit to an employer.
- CAPTCHA, MFA, ambiguous forms and unsupported channels become `REQUIRES_HUMAN`.
- LLM output is typed, evidence-grounded and validated before documents are stored.
- The server re-checks policy, ownership and limits at every irreversible boundary.
- No credentials, cookies or raw provider payloads are returned in API responses.
- Existing V1 routes and the current Swiss discovery catalog remain compatible.

## Review Focus

1. An opportunity with no profile, score or evidence must not reach application preparation.
2. A policy change or exhausted daily cap between preparation and queueing must block the browser task.
3. A generated document rejected by the evidence guard must never be pinned for submission.
4. A browser challenge or uncertain form must persist a human-required state without retrying.
5. A second user must not read or trigger another user's pipeline report or application.

### Task 1: Local autopilot domain report and service orchestration

**Files:**
- Create: `backend/app/domain/autopilot.py`
- Create: `backend/app/services/local_autopilot.py`
- Modify: `backend/app/api/dependencies.py`
- Test: `tests/test_v2_local_autopilot.py`

**Interfaces:**
- `LocalAutopilotService.run(user_id, opportunity_id, *, now, language=None) -> LocalAutopilotReport`
- The report contains the assessment, decision, document statuses and application state or a typed stop reason.

- [ ] Write tests for the safe sequential path and for each stop condition in the Review Focus.
- [ ] Run the focused test file and confirm collection fails because the service/report do not exist.
- [ ] Compose the existing services with injected repositories and call them in this order: assess, decide, create application when actionable, generate resume and cover letter, prepare application, then return without submission.
- [ ] Ensure missing evidence, ineligible results, low scores and human-required preparation terminate without queueing.
- [ ] Run focused tests and the existing assessment/document/application suites.
- [ ] Commit the vertical orchestration slice.

### Task 2: Owner-scoped API for local run and explicit queue boundary

**Files:**
- Create: `backend/app/api/routes/autopilot.py`
- Modify: `backend/app/api/router.py`, `backend/app/api/schemas.py`, `backend/app/api/dependencies.py`
- Test: `tests/test_v2_api_local_autopilot.py`, `tests/test_v2_api_surface.py`

**Interfaces:**
- `POST /api/v2/me/opportunities/{opportunity_id}/local-autopilot` runs the reversible pipeline and returns its typed report.
- `POST /api/v2/me/local-autopilot/{application_id}/queue` delegates only an already approved browser application to the existing queue service.

- [ ] Write owner-isolation, dry-run and challenge-state API tests first.
- [ ] Run them to observe the missing routes.
- [ ] Add typed response models with sanitized reason codes and no secrets/raw HTML.
- [ ] Wire both routes through the existing session, CSRF and service dependencies.
- [ ] Run API, application-engine and security tests.
- [ ] Regenerate OpenAPI and generated frontend types.
- [ ] Commit the API slice.

### Task 3: Local UI workflow

**Files:**
- Modify: `frontend/app/pages/map.vue`, `frontend/app/pages/applications.vue`
- Create or modify: `frontend/app/composables/useLocalAutopilot.ts`, `frontend/app/utils/autopilot-messages.ts`
- Test: `frontend/tests/nuxt/pages/map.spec.ts`, `frontend/tests/nuxt/pages/applications.spec.ts`, `frontend/tests/e2e/map.spec.ts`

- [ ] Add an explicit “Évaluer et préparer” action to a collected Swiss opportunity.
- [ ] Render score, eligibility, document status, application state and human-required reasons from the server report.
- [ ] Keep queueing as a separate explicit action; never auto-click it from a refresh or polling callback.
- [ ] Add French/English copy for safe, blocked and human-required states.
- [ ] Run frontend tests, typecheck, build and fixture-only browser tests.
- [ ] Commit the UI slice.

### Task 4: Local runtime verification and operator flow

**Files:**
- Modify: `docs/OPERATIONS.md`, `docs/APPLICATION_ENGINE.md`, `docs/MAP_EXPLORER.md`
- Create: `tests/test_v2_local_autopilot_integration.py`

- [ ] Add a fixture-only flow from a Swiss collected opportunity to a rendered document and a human-required browser state.
- [ ] Add a separate fixture-only approved queue test that asserts the browser adapter receives no employer request.
- [ ] Verify the local Docker stack, migrations, general worker and browser worker commands.
- [ ] Run the complete Python/frontend/browser verification suite and `git diff --check`.
- [ ] Document the exact local enablement flag, daily cap, challenge behavior and manual recovery path.
- [ ] Commit only this increment and report remaining source/deployment limitations.
