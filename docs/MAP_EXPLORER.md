# V2 Interactive Map Explorer

Built in Phase 8. This document describes the frontend that turns the Phase 7 geo
API into a *place you can look at*: a MapLibre view and a list, side by side, over
the same three reads.

```
/map ─> mapExplorer store (mode · selectedId · scope)
     ├─> useGeoExplorer ─> GET /api/v2/geo/opportunities        ┐
     │                     GET /api/v2/geo/companies            ├─ one query string,
     │                     GET /api/v2/me/search-profiles/{id}… ┘  reused as the cache key
     ├─> map-projection ─> GeoJSON FeatureCollection ─> OpportunityMap (setData)
     └─> useMapViewport ─> URL ⇄ camera, "n,s,e,w" bounds
```

The point of the phase in one sentence: **the map draws what the server placed,
and never invents a place it was not given.** An `UNRESOLVED` posting is a row in
the list with no pin, not a marker at `0,0`; a `REMOTE` one shows a distance of
`—`, not a false `0 km`; membership in a radius or a country is PostGIS's answer
(Phase 7), and nothing here re-decides it. The backend already refused to lie
about location (`GEO_SEARCH.md` §The invariant); this phase refuses to draw a lie.

## The invariant

Six claims, each held by a test rather than by review:

- **The server placed these points, not the client.** `utils/map-projection.ts`
  drops any item without a `location.point` from the marker set and keeps it in
  the list; it never synthesises a coordinate. A pure-remote or unresolved role
  therefore *cannot* acquire a pin, because there is no code path that would give
  it one (§8, §9). `tests/nuxt/utils/map-projection.spec.ts`.
- **One selection, shared.** `OpportunityMap` holds no selection of its own: a
  click emits an id, and the highlighted marker is whichever id the page passes
  back down from the store. The map and the list read the same
  `mapExplorer.selectedId`, so they cannot disagree (§12).
  `tests/nuxt/stores/mapExplorer.spec.ts`, `…/pages/map.spec.ts`.
- **A pan is not a query.** Moving the map updates the URL and arms the "Search
  this area" button; it does **not** fetch. A new read of the corpus happens only
  when the user asks for it, so panning is free and the result set is stable while
  you read it (§20). `tests/nuxt/pages/map.spec.ts`,
  `tests/e2e/map.spec.ts`.
- **The cache key is the request URL.** `useGeoExplorer` builds one query string
  with `URLSearchParams` and uses it both as the fetch URL and inside the
  `useApiQuery` key, so two views that ask the same question share one cached
  answer and two that differ never collide.
  `tests/nuxt/composables/useGeoExplorer.spec.ts`.
- **No fabricated fit.** Nothing on this surface shows a match score, a "% fit" or
  a ranking — the card, the list and the legend state *where* a role is and how
  sure the geocoder was, never *how good* it is (§42). Whether a candidate may
  take a role is Phase 9's question. `tests/nuxt/components/MapResultCard.spec.ts`,
  `…/MapLegend.spec.ts`.
- **The map failing does not take the list with it.** WebGL absent, a style that
  will not load, a construction throw — each becomes an `error` event and a small
  overlay, while the list column keeps rendering the same data (§30).
  `tests/nuxt/components/OpportunityMap.spec.ts`, `…/pages/map.spec.ts`.

V1 is untouched; so is the Phase 7 API. This phase adds a frontend and no backend
code, and no migration.

## Layout

```
frontend/app/pages/map.vue                 the orchestrator: guard, mode, filters,
                                            the three reads, selection, URL sync, mobile
frontend/app/components/map/
  OpportunityMap.vue    the one WebGL owner — MapLibre lifecycle, clustering,
                        radius rings, the highlight ring; imports maplibre-gl
                        dynamically inside onMounted, never at module scope
  MapControls.vue       the [Opportunities][Companies] toggle (role="tab")
  MapFilters.vue        the filter form; commits on Apply, not per keystroke
  MapList.vue           the results list — placed and unplaced, tagged, with distance
  MapResultCard.vue     the selected item; a company links to /companies/{id}
  MapLegend.vue         what the colours mean, and that pin-less roles are in the list
  MapSearchAreaButton.vue   the explicit "search this area"
frontend/app/composables/
  useGeoExplorer.ts     the three reads + the query-string builders (key == URL)
  useMapViewport.ts     URL ⇄ camera, markersBounds, "n,s,e,w" serialisation
frontend/app/stores/mapExplorer.ts     mode · selectedId · selectedSearchProfileId
frontend/app/utils/
  map-projection.ts     API items → GeoJSON markers; the unplaced complement
  geo-format.ts         distance / status / remote-scope → human labels
  geo-circle.ts         a radius → a geodesic ring polygon (decoration only)
frontend/app/types/map.ts              marker, feature-property and filter-form types
frontend/nuxt.config.ts                runtimeConfig.public.mapStyleUrl / mapAttribution
frontend/app/layouts/default.vue       the session-gated nav link
```

