# Frontend Architecture — Nuxt 4

## Decision

Job Search Pipeline V2 standardizes on Nuxt 4, Vue 3 and TypeScript.

The current React + Vite frontend is migrated rather than extended with major V2 features.

## Stack

- Nuxt 4
- Vue 3
- TypeScript
- Tailwind CSS
- Nuxt UI
- Pinia
- VueUse
- Zod
- MapLibre GL JS
- ECharts
- Vitest
- Vue Test Utils
- Playwright

## Boundary

FastAPI remains the business backend.

Nuxt handles presentation, navigation, UI state and user interaction.

Do not duplicate domain logic in Nitro.

## Data access

Prefer composables around FastAPI:

- `useOpportunities`
- `useCompanies`
- `useApplications`
- `useProfile`
- `useCareerChat`
- `useInterview`
- `useMapSearch`

Use `useFetch`, `useAsyncData` and `$fetch`.

## State

Pinia is for client/global state that benefits from explicit stores:

- authentication/session presentation state;
- onboarding wizard;
- map/filter state;
- currently selected opportunity;
- user UI preferences.

Backend entities remain server state.

## Map

Use MapLibre GL JS behind a Nuxt component/composable boundary.

The backend owns geographic filtering via PostGIS.

## Realtime

Use SSE first for LLM/chat/run progress because the V1 already follows this pattern.

Use WebSocket only for workflows that truly require bidirectional low-latency communication.

## Migration strategy

Migrate for parity first, redesign second.

Required order:

1. Nuxt shell/layout/navigation;
2. API client/composables;
3. Overview;
4. jobs/opportunities compatibility page;
5. analytics;
6. settings;
7. chat/copilot;
8. job detail/prep/followups;
9. frontend test parity;
10. remove V1 React frontend only after validation.

Do not mix this migration with onboarding or map feature development.
