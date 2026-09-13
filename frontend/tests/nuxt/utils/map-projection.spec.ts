// The one rule the map lives or dies by: a thing is placed only if the server gave it a
// point. Everything the phase forbids is a consequence, so it is all asserted here.
//
//   * UNRESOLVED → no point → no marker, but still in the list (§8).
//   * pure REMOTE → no point → no marker (§9); a remote role that keeps a physical anchor
//     is placed, with its scope preserved.
//   * COMPANY_FALLBACK → has a point → placed, but flagged approximate (§7).
//
// None of this is geographic filtering — the server already decided membership (§34); these
// functions only ask "did it come back with coordinates?".
import { describe, expect, it } from 'vitest'
import {
  companyMarkers,
  opportunityMarkers,
  toFeatureCollection,
  unplacedOpportunities,
} from '~/utils/map-projection'
import { companyGeoItem, geoLocation, opportunityGeoItem } from '../support/v2-fixtures'

describe('opportunityMarkers', () => {
  it('drops an unresolved row — no pin at 0,0 or a country centre', () => {
    const items = [
      opportunityGeoItem({ id: 'a', status: 'RESOLVED' }),
      opportunityGeoItem({ id: 'b', status: 'UNRESOLVED', location: null, distance_meters: null }),
    ]
    expect(opportunityMarkers(items).map(m => m.id)).toEqual(['a'])
  })

  it('drops a pure-remote row that carries no point', () => {
    const items = [
      opportunityGeoItem({
        id: 'remote',
        status: 'REMOTE',
        location: null,
        remote_scope: 'REMOTE_ANYWHERE',
        distance_meters: null,
      }),
    ]
    expect(opportunityMarkers(items)).toHaveLength(0)
  })

  it('keeps a remote role that still has a physical anchor, scope and all', () => {
    const items = [
      opportunityGeoItem({
        id: 'hybrid',
        status: 'RESOLVED',
        remote_scope: 'HYBRID',
        location: geoLocation(),
      }),
    ]
    const [marker] = opportunityMarkers(items)
    expect(marker?.remoteScope).toBe('HYBRID')
    expect(marker?.approximate).toBe(false)
  })

  // The pin is the employer's office; the flag is what lets the UI say so (§7).
  it('places a company fallback but marks it approximate', () => {
    const items = [opportunityGeoItem({ status: 'COMPANY_FALLBACK', location: geoLocation() })]
    const [marker] = opportunityMarkers(items)
    expect(marker?.approximate).toBe(true)
    expect(marker?.status).toBe('COMPANY_FALLBACK')
  })

  it('reads coordinates from the point, in [lng, lat] nowhere yet — that is the feature', () => {
    const items = [opportunityGeoItem({
      location: geoLocation({ point: { latitude: 47.1, longitude: 8.2 } }),
    })]
    const [marker] = opportunityMarkers(items)
    expect(marker?.longitude).toBe(8.2)
    expect(marker?.latitude).toBe(47.1)
  })
})

describe('unplacedOpportunities', () => {
  it('is the exact complement of what gets a marker', () => {
    const placed = opportunityGeoItem({ id: 'placed' })
    const remote = opportunityGeoItem({ id: 'remote', status: 'REMOTE', location: null })
    const unresolved = opportunityGeoItem({ id: 'unresolved', status: 'UNRESOLVED', location: null })
    const items = [placed, remote, unresolved]

    expect(opportunityMarkers(items).map(m => m.id)).toEqual(['placed'])
    expect(unplacedOpportunities(items).map(i => i.id)).toEqual(['remote', 'unresolved'])
  })

  it('treats a location whose point is null as unplaced', () => {
    const item = opportunityGeoItem({ location: geoLocation({ point: null }) })
    expect(unplacedOpportunities([item])).toHaveLength(1)
    expect(opportunityMarkers([item])).toHaveLength(0)
  })
})

describe('companyMarkers', () => {
  it('drops an employer with no point', () => {
    const item = companyGeoItem({ location: geoLocation({ point: null }) })
    expect(companyMarkers([item])).toHaveLength(0)
  })

  it('marks a city-precise office as approximate but an exact one as not', () => {
    const coarse = companyGeoItem({ location: geoLocation({ precision: 'CITY' }) })
    const exact = companyGeoItem({ location: geoLocation({ precision: 'EXACT_ADDRESS' }) })
    expect(companyMarkers([coarse])[0]?.approximate).toBe(true)
    expect(companyMarkers([exact])[0]?.approximate).toBe(false)
  })

  it('carries the company id and name, not the row id', () => {
    const [marker] = companyMarkers([companyGeoItem()])
    expect(marker?.id).toBe('44444444-4444-4444-8444-444444444444')
    expect(marker?.name).toBe('Logitech')
  })
})

describe('toFeatureCollection', () => {
  it('emits GeoJSON points with flat, click-resolvable properties', () => {
    const markers = opportunityMarkers([opportunityGeoItem({ id: 'x' })])
    const fc = toFeatureCollection(markers)

    expect(fc.type).toBe('FeatureCollection')
    const [feature] = fc.features
    expect(feature?.geometry.type).toBe('Point')
    // [lng, lat] order — the GeoJSON contract MapLibre reads.
    expect(feature?.geometry.coordinates).toEqual([6.6323, 46.5197])
    expect(feature?.properties).toMatchObject({ id: 'x', kind: 'opportunity' })
  })

  it('is empty for an empty set — the source clears rather than stales', () => {
    expect(toFeatureCollection([]).features).toEqual([])
  })
})
