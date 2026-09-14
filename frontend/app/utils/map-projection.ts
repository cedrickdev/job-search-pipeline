// From geo reads to map markers, and the one rule that matters: a thing is placed on the
// map only if it has a point. Everything the phase brief forbids follows from that.
//
//   * An `UNRESOLVED` row has no point, so it never becomes a marker — no pin at 0,0, no
//     pin at a country centre (§8). It stays in the list, where the caller can still act
//     on it, with "—" for a distance.
//   * A pure `REMOTE` row has no point either, so it is not on the map (§9); a hybrid or
//     country-restricted remote role that DOES carry a physical anchor keeps its point and
//     is placed, with its scope preserved for the card.
//   * A `COMPANY_FALLBACK` row has a point — the employer's office — so it IS placed, but
//     flagged `approximate` so the UI never claims it is the role's own address (§7).
//
// None of this is geographic filtering. The API already decided what is inside a radius
// or a country (PostGIS did — docs/GEO_SEARCH.md); this only asks "did the server give
// coordinates for it?", which is a rendering question, not a search one (§34).
import type { Feature, FeatureCollection, Point } from 'geojson'
import type { CompanyGeoItem, OpportunityGeoItem } from '~/types/v2'
import type {
  MapCompanyMarker,
  MapFeatureProperties,
  MapMarker,
  MapOpportunityMarker,
} from '~/types/map'

/** A location coarser than a street address — the card should hedge its wording. */
function isApproximatePrecision(precision: string): boolean {
  return precision !== 'EXACT_ADDRESS' && precision !== 'POSTAL_CODE'
}

/** The placeable opportunities, projected. The unplaceable ones are dropped here (see below). */
export function opportunityMarkers(items: OpportunityGeoItem[]): MapOpportunityMarker[] {
  const markers: MapOpportunityMarker[] = []
  for (const item of items) {
    const location = item.location
    const point = location?.point
    if (!location || !point) continue
    markers.push({
      kind: 'opportunity',
      id: item.id,
      title: item.title,
      companyName: item.company_name,
      companyId: item.company_id,
      longitude: point.longitude,
      latitude: point.latitude,
      status: item.status,
      approximate: item.status === 'COMPANY_FALLBACK',
      precision: location.precision,
      remoteScope: item.remote_scope,
      distanceMeters: item.distance_meters,
    })
  }
  return markers
}

/** The employers, projected. A company's `location` is non-null, but its `point` may be. */
export function companyMarkers(items: CompanyGeoItem[]): MapCompanyMarker[] {
  const markers: MapCompanyMarker[] = []
  for (const item of items) {
    const point = item.location.point
    if (!point) continue
    markers.push({
      kind: 'company',
      id: item.company.id,
      name: item.company.name,
      longitude: point.longitude,
      latitude: point.latitude,
      status: item.status,
      approximate: isApproximatePrecision(item.location.precision),
      precision: item.location.precision,
      isHeadquarters: item.is_headquarters,
      distanceMeters: item.distance_meters,
    })
  }
  return markers
}

/**
 * The opportunities that cannot be placed — no location, or a location with no point.
 * The list shows these (with "—" for distance); the map must not invent a pin for them.
 */
export function unplacedOpportunities(items: OpportunityGeoItem[]): OpportunityGeoItem[] {
  return items.filter(item => !item.location?.point)
}

/** One marker as a GeoJSON point feature carrying only what the source and a click need. */
function toFeature(marker: MapMarker): Feature<Point, MapFeatureProperties> {
  return {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [marker.longitude, marker.latitude] },
    properties: {
      id: marker.id,
      kind: marker.kind,
      status: marker.status,
      approximate: marker.approximate,
    },
  }
}

/** The markers as one FeatureCollection, ready for `source.setData(...)`. */
export function toFeatureCollection(markers: MapMarker[]): FeatureCollection<Point, MapFeatureProperties> {
  return { type: 'FeatureCollection', features: markers.map(toFeature) }
}
