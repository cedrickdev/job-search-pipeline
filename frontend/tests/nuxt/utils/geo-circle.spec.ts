// The radius ring is decoration, so the only thing worth pinning is that it is an honest
// circle: a closed ring whose every vertex is the requested distance from the centre on
// the ground (§16). If this drifts into a screen-space ellipse the map would imply a
// reach it does not have — north–south and east–west would read as different distances.
//
// Nothing here re-decides membership; PostGIS did (§34). These are geometry unit tests.
import { describe, expect, it } from 'vitest'
import { circlePolygon, radiusCirclesCollection } from '~/utils/geo-circle'
import type { RadiusCircle } from '~/utils/geo-circle'

const EARTH_RADIUS_KM = 6371

/** Great-circle distance in km — the same measure the ring claims to hold constant. */
function haversineKm(a: [number, number], b: [number, number]): number {
  const toRad = (d: number) => (d * Math.PI) / 180
  const [lng1, lat1] = a
  const [lng2, lat2] = b
  const dLat = toRad(lat2 - lat1)
  const dLng = toRad(lng2 - lng1)
  const s = Math.sin(dLat / 2) ** 2
    + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(s))
}

const LAUSANNE: RadiusCircle = { longitude: 6.6323, latitude: 46.5197, radiusKm: 25 }

describe('circlePolygon', () => {
  it('closes the ring — last point equals the first', () => {
    const ring = circlePolygon(LAUSANNE).geometry.coordinates[0]!
    expect(ring[0]).toEqual(ring[ring.length - 1])
  })

  it('has steps + 1 vertices, the closing point included', () => {
    expect(circlePolygon(LAUSANNE, 64).geometry.coordinates[0]).toHaveLength(65)
    expect(circlePolygon(LAUSANNE, 8).geometry.coordinates[0]).toHaveLength(9)
  })

  // The point of a geodesic ring: every vertex the same ground distance from the centre,
  // so the circle does not flatten into a north–south lie at Swiss latitudes.
  it('places every vertex the requested distance from the centre', () => {
    const centre: [number, number] = [LAUSANNE.longitude, LAUSANNE.latitude]
    for (const vertex of circlePolygon(LAUSANNE).geometry.coordinates[0]!) {
      // GeoJSON types a Position as number[]; the ring is 2D by construction.
      expect(haversineKm(centre, vertex as [number, number])).toBeCloseTo(25, 1)
    }
  })

  it('carries a matched-radius label through, and null when there is none', () => {
    expect(circlePolygon({ ...LAUSANNE, label: 'Home · 25 km' }).properties.label)
      .toBe('Home · 25 km')
    expect(circlePolygon(LAUSANNE).properties.label).toBeNull()
  })
})

describe('radiusCirclesCollection', () => {
  it('is one polygon feature per circle', () => {
    const fc = radiusCirclesCollection([LAUSANNE, { ...LAUSANNE, radiusKm: 50 }])
    expect(fc.type).toBe('FeatureCollection')
    expect(fc.features).toHaveLength(2)
    expect(fc.features.every(f => f.geometry.type === 'Polygon')).toBe(true)
  })

  it('is empty for no circles — the layer draws nothing rather than a stale ring', () => {
    expect(radiusCirclesCollection([]).features).toEqual([])
  })
})
