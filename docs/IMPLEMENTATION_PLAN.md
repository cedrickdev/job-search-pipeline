# V2 Gated Implementation Plan

## Execution protocol

This file is normative for Claude Code/Codex.

Only one phase may be implemented at a time.

At the end of each phase the coding agent must output:

```text
PHASE <N> COMPLETE

Implemented:
- ...

Changed files:
- ...

Migrations:
- ...

Tests/checks:
- ...

Known risks / follow-ups:
- ...

Please validate Phase <N>.
I will not start Phase <N+1> until you explicitly approve.
```

Then STOP.

---

## Phase 0 — Baseline and safety net

### Goal

Establish a reliable baseline before structural changes.

### Tasks

- document current V1 architecture;
- run existing Python tests;
- run frontend tests/build;
- inventory current database schema;
- inventory source and application adapters;
- inventory public FastAPI endpoints;
- document current LLM/chat behavior;
- add Ruff configuration;
- add mypy configuration for new V2 code without forcing a rewrite of all V1 modules;
- add CI if absent.

### Do not

- change domain behavior;
- migrate database;
- rename `Job` yet.

### Acceptance

- existing behavior still works;
- checks are reproducible;
- failing legacy checks are documented separately from new failures.

### Validation gate

STOP and ask user to validate Phase 0.

---

## Phase 1 — V2 domain foundation

### Goal

Introduce provider/source-neutral domain models without breaking V1.

### Add

- `OpportunityType`;
- `Opportunity`;
- `Company`;
- `CompanyLocation`;
- `CandidateProfile`;
- `SearchProfile`;
- `SearchArea`;
- `ApplicationPolicy`;
- typed scoring models;
- typed application decision models.

### Strategy

Create compatibility adapters between current `jobs` records and new `Opportunity` models.

### Acceptance

- V1 API remains functional;
- new models are fully typed and tested;
- no database migration yet unless strictly required.

### Validation gate

STOP.

---

## Phase 2 — Persistence V2: PostgreSQL + PostGIS foundation

### Goal

Create production-grade persistence while retaining a migration path from SQLite.

### Tasks

- Dockerfile for backend development/runtime;
- Docker Compose foundation;
- PostgreSQL + PostGIS container;
- SQLAlchemy 2;
- Alembic;
- PostgreSQL configuration;
- PostGIS extension/migrations;
- UUID ids;
- user-scoped core tables;
- repository abstractions;
- migration/import command from V1 SQLite for development data.

### Acceptance

- clean database can be created entirely by migrations;
- radius query has a test;
- migration/import is repeatable or safely idempotent;
- V1 data is not silently destroyed.

### Validation gate

STOP.

---

## Phase 3 — Frontend migration: React/Vite to Nuxt 4

### Goal

Move the V1 frontend to the official V2 stack before building major new product surfaces.

### Target

- Nuxt 4;
- Vue 3;
- TypeScript;
- Tailwind CSS;
- Nuxt UI;
- Pinia;
- VueUse;
- Zod where useful;
- Vitest + Vue Test Utils;
- Playwright E2E.

### Tasks

- create Nuxt application shell;
- reproduce existing routes/features before adding new product behavior;
- migrate Overview;
- migrate Jobs/Opportunities compatibility screen;
- migrate Analytics;
- migrate Settings;
- migrate Copilot/chat UI;
- migrate JobDrawer/Prep/Followups behavior;
- preserve FastAPI as backend;
- recreate API client/composables;
- migrate frontend tests;
- add Nuxt container to Docker Compose;
- remove React/Vite only after functional parity is validated.

### Do not

- redesign the entire product during migration;
- implement onboarding/map/interview V2 features early;
- move business logic into Nuxt/Nitro.

### Acceptance

- existing V1 frontend workflows have Nuxt equivalents;
- core frontend tests pass;
- frontend talks to the same FastAPI API;
- React frontend can be removed without losing supported behavior;
- Docker Compose can run Nuxt + FastAPI + PostgreSQL/PostGIS foundation.

### Validation gate

STOP.

---

## Phase 4 — Authentication, users and onboarding

### Goal

Make the product truly multi-user.

### Tasks

- User identity/authentication;
- CandidateProfile;
- SearchProfile;
- search country;
- radius/whole-country;
- opportunity types;
- schedule;
- languages;
- work authorization metadata;
- onboarding API;
- onboarding Nuxt flow.

### Acceptance

- two users cannot read one another's data;
- onboarding creates a usable SearchProfile;
- user can edit search preferences.

### Validation gate

STOP.

---

## Phase 5 — Country Packs and source plugin framework

### Goal

Remove Switzerland-specific logic from orchestration.

### Tasks

- Country Pack contract;
- CH pack first;
- SourceCapabilities;
- OpportunitySource protocol;
- wrap existing source modules;
- source registry by country/capability;
- health reporting.

### Acceptance

- existing Swiss discovery runs through registry;
- orchestrator has no hard-coded source import list;
- adding a new country does not require editing core discovery logic.

### Validation gate

STOP.

---

## Phase 6 — Company Discovery Engine

### Goal

Discover employers beyond job-board listings.

### Tasks

- Company canonicalization;
- careers-page records;
- ATS detection interface;
- company seed/discovery provider interfaces;
- link opportunities to companies;
- spontaneous-application capability field.

