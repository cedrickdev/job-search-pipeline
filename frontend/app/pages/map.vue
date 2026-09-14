<!--
  The map explorer. One screen, three sibling views kept in agreement by the store
  (stores/mapExplorer.ts): a mode toggle, a map, and a list. The page is the wiring —
  it owns the filter form, chooses which geo read runs, and is the single writer of the
  shareable URL. It holds no map internals; those live in MapOpportunityMap (§5).

  What is deliberate here:

  * **One selection, both ways (§12).** A marker click and a row click both call
    `store.select`; the map's highlight and the list's active row both read
    `store.selectedId`. There is no second copy to drift.

  * **The URL is written in one place (§14).** A debounced watcher mirrors mode, profile
    and viewport into the query string — never private data, never a candidate's home. A
    shared link reopens the same view; a junk value in it falls back rather than throwing
    (parseViewport in useMapViewport.ts).

  * **The map never re-queries on its own (§20).** Panning updates the viewport and the URL;
    fetching the new area waits for the explicit "Search this area" button.

  * **Nothing geographic is decided here (§34).** The filter form only shapes API params;
    PostGIS decides membership, and this renders whatever came back — placeable rows on the
    map, every row in the list (§8, §9).
-->
<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { useDebounceFn } from '@vueuse/core'
import OpportunityMap from '~/components/map/OpportunityMap.vue'
import { useMapExplorerStore } from '~/stores/mapExplorer'
import type { ExplorerMode } from '~/stores/mapExplorer'
import { markersBounds, useMapViewport, viewportToQuery } from '~/composables/useMapViewport'
import {
  GEO_PAGE_SIZE,
  useGeoCompaniesQuery,
  useGeoOpportunitiesQuery,
  useSavedSearchOpportunitiesQuery,
} from '~/composables/useGeoExplorer'
import type { GeoFilters, SavedGeoFilters } from '~/composables/useGeoExplorer'
import { companyMarkers, opportunityMarkers, toFeatureCollection } from '~/utils/map-projection'
import type { RadiusCircle } from '~/utils/geo-circle'
import { defaultMapFilterForm } from '~/types/map'
import type { CompanyGeoItem, OpportunityGeoItem } from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'

definePageMeta({ middleware: 'auth' })
useHead({ title: 'Map · Command Center' })

const route = useRoute()
const router = useRouter()
const config = useRuntimeConfig()
const store = useMapExplorerStore()

const styleUrl = config.public.mapStyleUrl as string
const attribution = (config.public.mapAttribution as string) ?? ''

// Seed the store from the link before any watcher is armed, so reopening a shared URL lands
// in the right mode and scope without echoing a write straight back out.
store.mode = route.query.mode === 'companies' ? 'companies' : 'opportunities'
store.selectedSearchProfileId = typeof route.query.profile === 'string' ? route.query.profile : null

const { viewport, bounds, applyMove } = useMapViewport()
// A link that pinned a viewport is honoured as-is; one that did not lets the results frame
// themselves (§22). This is read once, at open.
const hadUrlCenter = 'lat' in route.query && 'lng' in route.query
const autoFit = ref(!hadUrlCenter)

// --- filter form: bound live, applied on submit (mirrors the companies page) ---
const form = reactive(defaultMapFilterForm())
const applied = ref({ ...form })
// A radius, when used, is centred on the view at the moment it is applied — not a typed
// coordinate — and pinned there so later panning does not drag the ring around.
const radiusCenter = ref<[number, number] | null>(null)

function applyFilters() {
  applied.value = { ...form }
  radiusCenter.value = form.radiusEnabled
    ? [viewport.value.center[0], viewport.value.center[1]]
    : null
}

// The bounds actually sent to the API — set only by "Search this area", never by a pan.
const activeBounds = ref<string | null>(null)
function searchThisArea() {
  if (bounds.value) activeBounds.value = bounds.value
}

/** The `radius` param as `"lat,lng,km"`, or null when the radius control is off. */
const radiusParam = computed(() => {
  if (!applied.value.radiusEnabled || !radiusCenter.value) return null
  const [lng, lat] = radiusCenter.value
  return `${lat.toFixed(5)},${lng.toFixed(5)},${applied.value.radiusKm}`
})

/** The open-read filters. Role-only facets (type/workplace/remote) are added for
    opportunities only — an employer read does not take them. */
const openFilters = computed<GeoFilters>(() => {
  const filters: GeoFilters = { limit: GEO_PAGE_SIZE, offset: 0 }
  const country = applied.value.country.trim().toUpperCase()
  if (country) filters.countries = [country]
  if (activeBounds.value) filters.bounds = activeBounds.value
  if (radiusParam.value) filters.radii = [radiusParam.value]
  if (store.mode === 'opportunities') {
    filters.remote = applied.value.remote
    if (applied.value.opportunityType) filters.opportunityTypes = [applied.value.opportunityType]
    if (applied.value.workplaceMode) filters.workplaceModes = [applied.value.workplaceMode]
  }
  return filters
})

const savedFilters = computed<SavedGeoFilters>(() => ({
  bounds: activeBounds.value,
  limit: GEO_PAGE_SIZE,
  offset: 0,
}))

