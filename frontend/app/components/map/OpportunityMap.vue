<!--
  The one component that owns a live MapLibre map. Everything WebGL happens here and
  nowhere else (§5): the page hands this component data and a camera and listens for
  events; it never touches the map instance.

  Three rules shape the code:

  * **Client only.** `maplibre-gl` and its CSS are imported dynamically inside `onMounted`,
    never at module scope, so `nuxt generate` (which runs this file through Node) never
    evaluates WebGL code. The page also wraps this in `<ClientOnly>`. The map instance is
    a plain `let`, not a ref — Vue has no business making a WebGL context reactive.

  * **Update, never recreate.** New results arrive as a new FeatureCollection and go in via
    `source.setData(...)` (§5). The map is built once on mount and torn down once on unmount
    (`map.remove()`); nothing in between rebuilds it.

  * **The server placed these points, not us.** This draws what it is given. Clustering is
    MapLibre's own (§11), the radius rings are decoration (§16), and no expression here
    decides what is inside a radius or a country — PostGIS already did (§34).

  Selection is one string in, one string out: a click emits `select`, and the highlighted
  point is whichever id the parent passes back down. The component holds no selection of its
  own, so the map and the list cannot disagree (§12).
-->
<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useDebounceFn } from '@vueuse/core'
import type {
  FilterSpecification,
  GeoJSONSource,
  LngLatBoundsLike,
  Map as MapLibreMap,
  MapLayerMouseEvent,
} from 'maplibre-gl'
import type { FeatureCollection, Point, Polygon } from 'geojson'
import type { MapFeatureProperties } from '~/types/map'
import type { ViewportBounds } from '~/composables/useMapViewport'
import { boundsToString } from '~/composables/useMapViewport'
import type { RadiusCircle } from '~/utils/geo-circle'
import { radiusCirclesCollection } from '~/utils/geo-circle'

const props = withDefaults(defineProps<{
  /** The markers to show, already projected (map-projection.ts). Swapped via `setData`. */
  features: FeatureCollection<Point, MapFeatureProperties>
  /** Radius rings to stroke over the map. Visual only — PostGIS owns membership (§16). */
  radiusCircles?: RadiusCircle[]
  /** The one selected id, or null. Drives the highlight ring; never written here (§12). */
  selectedId?: string | null
  /** Initial camera. Seeded from the URL by the page; the map owns it afterwards (§22). */
  center: [number, number]
  zoom: number
  /** The MapLibre style URL and attribution, both from runtime config (§1, §36). */
  styleUrl: string
  attribution?: string
}>(), {
  radiusCircles: () => [],
  selectedId: null,
  attribution: '',
})

const emit = defineEmits<{
  /** A point was clicked (its id), or empty map was clicked (null). */
  select: [id: string | null]
  /** A pan/zoom settled. Carries the new camera and the viewport as `"n,s,e,w"`. */
  moveend: [payload: { center: [number, number], zoom: number, bounds: string }]
  /** The map finished loading and is interactive. */
  ready: []
  /** The map could not be created or its style failed — the page keeps the list (§30). */
  error: [message: string]
}>()

const SOURCE = 'entities'
const RADIUS_SOURCE = 'radius'
const CLUSTERS = 'clusters'
const CLUSTER_COUNT = 'cluster-count'
const POINTS = 'points'
const HIGHLIGHT = 'point-highlight'

// Brand palette, inlined because a paint expression cannot read a CSS custom property.
// `--accent` is one value across both themes; `--warn` (approximate) likewise (tokens.css).
const ACCENT = '#7c5cff'
const ACCENT_DIM = '#5b43c0'
const APPROX = '#f0b429'
const WHITE = '#ffffff'

const container = ref<HTMLDivElement | null>(null)
// Not reactive on purpose: a MapLibre instance must never be wrapped in a Vue proxy.
let map: MapLibreMap | null = null
let loaded = false
// A fit requested before the map finished loading, replayed once it has.
let pendingFit: ViewportBounds | null = null

const emptyCollection: FeatureCollection<Point, MapFeatureProperties> = {
  type: 'FeatureCollection',
  features: [],
}

/** The highlight layer's filter: the one unclustered point whose id is selected. The
    return annotation gives the array literal MapLibre's contextual filter type. */
function highlightFilter(id: string | null): FilterSpecification {
  return ['all', ['!', ['has', 'point_count']], ['==', ['get', 'id'], id ?? '']]
}

