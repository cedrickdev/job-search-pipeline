# Frontend Architecture — Nuxt 4

## Decision

Job Search Pipeline V2 standardizes on Nuxt 4, Vue 3 and TypeScript.

The Nuxt application in `frontend/` is the active frontend. Phase 3 migrated the React + Vite application to it for parity and then removed `webapp/`; the React tree survives only in git history, which is what the "ported from `webapp/src/...`" comments in `frontend/app/` refer to.

The app is `ssr: false`. `nuxt generate` prerenders it into `frontend/.output/public`, and FastAPI serves that directory — one origin, one port, no Node process in production. `pipeline.paths.FRONTEND_DIST` is the constant, and `server/app.py::_mount_spa` is the mount.

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

As built in Phase 3, the V1 surfaces share four generic composables rather than one per entity — `useApiQuery`, `useMutation`, `useQueries`, `useMutations` — because the V1 API is endpoint-shaped, not resource-shaped. The list above is the target for the V2 domain surfaces, which arrive with the phases that introduce them.

## State

Pinia is for client/global state that benefits from explicit stores:

- authentication/session presentation state;
- onboarding wizard;
- map/filter state;
- currently selected opportunity;
- user UI preferences.

Backend entities remain server state.

As built in Phase 4, `stores/session.ts` is the single store that holds backend data, and the file argues the exception rather than assuming it: a route middleware runs before any component exists, so it cannot call `useAsyncData`, and something outside the component tree has to answer "is there a session?" before a page is allowed to render. It holds the account and the session window and nothing else — the candidate profile, the saved searches and the onboarding counts are server state with pages of their own and stay in `useApiQuery`'s cache (`composables/useAccount.ts`). `status` has three values (`unknown`, `authenticated`, `anonymous`), because "we have not asked yet" is not "there is nobody", and collapsing them is what makes a login form flash on screen for a signed-in user.

## Map

Use MapLibre GL JS behind a Nuxt component/composable boundary.

The backend owns geographic filtering via PostGIS.

## Realtime

Use SSE first for LLM/chat/run progress because the V1 already follows this pattern.

Use WebSocket only for workflows that truly require bidirectional low-latency communication.

## Migration (completed in Phase 3)

Parity first, redesign second. The order followed was: shell/layout/navigation; API client and composables; Overview; jobs; analytics; settings; chat/copilot; job detail/prep/followups; test parity; and only then the removal of the React frontend, after the user validated parity.

What that produced, and what a later phase must not silently regress:

- 4 pages (`index`, `jobs`, `analytics`, `settings`) and 21 components under `frontend/app/`;
- 111 Vitest + Vue Test Utils specs in `frontend/tests/nuxt/`, covering every surface the 75 React tests covered;
- 14 Playwright flows in `frontend/tests/e2e/`, with every `/api/**` request stubbed in the browser and each test asserting nothing went unstubbed;
- a checked contract: FastAPI → `frontend/openapi.json` → `frontend/app/types/api.d.ts`, regenerated and diffed by the `api-contract` CI job.

Redesign work, onboarding surfaces and the map are separate phases.

## Accounts (added in Phase 4)

Four pages joined the four V1 ones — `login`, `register` (both `layout: false`, since a sign-in form has no shell to sit in), `onboarding` and `profile` — plus `ProfileForm` and `SearchProfileForm`, `composables/useAccount.ts`, `stores/session.ts`, `middleware/auth.ts`, `types/v2.ts` and `utils/v2-errors.ts`.

Four rules in that work are contracts rather than preferences, and [V2 Authentication](./AUTHENTICATION.md) §Frontend contract is the full statement of them:

- **The guard is named, not global.** A page opts in with `definePageMeta({ middleware: 'auth' })`, and only `/profile` and `/onboarding` do. The V1-ported screens stay unguarded because V1's `/api/**` has no accounts, so a guard there would demand a login the data behind it cannot use. The guard chooses a screen; the API is what refuses a request.
- **`X-CSRF-Token` on every unsafe request.** `utils/api-client.ts` reads the CSRF cookie per request — never a cached copy, because another tab can log out — and never touches the session cookie, which is `HttpOnly` by design.
- **No credential is stored client-side.** Not in Pinia, not in `localStorage`, not in a component. There is no field for one.
- **A refusal becomes one sentence.** `utils/v2-errors.ts` maps the `error` slug to a human sentence beside the form, and nothing redirects on a 401 from a write — a redirect out of a mutation would discard what the user typed.

The suite grew with the surfaces: **265** Vitest specs in `frontend/tests/nuxt/` and **21** Playwright flows in `frontend/tests/e2e/`, still with every `/api/**` request stubbed in the browser and every test asserting that nothing went unstubbed. Two of those Playwright tests exist because the assertion is only possible in a real browser: that the CSRF header actually leaves it, and that the router obeys the guard's `navigateTo`.

One Phase 3 defect was fixed here rather than deferred: `useApiQuery`'s `getCachedData` served its 15-second window to `refreshNuxtData` as well, so a post-write `invalidate()` could answer from the payload the write had just invalidated (`experimental.granularCachedData` consults it on every run), and it read a legitimately `null` payload as a cache miss. Both are pinned by `tests/nuxt/composables/useApiQuery.spec.ts`.