## The map component

`OpportunityMap.vue` is the only place in the app where WebGL happens (§5), and
three rules shape it:

- **Client only.** `maplibre-gl` and its CSS are imported dynamically inside
  `onMounted`, and the page wraps the component in `<ClientOnly>`. `nuxt generate`
  runs this file through Node with no WebGL and no `window`; a module-scope
  `import 'maplibre-gl'` would evaluate that code at prerender and break the build.
  The map instance is a plain `let`, never a `ref` — a WebGL context has no
  business being wrapped in a Vue reactive proxy.
- **Update, never recreate.** The map is built once on mount and torn down once on
  unmount (`map.remove()`, which drops the GL context and every listener). New
  results arrive as a new `FeatureCollection` and go in through
  `source.setData(...)`; nothing in between rebuilds the map (§5).
- **It draws; it does not decide.** Clustering is MapLibre's own
  (`cluster: true`, `promoteId: 'id'`) (§11); the radius rings are a `fill`+`line`
  layer over a GeoJSON polygon and no expression reads them to include or exclude a
  point (§16, §34). Approximate points (company fallback, coarse geocode) are
  painted in the warn colour so the map never claims an exact address it does not
  have (§7).

Selection is one string in, one string out. A point click emits its id; a cluster
click zooms into the cluster (`getClusterExpansionZoom`) and selects nothing; a
click that hit neither clears the selection. The highlight layer's filter is set
to the id the parent passes back — the component never writes its own selection.

## The reads, and which one runs

`mapExplorer.mode` and `selectedSearchProfileId` decide which of the three reads is
live; `useApiQuery`'s `enabled` keeps exactly one fetching at a time:

| Mode / scope | Endpoint | Owner-scoped? |
| --- | --- | --- |
| opportunities, no scope | `GET /api/v2/geo/opportunities` | no — shared corpus |
| companies | `GET /api/v2/geo/companies` | no — shared corpus |
| opportunities, saved-search scope | `GET /api/v2/me/search-profiles/{id}/opportunities` | yes — a profile is user-owned |

The open reads are over the shared corpus, like the company directory: guarded
because the surface is, but not per-user. The saved-search read *is* owner-scoped,
and a profile belonging to another account comes back absent and is mapped from
`search_profile_not_found` to `null` — the same "nothing here" the backend already
returns as a 404 (`GEO_SEARCH.md` §The API), so a wrong id never distinguishes
"not yours" from "never existed".

Every filter control is a parameter the Phase 7 API already takes — `radius`,
`country`, `opportunity_type`, `workplace_mode`, `remote`, and `bounds` — repeated
or single exactly as `GeoSearchParams` parses them. **The form never filters
client-side** (§34): it edits a query, and the answer is the server's.

## Sharing a view

The URL is the whole state, and it carries no private data (§14): `mode`, the
camera (`lat`, `lng`, `zoom`), the `profile` id of a saved-search scope, and the
committed filters. Opening a shared link seeds the store *before* any watcher is
armed, so it lands in the right mode and scope without echoing a write straight
back out; a link that pinned a camera is honoured as-is, and one that did not lets
the results frame themselves (`fitBounds`, §22). The write back to the URL is
debounced, so a drag does not spam the history.

## Configuration