// A saved search scopes the map only in opportunities mode; the id is what enables the read.
const savedProfileId = computed(() =>
  store.mode === 'opportunities' ? store.selectedSearchProfileId : null)

// All three reads are declared; `enabled` keeps only the active one fetching.
const openOpps = useGeoOpportunitiesQuery(openFilters, {
  enabled: computed(() => store.mode === 'opportunities' && store.selectedSearchProfileId === null),
})
const companiesQuery = useGeoCompaniesQuery(openFilters, {
  enabled: computed(() => store.mode === 'companies'),
})
const saved = useSavedSearchOpportunitiesQuery(savedProfileId, savedFilters, {
  enabled: computed(() => store.mode === 'opportunities'),
})

const usingSaved = computed(() => savedProfileId.value !== null)
const activeStatus = computed(() => {
  if (store.mode === 'companies') return companiesQuery.status.value
  return usingSaved.value ? saved.status.value : openOpps.status.value
})
const activeError = computed(() => {
  if (store.mode === 'companies') return companiesQuery.error.value
  return usingSaved.value ? saved.error.value : openOpps.error.value
})

const opportunities = computed<OpportunityGeoItem[]>(() => {
  if (store.mode !== 'opportunities') return []
  const data = usingSaved.value ? saved.data.value : openOpps.data.value
  return data?.opportunities ?? []
})
const companies = computed<CompanyGeoItem[]>(() =>
  store.mode === 'companies' ? (companiesQuery.data.value?.companies ?? []) : [])

const markers = computed(() =>
  store.mode === 'companies' ? companyMarkers(companies.value) : opportunityMarkers(opportunities.value))
const features = computed(() => toFeatureCollection(markers.value))

const radiusCircles = computed<RadiusCircle[]>(() => {
  if (!applied.value.radiusEnabled || !radiusCenter.value) return []
  const [lng, lat] = radiusCenter.value
  return [{ longitude: lng, latitude: lat, radiusKm: applied.value.radiusKm }]
})

const selectedOpportunity = computed(() =>
  store.mode === 'opportunities'
    ? (opportunities.value.find(item => item.id === store.selectedId) ?? null)
    : null)
const selectedCompany = computed(() =>
  store.mode === 'companies'
    ? (companies.value.find(item => item.company.id === store.selectedId) ?? null)
    : null)

const resultCount = computed(() => store.mode === 'companies' ? companies.value.length : opportunities.value.length)
const noun = computed(() => store.mode === 'companies' ? 'employers' : 'opportunities')
const countLabel = computed(() => {
  if (resultCount.value === 0) return `No ${noun.value} in view`
  const more = resultCount.value >= GEO_PAGE_SIZE ? '+' : ''
  return `${resultCount.value}${more} ${noun.value}`
})

const isLoading = computed(() => activeStatus.value === 'pending' && resultCount.value === 0)
const isError = computed(() => Boolean(activeError.value))
const isEmpty = computed(() => !isLoading.value && !isError.value && resultCount.value === 0)
const errorText = computed(() => (activeError.value ? errorMessage(activeError.value) : ''))

// --- map instance & events ---
const mapRef = ref<InstanceType<typeof OpportunityMap> | null>(null)
const mapReady = ref(false)
const mapError = ref<string | null>(null)

function onMapMove(payload: { center: [number, number], zoom: number, bounds: string }) {
  applyMove(payload)
}
function onMapReady() {
  mapReady.value = true
}
function onMapError(message: string) {
  mapError.value = message
}

// Frame the results when the view was not pinned by the link — but only once per fetch,
// and never by yanking the camera away from a user who has taken over (§22).
watch(markers, (next) => {
  if (!autoFit.value || next.length === 0) return
  const box = markersBounds(next)
  if (box) {
    mapRef.value?.fitTo(box)
    autoFit.value = false
  }
})

// --- selection (one writer: the store) ---
function onSelect(id: string | null) {
  store.select(id)
}
function onCardClose() {
  store.clearSelection()
}

// --- mode & scope ---
const modeModel = computed<ExplorerMode>({
  get: () => store.mode,
  set: (mode) => {
    if (mode === store.mode) return
    store.setMode(mode)
    autoFit.value = true
  },
})
function clearScope() {
  store.setSearchProfile(null)
  autoFit.value = true
}

// --- the single URL writer (§14) ---
const writeUrl = useDebounceFn(() => {
  const query: Record<string, string> = { ...viewportToQuery(viewport.value), mode: store.mode }
  if (store.selectedSearchProfileId) query.profile = store.selectedSearchProfileId
  void router.replace({ query })
}, 300)
watch(
  [() => store.mode, () => store.selectedSearchProfileId, () => viewport.value],
  () => writeUrl(),
)

// --- mobile: one column at a time, and the canvas must re-measure when it reappears ---
const mobileView = ref<'map' | 'list'>('map')
watch(mobileView, async (view) => {
  if (view === 'map') {
    await nextTick()
    mapRef.value?.resize()
  }
})
</script>

