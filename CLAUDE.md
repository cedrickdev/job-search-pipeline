# Claude Code Instructions — Job Search Pipeline V2

Read these files before implementing V2 work:

1. `docs/README.md`
2. `docs/V2_SPECIFICATION.md`
3. `docs/ARCHITECTURE.md`
4. `docs/LLM_PROVIDER_ARCHITECTURE.md`
5. `docs/ENGINEERING_STANDARDS.md`
6. `docs/IMPLEMENTATION_PLAN.md`

## Mandatory phase gate

You MUST implement only one roadmap phase at a time.

Before coding:

1. identify the current approved phase;
2. inspect existing code related to that phase;
3. present a concise implementation plan;
4. state compatibility/migration risks.

After implementation:

1. run required tests/checks;
2. summarize changes;
3. list changed files;
4. list migrations;
5. list known risks;
6. ask the user to validate the phase;
7. STOP.

Do not start the next phase until the user explicitly says to continue/approve.

## Preserve V1

The repository already contains working discovery, application adapters, CV truth validation, chat, interview prep and frontend tests.

Prefer adapters and compatibility layers over destructive rewrites.

## Architecture rules

- `Opportunity`, not `Job`, is the V2 domain abstraction.
- V2 is multi-user.
- PostgreSQL/PostGIS is the production persistence target.
- LLM access is provider-neutral.
- Playwright is an application/browser adapter, not the decision engine.
- LLM output proposes typed actions; application services validate and execute them.
- Never fabricate candidate facts.
- Never bypass CAPTCHA/MFA or intentionally evade platform protections.
- User-owned data must be authorization-scoped.

## LLM integrations

The final architecture must support:

- Claude Code;
- Codex;
- generic OpenAI-compatible API using `base_url`, `api_key`, and `model`;
- local OpenAI-compatible servers.

Do not spread CLI flags or provider SDK calls across the codebase. Keep them inside provider adapters.

## Testing

Do not rely on live job boards or live LLMs for default tests.
Use fixtures/mocks and adapter contract tests.


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