function fitTo(bounds: ViewportBounds): void {
  if (!map || !loaded) {
    pendingFit = bounds
    return
  }
  const box: LngLatBoundsLike = [
    [bounds.west, bounds.south],
    [bounds.east, bounds.north],
  ]
  map.fitBounds(box, { padding: 64, maxZoom: 14, duration: 600 })
}

/** Called by the page after a layout change (the mobile sheet) so the canvas re-measures. */
function resize(): void {
  map?.resize()
}

defineExpose({ fitTo, resize })

// A settled move is reported at most every 200ms — panning fires `moveend` in bursts and
// the page turns each into a URL write and a "search this area" candidate (§19).
const emitMoveEnd = useDebounceFn(() => {
  if (!map) return
  const c = map.getCenter()
  const b = map.getBounds()
  emit('moveend', {
    center: [c.lng, c.lat],
    zoom: map.getZoom(),
    bounds: boundsToString({
      north: b.getNorth(),
      south: b.getSouth(),
      east: b.getEast(),
      west: b.getWest(),
    }),
  })
}, 200)

function onClusterClick(event: MapLayerMouseEvent): void {
  if (!map) return
  const hit = map.queryRenderedFeatures(event.point, { layers: [CLUSTERS] })[0]
  if (!hit) return
  const clusterId = hit.properties?.cluster_id as number | undefined
  if (clusterId === undefined) return
  const source = map.getSource(SOURCE) as GeoJSONSource
  // Promise-based in MapLibre v6 — the callback signature was dropped.
  void source.getClusterExpansionZoom(clusterId).then((zoom) => {
    const geometry = hit.geometry
    if (map && geometry.type === 'Point') {
      map.easeTo({ center: geometry.coordinates as [number, number], zoom })
    }
  })
}

function onPointClick(event: MapLayerMouseEvent): void {
  const id = event.features?.[0]?.properties?.id
  if (typeof id === 'string') emit('select', id)
}

// A click that hit neither a point nor a cluster clears the selection.
function onBackgroundClick(event: MapLayerMouseEvent): void {
  if (!map) return
  const hits = map.queryRenderedFeatures(event.point, { layers: [POINTS, CLUSTERS] })
  if (hits.length === 0) emit('select', null)
}

function addLayers(): void {
  if (!map) return

  map.addSource(SOURCE, {
    type: 'geojson',
    data: props.features,
    cluster: true,
    clusterMaxZoom: 14,
    clusterRadius: 50,
    promoteId: 'id',
  })
  map.addSource(RADIUS_SOURCE, {
    type: 'geojson',
    data: radiusCirclesCollection(props.radiusCircles) as FeatureCollection<Polygon>,
  })

  // Rings first, so markers always sit on top of their own radius decoration.
  map.addLayer({
    id: 'radius-fill',
    type: 'fill',
    source: RADIUS_SOURCE,
    paint: { 'fill-color': ACCENT, 'fill-opacity': 0.06 },
  })
  map.addLayer({
    id: 'radius-line',
    type: 'line',
    source: RADIUS_SOURCE,
    paint: { 'line-color': ACCENT, 'line-opacity': 0.4, 'line-width': 1.5, 'line-dasharray': [2, 2] },
  })

  map.addLayer({
    id: CLUSTERS,
    type: 'circle',
    source: SOURCE,
    filter: ['has', 'point_count'],
    paint: {
      'circle-color': ACCENT,
      'circle-opacity': 0.85,
      'circle-radius': ['step', ['get', 'point_count'], 16, 10, 22, 50, 28],
      'circle-stroke-width': 2,
      'circle-stroke-color': WHITE,
    },
  })
  map.addLayer({
    id: CLUSTER_COUNT,
    type: 'symbol',
    source: SOURCE,
    filter: ['has', 'point_count'],
    layout: {
      'text-field': ['get', 'point_count_abbreviated'],
      'text-font': ['Open Sans Semibold', 'Arial Unicode MS Bold'],
      'text-size': 12,
    },
    paint: { 'text-color': WHITE },
  })

  // The selection ring sits directly beneath the points so the chosen marker crowns it.
  map.addLayer({
    id: HIGHLIGHT,
    type: 'circle',
    source: SOURCE,
    filter: highlightFilter(props.selectedId),
    paint: {
      'circle-radius': 14,
      'circle-color': ACCENT,
      'circle-opacity': 0.22,
      'circle-stroke-width': 2,
      'circle-stroke-color': ACCENT_DIM,
    },
  })
  map.addLayer({
    id: POINTS,
    type: 'circle',
    source: SOURCE,
    filter: ['!', ['has', 'point_count']],
    paint: {
      // Approximate locations (company fallback, coarse geocode) wear the warn colour so
      // the map never claims an exact address it does not have (§7).
      'circle-color': ['case', ['get', 'approximate'], APPROX, ACCENT],
      'circle-radius': 7,
      'circle-stroke-width': 2,
      'circle-stroke-color': WHITE,
    },
  })

  map.on('click', CLUSTERS, onClusterClick)
  map.on('click', POINTS, onPointClick)
  map.on('click', onBackgroundClick)
  for (const layer of [CLUSTERS, POINTS]) {
    map.on('mouseenter', layer, () => { if (map) map.getCanvas().style.cursor = 'pointer' })
    map.on('mouseleave', layer, () => { if (map) map.getCanvas().style.cursor = '' })
  }
  map.on('moveend', emitMoveEnd)
}

