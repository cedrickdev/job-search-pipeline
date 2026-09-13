// A radius as a polygon ring — for drawing only.
//
// The circle a user sees is decoration over an answer PostGIS already gave: the backend
// decides what is within a radius with `ST_DWithin` (docs/GEO_SEARCH.md), and nothing
// here re-checks that (§16, §34). This turns a centre and a distance into a ring the map
// can stroke, and that is all it does. The points are spaced by walking a constant
// distance around the centre (the spherical direct problem), so the ring stays a circle
// on the ground rather than a screen-space oval that lies about distance north–south.
import type { Feature, FeatureCollection, Polygon } from 'geojson'

/** A centre and a distance, with an optional label carried through from a matched radius. */
export interface RadiusCircle {
  longitude: number
  latitude: number
  radiusKm: number
  label?: string | null
}

const EARTH_RADIUS_KM = 6371
const TWO_PI = 2 * Math.PI
const toRad = (deg: number) => (deg * Math.PI) / 180
const toDeg = (rad: number) => (rad * 180) / Math.PI

/**
 * One circle as a closed polygon ring of `steps` points (64 is smooth at city scale). The
 * ring is geodesic — each point is the same ground distance from the centre — so it reads
 * as an honest radius rather than a flattened ellipse.
 */
export function circlePolygon(circle: RadiusCircle, steps = 64): Feature<Polygon, { label: string | null }> {
  const lat = toRad(circle.latitude)
  const lng = toRad(circle.longitude)
  const angular = circle.radiusKm / EARTH_RADIUS_KM
  const ring: [number, number][] = []
  for (let i = 0; i <= steps; i++) {
    const bearing = (i / steps) * TWO_PI
    const lat2 = Math.asin(
      Math.sin(lat) * Math.cos(angular) + Math.cos(lat) * Math.sin(angular) * Math.cos(bearing),
    )
    const lng2 = lng + Math.atan2(
      Math.sin(bearing) * Math.sin(angular) * Math.cos(lat),
      Math.cos(angular) - Math.sin(lat) * Math.sin(lat2),
    )
    ring.push([toDeg(lng2), toDeg(lat2)])
  }
  return {
    type: 'Feature',
    geometry: { type: 'Polygon', coordinates: [ring] },
    properties: { label: circle.label ?? null },
  }
}

/** Several circles as one FeatureCollection, ready for a fill/line layer's source. */
export function radiusCirclesCollection(circles: RadiusCircle[]): FeatureCollection<Polygon, { label: string | null }> {
  return { type: 'FeatureCollection', features: circles.map(circle => circlePolygon(circle)) }
}
