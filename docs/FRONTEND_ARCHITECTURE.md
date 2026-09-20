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

Built in Phase 8 — see [Interactive Map Explorer](./MAP_EXPLORER.md) and the section below. The boundary is a single component: `components/map/OpportunityMap.vue` is the only file that imports `maplibre-gl`, and it does so dynamically inside `onMounted` so `nuxt generate` never evaluates WebGL code.

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

## Companies (added in Phase 6)

Two pages — `companies/index.vue` (the filtered, paginated directory plus a `Run discovery` button) and `companies/[id].vue` (one employer and what every claim on the page rests on) — plus `composables/useCompanies.ts`, the first of the target domain composables listed above to be built.

Intentionally simple, because §31 of the phase order asks for the minimum that proves the API rather than a designed surface: no map, no coordinates, no scoring. The map is Phase 8.

Three rules in it are contracts rather than preferences, and [Company Discovery](./COMPANY_DISCOVERY.md) is the full statement of them:

- **A claim never appears without its status.** The ATS platform is rendered beside its `CONFIRMED`/`LIKELY` state and its evidence codes, because "on Greenhouse" concluded from a URL and concluded from a configuration file are not the same fact.
- **`UNKNOWN` is rendered as unknown, never as "no".** A verdict nobody has formed and an employer that refuses unsolicited applications are different facts, and only the second is a reason not to write. The same rule makes a null `last_checked_at` read "checked never".
- **The cache keys carry no account.** A company is a shared fact, so `companies:list:…` keys on the filters only and there is no per-user variant to invalidate. The nav link is session-gated because the pages are guarded, not because the data is private.

The suite grew again: **294** Vitest specs across 27 files in `frontend/tests/nuxt/` and **25** Playwright flows in `frontend/tests/e2e/`, still with every `/api/**` request stubbed in the browser and every test asserting that nothing went unstubbed. The four new browser flows exist for what only a browser can show: that the directory is reachable from the nav, that `has_opportunities=false` survives a real round trip, that the discovery pass leaves with its CSRF header and **no request body**, and that the guard sends an anonymous visitor to `/login`.

## Map explorer (added in Phase 8)

One page — `map.vue`, the geo explorer at `/map` — plus a seven-component `components/map/` folder, two composables (`useGeoExplorer.ts` for the three reads, `useMapViewport.ts` for the URL ⇄ camera), the `mapExplorer` store, and three pure utilities (`map-projection.ts`, `geo-format.ts`, `geo-circle.ts`). [Interactive Map Explorer](./MAP_EXPLORER.md) is the full statement; the contracts that are contracts rather than preferences:

- **One component owns WebGL.** `OpportunityMap.vue` is the only file that touches `maplibre-gl`, imported dynamically inside `onMounted` and wrapped in `<ClientOnly>`, so `nuxt generate` (Node, no WebGL) never evaluates it. The map is built once and updated through `source.setData(...)`; it is never rebuilt on new data, and its instance is a plain `let`, never a reactive `ref`.
- **The client draws; it does not decide.** No coordinate is synthesised — an unplaceable role is a list row with no pin, never a marker at `0,0` — and no membership in a radius or country is re-computed client-side; PostGIS already answered (§34). No match score appears anywhere on the surface (§42).
- **One selection, and a pan is not a query.** The map and the list read the same `mapExplorer.selectedId`, so they cannot disagree; panning updates the URL and arms "Search this area" but never fetches, so the result set is stable while it is read.
- **The style URL is config, never a committed key.** `runtimeConfig.public.mapStyleUrl` (`NUXT_PUBLIC_MAP_STYLE_URL`) names the one deployment-specific value; `mapAttribution` is appended to the style's own credit, never a replacement.

The suite grew again: **393** Vitest specs across 41 files in `frontend/tests/nuxt/` and **34** Playwright flows in `frontend/tests/e2e/`, still with every `/api/**` request stubbed in the browser and every test asserting that nothing went unstubbed. The unit suite mocks `maplibre-gl` because happy-dom has no WebGL; the nine new browser flows run a **real** MapLibre map against a stubbed offline style — the map style and its glyph ranges are intercepted alongside `/api/**`, so nothing reaches a tile or font server — to prove what only a browser can: that the map boots into a WebGL context without falling to the error state, that the mode toggle and a `?profile` link pick the right read, that Apply re-queries while a pan does not, that clustering does not error, and that the guard sends an anonymous visitor to `/login`.

## Documents and evidence (added in Phase 10)

Two guarded screens and one generation entry point, over the phase's two backends — the [Candidate Evidence Store](./CANDIDATE_EVIDENCE.md) and the [ATS Documents](./ATS_DOCUMENTS.md) pipeline. The frontend is `pages/evidence.vue` (the attested-record write surface), `pages/documents/index.vue` (the list) and `pages/documents/[id].vue` (one document's version history and guard verdicts), fed by one composable file, `composables/useDocuments.ts`, that holds both surfaces' reads and writes. Generation is launched from `components/DocumentGenerateActions.vue` on an opportunity's map result card. The contracts that are contracts rather than preferences:

- **The truth guarantee is visible in the UI, not only enforced on the server.** The evidence screen is two forms in a deliberate order — record a fact, *then* assert a claim on it — and the claim form's Assert button stays disabled until at least one evidence record is checked, because a claim resting on nothing is exactly what the backend refuses (`claim_cites_unknown_evidence`). A person sees the rule before the server has to state it.
- **A refusal is rendered, never swallowed.** A rejected document version is shown with its guard verdict in full — each violation's `code`, its `detail`, and the `offending_text` quoted — so the refusal is auditable on screen (§45). An `insufficient_evidence` (409) generation is not a thrown error but a line pointing at `/evidence`; a `document_not_found` (from a stale link) maps to a "no such document" screen, not a crash.
- **A download goes through the document id, never a storage key.** `useDownloadDocument()` streams the PDF by id; the response body never carries the artifact locator, and the button is disabled unless some version is `RENDERED`. A generation is a `POST` that invalidates the whole `documents` cache, so the list and any open detail both pick up the new attempt.
- **Both screens are session-gated.** The two nav links appear only for a signed-in visitor, and `middleware: 'auth'` sends an anonymous click to `/login?redirect=...` — the pages hold user-owned data, unlike the unconditional V1 screens.

The suite grew again: **424** Vitest specs across 45 files in `frontend/tests/nuxt/` and **38** Playwright flows in `frontend/tests/e2e/`, still with every `/api/**` request stubbed in the browser and every test asserting that nothing went unstubbed. The four new browser flows exist for what only a browser can show: that the two screens are reachable from the nav, that recording a fact — an unsafe write over user-owned data — leaves with its `X-CSRF-Token` header, that a rejected version survives to the screen with the rule it broke and the line that broke it, and that the guard sends an anonymous visitor to `/login`. No unit or browser test touches a live LLM or renderer: a "generated" document is a stubbed payload (CLAUDE.md §Testing).
