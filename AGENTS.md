# Coding Agent Instructions — Job Search Pipeline V2

This file applies to Codex and other repository-aware coding agents.

Read all V2 documentation under `docs/` before modifying architecture.

## Gated implementation

Only implement the currently approved phase from `docs/IMPLEMENTATION_PLAN.md`.

Never continue into the next phase automatically.

At phase completion:

- run tests/checks;
- report implementation;
- report changed files;
- report migrations;
- report risks;
- explicitly request user validation;
- STOP.

## Core architecture constraints

- V2 central concept: `Opportunity`.
- Multi-user ownership must be explicit.
- PostgreSQL + PostGIS is the production target.
- Provider-neutral LLM layer is mandatory.
- Claude Code, Codex and generic `base_url + api_key + model` gateways are adapters.
- Playwright is a browser executor only.
- LLM state-changing output must be typed and validated.
- Candidate claims must remain evidence-grounded.
- Preserve V1 functionality during incremental migration.

## Safety and reliability

- no secret commits;
- no cross-user data leakage;
- no CAPTCHA/MFA bypass;
- no intentional anti-bot evasion;
- no uncontrolled mass submissions outside user policy;
- no real application submissions in automated tests.


## Official V2 frontend

The V2 frontend is **Nuxt 4 + Vue 3 + TypeScript**.

The current React + Vite application is V1 compatibility code and must be migrated during the dedicated frontend migration phase. Do not build major new V2 frontend surfaces in React.

Target frontend ecosystem:

- Nuxt 4
- Vue 3
- TypeScript
- Tailwind CSS
- Nuxt UI
- Pinia
- VueUse
- Zod where useful
- MapLibre GL JS
- ECharts
- Vitest + Vue Test Utils
- Playwright

FastAPI remains the business backend. Do not move core domain logic into Nuxt/Nitro.

## Docker

Docker/Docker Compose becomes mandatory from the PostgreSQL/PostGIS foundation phase onward.

The target composition evolves toward:

- Nuxt frontend
- FastAPI API
- PostgreSQL + PostGIS
- Redis
- background worker
- isolated Playwright browser worker