onMounted(async () => {
  if (!container.value) return
  let maplibregl: typeof import('maplibre-gl')
  try {
    const mod = await import('maplibre-gl')
    maplibregl = (mod as { default?: typeof import('maplibre-gl') }).default ?? mod
    await import('maplibre-gl/dist/maplibre-gl.css')
  }
  catch {
    emit('error', 'The map library could not be loaded.')
    return
  }

  try {
    map = new maplibregl.Map({
      container: container.value,
      style: props.styleUrl,
      center: props.center,
      zoom: props.zoom,
      attributionControl: false,
    })
  }
  catch {
    // WebGL unavailable, or a malformed style URL. The list still works without us (§30).
    emit('error', 'The map could not be displayed in this browser.')
    return
  }

  // Attribution is never dropped (§36): the style's own credit flows through this control,
  // with any configured extra appended.
  map.addControl(new maplibregl.AttributionControl({
    compact: true,
    customAttribution: props.attribution || undefined,
  }))
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right')

  // A style that fails to fetch fires `error` rather than throwing; surface it once.
  let styleFailed = false
  map.on('error', (event) => {
    const message = (event as { error?: { message?: string } }).error?.message ?? ''
    if (!styleFailed && /style|sprite|glyph|tiles?\.json|fetch/i.test(message)) {
      styleFailed = true
      emit('error', 'The map style could not be loaded.')
    }
  })

  map.on('load', () => {
    if (!map) return
    addLayers()
    loaded = true
    emit('ready')
    if (pendingFit) {
      fitTo(pendingFit)
      pendingFit = null
    }
  })
})

onBeforeUnmount(() => {
  // One teardown for the whole lifecycle: drops the WebGL context and every listener.
  if (map) {
    map.remove()
    map = null
  }
  loaded = false
})

// Reactive data updates go through the sources — the map is never rebuilt (§5).
watch(() => props.features, (features) => {
  if (map && loaded) (map.getSource(SOURCE) as GeoJSONSource | undefined)?.setData(features ?? emptyCollection)
})

watch(() => props.radiusCircles, (circles) => {
  if (map && loaded) {
    (map.getSource(RADIUS_SOURCE) as GeoJSONSource | undefined)
      ?.setData(radiusCirclesCollection(circles ?? []) as FeatureCollection<Polygon>)
  }
}, { deep: true })

watch(() => props.selectedId, (id) => {
  if (!map || !loaded) return
  map.setFilter(HIGHLIGHT, highlightFilter(id))
  // Bring the chosen marker into view if we know where it is — the gentle half of §12.
  if (id) {
    const match = props.features.features.find(f => f.properties.id === id)
    if (match && match.geometry.type === 'Point') {
      map.easeTo({ center: match.geometry.coordinates as [number, number], duration: 400 })
    }
  }
})
</script>

<template>
  <div ref="container" class="opportunity-map" data-testid="opportunity-map" />
</template>

<style scoped>
.opportunity-map {
  width: 100%;
  height: 100%;
  min-height: 320px;
  background: var(--surface-2);
}

/* The default MapLibre controls are light-on-white; nudge them onto the app surface so
   they read in dark mode without restyling every inner element. */
.opportunity-map :deep(.maplibregl-ctrl-group) {
  background: var(--surface);
  border: 1px solid var(--border);
}
.opportunity-map :deep(.maplibregl-ctrl-attrib) {
  font-size: 10px;
}
</style>
