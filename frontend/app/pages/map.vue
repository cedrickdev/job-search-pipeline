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
const { t, localize: l, locale: uiLocale } = useLocale()
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
import { useDiscoveryRun } from '~/composables/useDiscovery'
import { discoveryLabels, discoveryMessage } from '~/utils/discovery-messages'
import type { DiscoveryLocale } from '~/utils/discovery-messages'
import { useLocalAutopilot } from '~/composables/useLocalAutopilot'
import { useApplicationActions } from '~/composables/useApplications'
import { useSearchProfilesQuery } from '~/composables/useAccount'
import { useRunCompanyDiscovery } from '~/composables/useCompanies'
import { ApiError } from '~/utils/api-client'
import { countryOptionsFor, useCountryPacksQuery, viewportFor } from '~/composables/useCountryPacks'

definePageMeta({ middleware: 'auth' })
useHead({ title: () => `${t('page.map')} · Command Center` })

const route = useRoute()
const router = useRouter()
const config = useRuntimeConfig()
const store = useMapExplorerStore()
const { data: searchProfiles, status: searchProfilesStatus } = useSearchProfilesQuery(true)
const { data: countryPacks } = useCountryPacksQuery()
const countryViewport = computed(() =>
  viewportFor(countryPacks.value?.packs.find(pack => pack.country === 'CH')) ?? undefined)
const countryOptions = computed(() => countryOptionsFor(countryPacks.value))

const styleUrl = config.public.mapStyleUrl as string
const tileUrl = config.public.mapTilesUrl as string
const attribution = (config.public.mapAttribution as string) ?? ''

// Seed the store from the link before any watcher is armed, so reopening a shared URL lands
// in the right mode and scope without echoing a write straight back out.
store.mode = route.query.mode === 'companies' ? 'companies' : 'opportunities'
store.selectedSearchProfileId = typeof route.query.profile === 'string' ? route.query.profile : null
const hasManuallyChangedScope = ref(false)
const ALL_COLLECTED = '__all_collected'

const searchProfileItems = computed(() => [
  { value: ALL_COLLECTED, label: l('All collected opportunities', 'Toutes les offres collectées') },
  ...(searchProfiles.value?.search_profiles ?? []).map(profile => ({
    value: profile.id,
    label: profile.search.name,
  })),
])
const searchProfileModel = computed({
  get: () => store.selectedSearchProfileId ?? ALL_COLLECTED,
  set: (profileId: string) => {
    hasManuallyChangedScope.value = true
    store.setSearchProfile(profileId === ALL_COLLECTED ? null : profileId)
    autoFit.value = true
  },
})

const { viewport, bounds, applyMove } = useMapViewport(
  computed(() => countryViewport.value ?? { center: [8.23, 46.82], zoom: 6 }),
)
// A link that pinned a viewport is honoured as-is; one that did not lets the results frame
// themselves (§22). This is read once, at open.
const hadUrlCenter = 'lat' in route.query && 'lng' in route.query
const autoFit = ref(!hadUrlCenter)

// --- filter form: bound live, applied on submit (mirrors the companies page) ---
const form = reactive(defaultMapFilterForm())
const applied = ref({ ...form })
// Geo endpoints intentionally reject an unbounded global query. Wait for the saved-search
// lookup before enabling reads, then borrow its first country for the company layer.
const scopeReady = ref(false)
watch([searchProfiles, searchProfilesStatus], ([profiles, status]) => {
  if (status === 'pending') return
  const active = profiles?.search_profiles ?? []
  if (!hasManuallyChangedScope.value && !store.selectedSearchProfileId && store.mode === 'opportunities' && active.length === 1) {
    store.setSearchProfile(active[0]!.id)
  }
  const countryArea = active.flatMap(profile => profile.search.areas)
    .find(area => area.kind === 'COUNTRY')
  if (countryArea && form.country === '') {
    form.country = countryArea.country
    applied.value = { ...form }
  }
  scopeReady.value = true
}, { immediate: true })
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
async function searchThisArea() {
  if (!bounds.value) return
  activeBounds.value = bounds.value
  // The user explicitly requested a new read. Wait until the reactive request
  // shape includes the viewport, then refresh the active source deterministically
  // instead of relying on key-change scheduling alone.
  await nextTick()
  await retryActiveQuery()
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
  catalog: true,
}))

// A saved search scopes the map only in opportunities mode; the id is what enables the read.
const savedProfileId = computed(() =>
  store.mode === 'opportunities' ? store.selectedSearchProfileId : null)