<template>
  <div class="map-page">
    <header class="map-head">
      <h1>Map</h1>
      <MapControls v-model="modeModel" />
      <span v-if="store.selectedSearchProfileId" class="map-scope">
        Scoped to a saved search
        <button type="button" class="map-scope-clear" @click="clearScope">
          Clear
        </button>
      </span>
    </header>

    <MapFilters v-model="form" :mode="store.mode" @apply="applyFilters" />

    <div class="map-mobile-toggle" role="tablist" aria-label="View">
      <button
        type="button"
        role="tab"
        :aria-selected="mobileView === 'map'"
        :class="{ active: mobileView === 'map' }"
        @click="mobileView = 'map'"
      >
        Map
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="mobileView === 'list'"
        :class="{ active: mobileView === 'list' }"
        @click="mobileView = 'list'"
      >
        List
      </button>
    </div>

    <div class="map-body" :data-view="mobileView">
      <section class="map-col map-col--list" aria-label="Results">
        <p class="map-count">
          {{ countLabel }}
        </p>
        <p v-if="isLoading" class="ov-state">
          Loading…
        </p>
        <p v-else-if="isError" class="ov-state ov-error">
          {{ errorText }}
        </p>
        <p v-else-if="isEmpty" class="ov-empty">
          No results in this view. Try widening the filters or searching a larger area.
        </p>
        <MapList
          v-else
          :mode="store.mode"
          :opportunities="opportunities"
          :companies="companies"
          :selected-id="store.selectedId"
          @select="onSelect"
        />
      </section>

      <section class="map-col map-col--map" aria-label="Map">
        <ClientOnly>
          <OpportunityMap
            ref="mapRef"
            :features="features"
            :radius-circles="radiusCircles"
            :selected-id="store.selectedId"
            :center="viewport.center"
            :zoom="viewport.zoom"
            :style-url="styleUrl"
            :attribution="attribution"
            @select="onSelect"
            @moveend="onMapMove"
            @ready="onMapReady"
            @error="onMapError"
          />
          <template #fallback>
            <div class="map-fallback">
              Loading map…
            </div>
          </template>
        </ClientOnly>

        <MapLegend class="map-overlay map-overlay--legend" :mode="store.mode" />
        <MapSearchAreaButton
          v-if="!mapError"
          class="map-overlay map-overlay--search"
          :loading="isLoading"
          @search="searchThisArea"
        />
        <p v-if="mapError" class="map-overlay map-overlay--error" role="alert">
          {{ mapError }} The list still works.
        </p>
        <MapResultCard
          v-if="selectedOpportunity || selectedCompany"
          class="map-overlay map-overlay--card"
          :opportunity="selectedOpportunity"
          :company="selectedCompany"
          @close="onCardClose"
        />
      </section>
    </div>
  </div>
</template>

<style scoped>
.map-page {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  height: calc(100vh - 96px);
  min-height: 480px;
}
.map-head {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  flex-wrap: wrap;
}
.map-head h1 {
  font-size: 22px;
  margin: 0;
}
.map-scope {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: 12px;
  color: var(--text-dim);
  background: var(--accent-tint);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 2px var(--space-3);
}
.map-scope-clear {
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  font-weight: 600;
}

.map-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 360px 1fr;
  gap: var(--space-4);
}
.map-col {
  min-height: 0;
}
.map-col--list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  overflow: hidden;
}
.map-count {
  font-size: 13px;
  color: var(--text-dim);
  margin: 0;
}
.map-col--map {
  position: relative;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}
.map-fallback {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-dim);
  background: var(--surface-2);
}

/* Overlays float over the map column without stealing its layout height. */
.map-overlay {
  position: absolute;
  z-index: 1;
}
.map-overlay--legend {
  top: var(--space-3);
  left: var(--space-3);
}
.map-overlay--search {
  bottom: var(--space-3);
  left: 50%;
  transform: translateX(-50%);
}
.map-overlay--error {
  top: var(--space-3);
  left: 50%;
  transform: translateX(-50%);
  background: var(--surface);
  border: 1px solid var(--danger);
  color: var(--danger);
  border-radius: var(--radius-sm);
  padding: var(--space-2) var(--space-3);
  font-size: 13px;
  max-width: 80%;
}
.map-overlay--card {
  bottom: var(--space-3);
  left: var(--space-3);
  width: min(320px, calc(100% - 2 * var(--space-3)));
}

/* The mobile view switch is desktop-hidden; the two columns share the row above it. */
.map-mobile-toggle {
  display: none;
  gap: 2px;
  padding: 2px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  align-self: flex-start;
}
.map-mobile-toggle button {
  border: none;
  background: transparent;
  color: var(--text-dim);
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  padding: var(--space-1) var(--space-4);
  border-radius: 6px;
  cursor: pointer;
}
.map-mobile-toggle button.active {
  background: var(--accent);
  color: #fff;
}

@media (max-width: 860px) {
  .map-page {
    height: calc(100vh - 72px);
  }
  .map-mobile-toggle {
    display: inline-flex;
  }
  .map-body {
    grid-template-columns: 1fr;
  }
  .map-body[data-view="map"] .map-col--list,
  .map-body[data-view="list"] .map-col--map {
    display: none;
  }
}
</style>
