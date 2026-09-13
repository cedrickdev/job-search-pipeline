// The map camera, and the URL-shaped values around it.
//
// The pure helpers here are the ones the phase brief wants tested directly (§49): parsing
// a viewport out of a shared link, turning one back into query params, framing a set of
// results, and formatting a viewport's bounds for the `bounds` query param. Keeping them
// free of MapLibre is deliberate — they run in happy-dom with no WebGL, and the component
// that does own a live map calls them.
//
// The default viewport is a neutral country frame — Switzerland, the reference pack's
// country — never a candidate's own coordinates (§37). It is only a fallback: a saved
// search, a shared link, or the results themselves override it.
import { ref } from 'vue'

export interface Viewport {
  /** MapLibre order: `[longitude, latitude]`. The URL names them `lng`/`lat` for humans. */
  center: [number, number]
  zoom: number
}

export interface ViewportBounds {
  north: number
  south: number
  east: number
  west: number
}

/** Switzerland, roughly framed. A neutral fallback, overridden by profile/results/link. */
export const DEFAULT_VIEWPORT: Viewport = { center: [8.23, 46.82], zoom: 6 }

function firstNumber(value: unknown): number | null {
  const raw = Array.isArray(value) ? value[0] : value
  if (typeof raw !== 'string' || raw.trim() === '') return null
  const parsed = Number(raw)
  return Number.isFinite(parsed) ? parsed : null
}

/**
 * A viewport from a URL query, each field validated independently and falling back to the
 * default on its own. A shared link with a plausible `lat`/`lng` but a junk `zoom` still
 * opens where it points, just at the default zoom — a stranger's link should never throw.
 */
export function parseViewport(query: Record<string, unknown>): Viewport {
  const lat = firstNumber(query.lat)
  const lng = firstNumber(query.lng)
  const zoom = firstNumber(query.zoom)
  const latOk = lat !== null && lat >= -90 && lat <= 90
  const lngOk = lng !== null && lng >= -180 && lng <= 180
  const center: [number, number] = latOk && lngOk ? [lng, lat] : DEFAULT_VIEWPORT.center
  const z = zoom !== null && zoom >= 0 && zoom <= 24 ? zoom : DEFAULT_VIEWPORT.zoom
  return { center, zoom: z }
}

/** A viewport as the three query params the URL carries. Rounded — full float precision is noise. */
export function viewportToQuery(viewport: Viewport): { lat: string, lng: string, zoom: string } {
  return {
    lat: viewport.center[1].toFixed(5),
    lng: viewport.center[0].toFixed(5),
    zoom: viewport.zoom.toFixed(2),
  }
}

/** Bounds as the `"north,south,east,west"` string the geo API's `bounds` param expects. */
export function boundsToString(bounds: ViewportBounds): string {
  return `${bounds.north},${bounds.south},${bounds.east},${bounds.west}`
}

/**
 * The bounding box of a set of points, or `null` for an empty set. Used to frame results
 * when a link carries no viewport of its own (§22) — the map fits what came back rather
 * than opening on an arbitrary default.
 */
export function markersBounds(points: ReadonlyArray<{ longitude: number, latitude: number }>): ViewportBounds | null {
  if (points.length === 0) return null
  let north = -90
  let south = 90
  let east = -180
  let west = 180
  for (const point of points) {
    if (point.latitude > north) north = point.latitude
    if (point.latitude < south) south = point.latitude
    if (point.longitude > east) east = point.longitude
    if (point.longitude < west) west = point.longitude
  }
  return { north, south, east, west }
}

/** The reactive camera plus the current view's bounds, seeded from the URL once at setup. */
export function useMapViewport() {
  const route = useRoute()
  const viewport = ref<Viewport>(parseViewport(route.query))
  // The `"n,s,e,w"` of the current view, refreshed on every settled move. What
  // "Search this area" sends, and what a bounds-scoped query reads.
  const bounds = ref<string | null>(null)

  /** Called by the map once a pan/zoom settles (debounced upstream, on `moveend`). */
  function applyMove(next: { center: [number, number], zoom: number, bounds: string }) {
    viewport.value = { center: next.center, zoom: next.zoom }
    bounds.value = next.bounds
  }

  return { viewport, bounds, applyMove }
}