### Acceptance

- search can return relevant companies with or without an active opportunity;
- provenance is stored;
- duplicate companies are handled.

### Validation gate

STOP.

---

## Phase 7 — Geo Opportunity Explorer backend

### Goal

Implement geographic search semantics.

### Tasks

- geocoding abstraction;
- coordinates for company/opportunity locations;
- radius queries;
- whole-country filtering;
- geo API endpoints;
- clustering/aggregation endpoint if useful.

### Acceptance

- 100 km radius query is database-backed;
- remote opportunities follow explicit policy;
- missing coordinates degrade gracefully.

### Validation gate

STOP.

---

## Phase 8 — Interactive map frontend

### Goal

Create the signature map experience.

### Tasks

- map route;
- company and opportunity markers;
- clustering;
- filters;
- radius control;
- map/list synchronization;
- selected company/opportunity cards;
- statuses and match indicators.

### Acceptance

- large result sets remain usable;
- filter state is synchronized with API queries;
- map works on desktop and mobile.

### Validation gate

STOP.

---

## Phase 9 — Matching and Eligibility Engine V2

### Goal

Replace one-dimensional scoring.

### Scores

- skills;
- experience;
- education;
- language;
- location;
- schedule;
- work authorization;
- evidence confidence;
- overall.

### Acceptance

- hard eligibility failure is distinguishable from low fit;
- every score has explainable reasons;
- deterministic rules are used where possible;
- LLM is not sole authority for legal/eligibility decisions.

### Validation gate

STOP.

---

## Phase 10 — ATS Resume and Cover Letter V2

### Goal

Generalize the strong V1 CV pipeline.

### Tasks

- CandidateEvidence store;
- EvidenceGuard;
- opportunity analysis;
- resume artifact versions;
- ATS structural checks;
- cover-letter generator;
- prompt versioning;
- LLM provider-neutral generation.

### Preserve

Existing no-fabrication guarantees.

### Acceptance

- generated claims are traceable;
- invalid claims are rejected;
- artifacts are versioned;
- generation metadata includes provider/model/prompt version.

### Validation gate

STOP.

---

## Phase 11 — Provider-neutral LLM platform

### Goal

Complete the model abstraction.

### Providers

- Claude Code adapter;
- Codex adapter;
- OpenAI-compatible API adapter (`base_url + api_key + model`);
- Ollama/LM Studio compatibility;
- optional native adapters later.

### Tasks

- provider registry;
- encrypted connections;
- health checks;
- model selection;
- capability detection;
- explicit fallbacks;
- token/cost telemetry;
- generic provider sessions.

### Acceptance

- business logic imports no provider-specific SDK/CLI module;
- the same typed task can execute using at least two provider implementations;
- secrets are not returned after storage.

### Validation gate

STOP.

---

## Phase 12 — Autonomous Application Engine V2

### Goal

Route approved applications through supported channels.

### Tasks

- ApplicationStrategy;
- ApplicationPolicy;
- adapter registry;
- idempotency;
- audit trail;
- browser-run isolation;
- human-required state;
- application rate limits.

### Acceptance

- duplicate submission is prevented;
- unsupported flows fail safely;
- every irreversible submission is auditable.

### Validation gate

STOP.

---

## Phase 13 — Career Chat as control plane

### Goal

Make chat capable of explaining and proposing typed system actions.

### Tasks

- scoped context builder;
- provider-neutral streaming;
- typed action proposals;
- authorization and policy validation;
- search preference actions;
- application actions;
- interview actions.

### Acceptance

- prose cannot directly mutate state;
- all actions pass validation;
- conversations survive provider changes where practical.

### Validation gate

STOP.

---

## Phase 14 — Interview Simulator

### Goal

Move from static prep to adaptive practice.

### Tasks

- InterviewSession model;
- interview modes;
- adaptive question engine;
- voice/text responses;
- answer evaluation;
- follow-up questions;
- readiness score/history.

### Acceptance

- session state persists;
- follow-ups depend on candidate answer;
- feedback is tied to job/profile context.

### Validation gate

STOP.

---

## Phase 15 — Outcome tracking and Career Intelligence Loop

### Goal

Learn from real outcomes.

### Tasks

- normalized outcomes;
- funnel metrics;
- response/interview rates;
- timing analytics;
- role/source strategy analytics;
- recommendation generation;
- explicit approval for strategy-policy changes.

### Acceptance

- recommendations cite internal evidence/metrics;
- system never silently expands the user's application policy.

### Validation gate

STOP.

---

## Phase 16 — SaaS subscriptions and production hardening

### Goal

Prepare commercial deployment.

### Tasks

- plans/quotas;
- billing;
- usage metering;
- credential encryption;
- account export/delete;
- retention rules;
- production worker deployment;
- monitoring/alerts;
- backup/recovery;
- abuse protections.

### Validation gate

STOP.

---

## Phase 17 — Expansion by country

### Goal

Add countries incrementally using Country Packs.

Start with Switzerland as reference, then add one country at a time.

Each country requires:

- source review;
- terminology;
- opportunity types;
- geographic validation;
- eligibility metadata;
- adapter tests;
- documented limitations.

### Validation gate

Each country is its own gated delivery.
