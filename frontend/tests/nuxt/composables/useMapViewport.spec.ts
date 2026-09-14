// The URL is the map's memory: a shared link reopens where it pointed, and a stranger's
// link must never throw (§14, §22). So each field is parsed and range-checked on its own —
// a plausible lat/lng with a junk zoom still opens where it points, at the default zoom.
//
// These are pure functions on purpose (§49): no MapLibre, no WebGL, so they run here and
// the one component that owns a live map just calls them.
import { describe, expect, it } from 'vitest'
import {
  DEFAULT_VIEWPORT,
  boundsToString,
  markersBounds,
  parseViewport,
  viewportToQuery,
} from '~/composables/useMapViewport'

describe('parseViewport', () => {
  it('reads a full viewport, lng/lat swapped into MapLibre order', () => {
    expect(parseViewport({ lat: '46.52', lng: '6.63', zoom: '11' }))
      .toEqual({ center: [6.63, 46.52], zoom: 11 })
  })

  it('falls back field by field — a junk zoom keeps the centre it could read', () => {
    expect(parseViewport({ lat: '46.52', lng: '6.63', zoom: 'banana' }))
      .toEqual({ center: [6.63, 46.52], zoom: DEFAULT_VIEWPORT.zoom })
  })

  it('rejects an out-of-range coordinate rather than pan off the earth', () => {
    // lat 200 is impossible; the whole centre falls back, zoom is still honoured.
    expect(parseViewport({ lat: '200', lng: '6.63', zoom: '9' }))
      .toEqual({ center: DEFAULT_VIEWPORT.center, zoom: 9 })
    expect(parseViewport({ lat: '46.52', lng: '999', zoom: '9' }).center)
      .toEqual(DEFAULT_VIEWPORT.center)
  })

  it('is the default when the query carries no viewport at all', () => {
    expect(parseViewport({})).toEqual(DEFAULT_VIEWPORT)
  })

  it('takes the first value when a param arrives repeated', () => {
    expect(parseViewport({ lat: ['46.52', '0'], lng: ['6.63', '0'], zoom: ['11'] }))
      .toEqual({ center: [6.63, 46.52], zoom: 11 })
  })

  it('treats an empty string as absent, not as zero', () => {
    expect(parseViewport({ lat: '', lng: '', zoom: '' })).toEqual(DEFAULT_VIEWPORT)
  })
})

describe('viewportToQuery', () => {
  it('rounds to the precision a URL should carry — five places, two for zoom', () => {
    expect(viewportToQuery({ center: [6.632312345, 46.519789], zoom: 11.456 }))
      .toEqual({ lat: '46.51979', lng: '6.63231', zoom: '11.46' })
  })
})

describe('boundsToString', () => {
  it('is the north,south,east,west the bounds param expects', () => {
    expect(boundsToString({ north: 47, south: 46, east: 7, west: 6 })).toBe('47,46,7,6')
  })
})

describe('markersBounds', () => {
  it('is null for an empty set — there is nothing to frame', () => {
    expect(markersBounds([])).toBeNull()
  })

  it('is the bounding box of the points', () => {
    expect(markersBounds([
      { longitude: 6, latitude: 46 },
      { longitude: 8, latitude: 47 },
      { longitude: 7, latitude: 45 },
    ])).toEqual({ north: 47, south: 45, east: 8, west: 6 })
  })

  it('frames a single point as a degenerate box on that point', () => {
    expect(markersBounds([{ longitude: 6.63, latitude: 46.52 }]))
      .toEqual({ north: 46.52, south: 46.52, east: 6.63, west: 6.63 })
  })
})