// Collection language controls only the source query.  It must not overwrite the
// application locale: a candidate can collect English postings while keeping every
// screen, label and assistive announcement in French (and vice versa).
const discoveryLocale = useCookie<DiscoveryLocale>('discovery_locale', {
  default: () => uiLocale.value, sameSite: 'lax', path: '/',
})
const activeLocale = computed<DiscoveryLocale>(() => discoveryLocale.value === 'en' ? 'en' : 'fr')
const discovery = useDiscoveryRun(savedProfileId)
const localAutopilot = useLocalAutopilot()
const applicationActions = useApplicationActions()
const companyDiscovery = useRunCompanyDiscovery()
const autopilotError = ref<string | null>(null)
const companyDiscoveryMessage = ref<string | null>(null)
const labels = computed(() => discoveryLabels[activeLocale.value])
const discoveryStatus = computed(() => discovery.errorCode.value === 'discovery_source_unapproved'
  ? l(
      'Discovery is paused: no approved Swiss source is available. Contact the administrator.',
      'Recherche suspendue : aucune source suisse autorisée n’est disponible. Contactez l’administrateur.',
    )
  : discoveryMessage(activeLocale.value, discovery.status.value))

// All three reads are declared; `enabled` keeps only the active one fetching.
const openOpps = useGeoOpportunitiesQuery(openFilters, {
  enabled: computed(() => scopeReady.value && store.mode === 'opportunities' && store.selectedSearchProfileId === null),
})
const companiesQuery = useGeoCompaniesQuery(openFilters, {
  enabled: computed(() => scopeReady.value && store.mode === 'companies'),
})
const saved = useSavedSearchOpportunitiesQuery(savedProfileId, savedFilters, {
  enabled: computed(() => scopeReady.value && store.mode === 'opportunities'),
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
const countLabel = computed(() => {
  if (resultCount.value === 0) return l(
    store.mode === 'companies' ? 'No employers in view' : 'No opportunities in view',
    store.mode === 'companies' ? 'Aucune entreprise dans cette zone' : 'Aucune offre dans cette zone',
  )
  const more = resultCount.value >= GEO_PAGE_SIZE ? '+' : ''
  return store.mode === 'companies'
    ? l(`${resultCount.value}${more} employers`, `${resultCount.value}${more} entreprises`)
    : l(`${resultCount.value}${more} opportunities`, `${resultCount.value}${more} offres`)
})

const isLoading = computed(() => activeStatus.value === 'pending' && resultCount.value === 0)
const isError = computed(() => Boolean(activeError.value))
const isEmpty = computed(() => !isLoading.value && !isError.value && resultCount.value === 0)
const errorText = computed(() => {
  if (!activeError.value) return ''
  if (!(activeError.value instanceof ApiError)) {
    return l(
      'The local service could not be reached. Keep this page open, start the app with ./start.sh, then retry.',
      'Le service local est inaccessible. Laissez cette page ouverte, lancez ./start.sh, puis réessayez.',
    )
  }
  return errorMessage(activeError.value)
})

async function retryActiveQuery() {
  if (store.mode === 'companies') await companiesQuery.refresh()
  else if (usingSaved.value) await saved.refresh()
  else await openOpps.refresh()
}

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

async function runLocalAutopilot() {
  if (!selectedOpportunity.value) return
  autopilotError.value = null
  try {
    await localAutopilot.mutateAsync(selectedOpportunity.value.id)
  }
  catch (error) {
    autopilotError.value = errorMessage(error)
  }
}

async function queueLocalAutopilot() {
  const applicationId = localAutopilot.data?.application?.id
  if (applicationId) await applicationActions.queue.mutateAsync(applicationId)
}

async function discoverCompanies() {
  companyDiscoveryMessage.value = null
  try {
    const country = applied.value.country.trim().toUpperCase()
    const outcome = await companyDiscovery.mutateAsync(country ? { country } : {})
    companyDiscoveryMessage.value = l(
      `${outcome.created} companies discovered and ${outcome.links.linked} opportunities linked.`,
      `${outcome.created} entreprises découvertes et ${outcome.links.linked} offres reliées.`,
    )
    await companiesQuery.refresh()
  }
  catch {
    // The visible mutation error below gives the user the typed server explanation.
  }
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
  hasManuallyChangedScope.value = true
  store.setSearchProfile(null)
  autoFit.value = true
}

// --- the single URL writer (§14) ---
let leavingMap = false
onBeforeRouteLeave(() => { leavingMap = true })
const writeUrl = useDebounceFn(() => {
  // A pending map-move timer must not race a document/application navigation.
  if (leavingMap || route.path !== '/map') return
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
      <h1>{{ t('page.map') }}</h1>
      <MapControls v-model="modeModel" />
      <label v-if="store.mode === 'opportunities' && searchProfileItems.length > 1" class="map-search-picker">
        <span>{{ l('Search', 'Recherche') }}</span>
        <USelect v-model="searchProfileModel" :items="searchProfileItems" value-key="value" :aria-label="l('Saved search', 'Recherche enregistrée')" />
      </label>
      <span v-if="store.selectedSearchProfileId" class="map-scope">
        {{ l('Scoped to a saved search', 'Limitée à une recherche enregistrée') }}
        <button type="button" class="map-scope-clear" @click="clearScope">
          {{ l('Clear', 'Effacer') }}
        </button>
      </span>
      <div v-if="store.mode === 'companies'" class="map-company-discovery">
        <button
          type="button"
          class="btn-primary"
          :disabled="companyDiscovery.isPending"
          :aria-busy="companyDiscovery.isPending ? 'true' : 'false'"
          @click="discoverCompanies"
        >
          {{ companyDiscovery.isPending
            ? l('Discovering companies…', 'Découverte des entreprises…')
            : l('Discover companies', 'Découvrir les entreprises') }}
        </button>
        <p v-if="companyDiscoveryMessage" class="map-company-discovery__status" role="status">
          {{ companyDiscoveryMessage }}
        </p>
        <p v-else-if="companyDiscovery.error" class="map-company-discovery__status map-company-discovery__status--error" role="alert">
          {{ errorMessage(companyDiscovery.error) }}
        </p>
      </div>
    </header>

    <MapFilters :model-value="form" :mode="store.mode" :country-options="countryOptions" @update:model-value="Object.assign(form, $event)" @apply="applyFilters" />

    <section v-if="savedProfileId" class="discovery-panel" aria-live="polite">
      <label>
        {{ labels.language }}
          <USelect v-model="discoveryLocale" :items="[{ value: 'fr', label: 'Français' }, { value: 'en', label: 'English' }]" value-key="value" :aria-label="labels.language" />
      </label>
      <button
        type="button"
        class="btn-primary"
        :disabled="discovery.busy.value || discovery.status.value === 'RUNNING'"
        :aria-busy="discovery.busy.value ? 'true' : 'false'"
        @click="discovery.refresh"
      >
        {{ discovery.busy.value ? l('Refreshing offers…', 'Actualisation des offres…') : labels.refresh }}
      </button>
      <p class="discovery-status" role="status">{{ discoveryStatus }}</p>
    </section>

    <div class="map-mobile-toggle" role="tablist" :aria-label="l('View', 'Affichage')">
      <button
        type="button"
        role="tab"
        :aria-selected="mobileView === 'map'"
        :class="{ active: mobileView === 'map' }"
        @click="mobileView = 'map'"
      >
        {{ l('Map', 'Carte') }}
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="mobileView === 'list'"
        :class="{ active: mobileView === 'list' }"
        @click="mobileView = 'list'"
      >
        {{ l('List', 'Liste') }}
      </button>
    </div>

    <div class="map-body" :data-view="mobileView">
      <section class="map-col map-col--list" :aria-label="l('Results', 'Résultats')">
        <p class="map-count">
          {{ countLabel }}
        </p>
        <p v-if="isLoading" class="ov-state">
          {{ l('Loading opportunities…', 'Chargement des offres…') }}
        </p>
        <section v-else-if="isError" class="map-state-card map-state-card--error" role="alert">
          <UIcon name="i-lucide-circle-alert" aria-hidden="true" />
          <div>
            <strong>{{ l('The offers could not be loaded', 'Les offres n’ont pas pu être chargées') }}</strong>
            <p>{{ errorText }}</p>
          </div>
          <button type="button" class="btn-ghost" @click="retryActiveQuery">
            {{ l('Retry', 'Réessayer') }}
          </button>
        </section>
        <section v-else-if="isEmpty" class="map-state-card">
          <UIcon name="i-lucide-map-pin-off" aria-hidden="true" />
          <div>
            <strong>{{ savedProfileId ? labels.empty : l('No collected opportunities in this view', 'Aucune offre collectée dans cette zone') }}</strong>
            <p>
              {{ savedProfileId
                ? l('Launch a search to collect opportunities matching this profile.', 'Lancez une recherche pour collecter les offres correspondant à ce profil.')
                : l('Choose a saved search to collect matching opportunities, or widen the filters.', 'Choisissez une recherche enregistrée pour collecter les offres correspondantes, ou élargissez les filtres.') }}
            </p>
          </div>
          <button
            v-if="savedProfileId"
            type="button"
            class="btn-primary"
            :disabled="discovery.busy.value || discovery.status.value === 'RUNNING'"
            :aria-busy="discovery.busy.value ? 'true' : 'false'"
            @click="discovery.refresh"
          >
            {{ discovery.busy.value ? l('Refreshing offers…', 'Actualisation des offres…') : labels.refresh }}
          </button>
        </section>
        <MapList
          v-else
          :mode="store.mode"
          :opportunities="opportunities"
          :companies="companies"
          :selected-id="store.selectedId"
          @select="onSelect"
        />
      </section>

      <section class="map-col map-col--map" :aria-label="l('Map', 'Carte')">
        <ClientOnly>
          <OpportunityMap
            ref="mapRef"
            :features="features"
            :radius-circles="radiusCircles"
            :selected-id="store.selectedId"
            :center="viewport.center"
            :zoom="viewport.zoom"
            :style-url="styleUrl"
            :tile-url="tileUrl"
            :attribution="attribution"
            @select="onSelect"
            @moveend="onMapMove"
            @ready="onMapReady"
            @error="onMapError"
          />
          <template #fallback>
            <div class="map-fallback">
              {{ l('Loading map…', 'Chargement de la carte…') }}
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
          {{ mapError }} {{ l('The result list remains available.', 'La liste des résultats reste disponible.') }}
        </p>
        <MapResultCard
          :discovery-locale="uiLocale"
          v-if="selectedOpportunity || selectedCompany"
          class="map-overlay map-overlay--card"
          :opportunity="selectedOpportunity"
          :company="selectedCompany"
          :autopilot-busy="localAutopilot.isPending"
          :autopilot-report="localAutopilot.data"
          :autopilot-error="autopilotError"
          :queue-busy="applicationActions.queue.isPending"
          @close="onCardClose"
          @run-autopilot="runLocalAutopilot"
          @queue-autopilot="queueLocalAutopilot"
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
.discovery-panel {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  font-size: 13px;
}
.discovery-panel p { margin: 0; }
.map-head h1 {
  font-size: 22px;
  margin: 0;
}
.map-search-picker {
  display: grid;
  grid-template-columns: auto minmax(220px, 1fr);
  align-items: center;
  gap: var(--space-2);
  color: var(--text-dim);
  font-size: 12px;
}
.map-search-picker :deep(button) { min-width: 220px; }
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
.map-company-discovery {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.map-company-discovery__status {
  margin: 0;
  color: var(--text-dim);
  font-size: 12px;
}
.map-company-discovery__status--error { color: var(--danger); }

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
.map-state-card {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: var(--space-3);
  align-items: start;
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--text);
}
.map-state-card > :first-child { color: var(--accent); font-size: 1.25rem; margin-top: 2px; }
.map-state-card strong { display: block; font-size: 14px; }
.map-state-card p { margin: var(--space-1) 0 0; color: var(--text-dim); font-size: 13px; line-height: 1.5; }
.map-state-card .btn-primary,
.map-state-card .btn-ghost { grid-column: 2; justify-self: start; }
.map-state-inline-error { grid-column: 2; margin: 0; color: var(--danger); font-size: 13px; }
.map-state-card--error { border-color: var(--danger); }
.map-state-card--error > :first-child { color: var(--danger); }
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
    height: auto;
    min-height: calc(100dvh - 180px);
  }
  .map-mobile-toggle {
    display: inline-flex;
    position: sticky;
    top: 108px;
    z-index: 11;
    background: var(--surface-2);
  }
  .map-head { align-items: stretch; flex-direction: column; gap: var(--space-2); }
  .map-search-picker { grid-template-columns: 1fr; }
  .map-search-picker :deep(button) { min-width: 0; width: 100%; }
  .map-scope { align-self: flex-start; flex-wrap: wrap; }
  .discovery-panel, .map-company-discovery { align-items: stretch; flex-direction: column; }
  .discovery-panel > :is(label, button) { width: 100%; }
  .map-company-discovery > button { width: 100%; }
  .map-body {
    grid-template-columns: 1fr;
    min-height: 55dvh;
  }
  .map-col--map { min-height: min(62dvh, 42rem); }
  .map-body[data-view="map"] .map-col--list,
  .map-body[data-view="list"] .map-col--map {
    display: none;
  }
}

@media (max-width: 520px) {
  .map-state-card { grid-template-columns: 1fr; }
  .map-state-card > :first-child { display: none; }
  .map-state-card .btn-primary,
  .map-state-card .btn-ghost { grid-column: auto; width: 100%; }
  .map-overlay--legend { max-width: calc(100% - 2 * var(--space-3)); }
  .map-mobile-toggle { width: 100%; }
  .map-mobile-toggle button { flex: 1; min-height: 44px; }
  .map-overlay--search { width: calc(100% - 2 * var(--space-3)); }
}
</style>