The base map style is the one deployment-specific piece, and it is deliberately
**not** a committed key. `runtimeConfig.public.mapStyleUrl` defaults to MapLibre's
keyless demo style so `npm run dev` needs no environment, and a deployment
overrides it with `NUXT_PUBLIC_MAP_STYLE_URL` — baked at `nuxt generate` time,
because the app is static and FastAPI serves the output, so nothing reads env at
runtime. `mapAttribution`, when set, is *appended* to the style's own credit
through the `AttributionControl`, never a replacement, so a provider's required
attribution cannot be dropped (§36).

## Two boundaries worth naming

- **The radius overlay is a layer, not a component.** The phase order (§41)
  imagined a `RadiusOverlay`; it is realised instead as the `radius` source and its
  `fill`/`line` layers *inside* `OpportunityMap`, because a second MapLibre-aware
  component over the same map instance would duplicate the one thing this file
  exists to keep in one place. The behaviour §41 asked for is unchanged — a visual
  ring, membership owned by PostGIS.
- **A radius is centred on the view at the moment it is applied.** Turning the
  radius on snapshots the current map centre and pins the ring there; later panning
  does not drag it around. This is the MVP boundary: there is no draggable centre
  handle and no typed coordinate. Moving the ring means re-centring the map and
  applying again.

## Tests

No test here touches a live tile server, font server, job board or LLM. The unit
suite mocks `maplibre-gl` (happy-dom has no WebGL) and stubs `fetch`; the browser
suite runs a *real* MapLibre map against a stubbed offline style and answers every
`/api/**` from a table.

| Suite | Covers |
| --- | --- |
| `tests/nuxt/utils/map-projection.spec.ts` | items → markers, the unplaced complement, approximate flagging, flat GeoJSON properties |
| `tests/nuxt/utils/geo-format.spec.ts` | the distance / status / remote-scope labels, including `—` for no distance |
| `tests/nuxt/utils/geo-circle.spec.ts` | the geodesic ring — closed, `steps+1` vertices, every vertex the requested distance from the centre |
| `tests/nuxt/composables/useMapViewport.spec.ts` | URL ⇄ camera, range validation, `markersBounds`, `"n,s,e,w"` |
| `tests/nuxt/composables/useGeoExplorer.spec.ts` | the query-string builders, the key == URL agreement, the `enabled` gating, the cross-user `null` |
| `tests/nuxt/stores/mapExplorer.spec.ts` | mode / selection / scope transitions and their idempotence |
| `tests/nuxt/components/*.spec.ts` | the seven components — the toggle, filters, list, card (no score), legend, search button, and the MapLibre lifecycle against a fake map |
| `tests/nuxt/pages/map.spec.ts` | the page wiring — endpoint per mode/scope, an unplaceable role in the list, selection sync, "search this area" sends bounds |
| `tests/e2e/map.spec.ts` | a real map in Chromium — it boots, the mode toggle and `?profile` pick the right read, Apply re-queries, a pan searches only on the button, clustering does not error, the mobile switch, the guard |

## Known risks

- **The base demo style is not a production style.** The default
  `demotiles.maplibre.org` style is for development; a deployment that ships it
  gets a low-detail world map. Pointing `NUXT_PUBLIC_MAP_STYLE_URL` at a real
  provider is a deployment step, not a code change — stated rather than hidden.
- **The radius ring is a single-view snapshot.** As above: no draggable handle, no
  typed centre. Adequate for "within 25 km of here", not for exploring several
  radii at once.
- **Clustering is visual; the count is MapLibre's.** A cluster bubble's number is
  what MapLibre aggregated from the points in view, not a server total. The list
  and the count label are the authoritative counts (and the count label marks a
  full page with a `+`).
- **`nuxt dev` logs a `maplibre-gl-worker.mjs` optimize-deps warning.** It is Vite
  pre-bundling noise from the map worker and is non-fatal — the dev server, the
  browser suite and `nuxt generate` all run through it. It does not appear in the
  static build output.

## Not in Phase 8

Matching, ranking or eligibility of any kind — no score reaches this surface, by
design (§42), and whether a candidate may legally take a remote role is Phase 9's
question. A public (unauthenticated) map. Drawing or editing a `SearchArea` from
the map — the saved-search scope is read-only here. A draggable radius centre,
multiple simultaneous radii, isochrones or travel-time. Reverse geocoding from a
click. Server-driven clustering or tiled vector delivery of the corpus. The
background worker, Redis and the browser worker.
