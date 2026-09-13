// The map's own vocabulary — small, flat, JSON-safe projections of the geo reads.
//
// These are deliberately NOT the backend items. Two reasons, both from the phase brief
// (docs/MAP_EXPLORER.md §Projection):
//
//   1. MapLibre feature `properties` want flat primitives, not nested records. A marker
//      carries only what styling and a click need — an id, a kind, a status — and the
//      card looks the full record up by id from the query cache. Passing whole backend
//      objects through GeoJSON would duplicate the payload and couple the map to the
//      wire shape.
//   2. "Can this be placed?" is answered once, here, by whether a `point` exists. A
//      marker therefore always has coordinates; an item that cannot be placed never
//      becomes one (it stays in the list — §8, §9).
import type {
  GeoStatus,
  LocationPrecision,
  OpportunityType,
  RemoteScope,
  RemoteSelection,
  WorkplaceMode,
} from '~/types/v2'

/** The two things the two geo reads place on the map. */
export type MapEntityKind = 'opportunity' | 'company'

/** One opportunity as a marker. Always has coordinates — an unplaceable one is not here. */
export interface MapOpportunityMarker {
  kind: 'opportunity'
  id: string
  title: string
  companyName: string
  companyId: string | null
  longitude: number
  latitude: number
  status: GeoStatus
  /**
   * True only for `COMPANY_FALLBACK`: the pin is the employer's office, not the role's
   * own address, and the UI must say so rather than imply an exact location (§7).
   */
  approximate: boolean
  precision: LocationPrecision
  remoteScope: RemoteScope | null
  distanceMeters: number | null
}

/** One employer as a marker. */
export interface MapCompanyMarker {
  kind: 'company'
  id: string
  name: string
  longitude: number
  latitude: number
  status: GeoStatus
  /** The office is known no more precisely than a city/region — the card should hedge. */
  approximate: boolean
  precision: LocationPrecision
  isHeadquarters: boolean
  distanceMeters: number | null
}

export type MapMarker = MapOpportunityMarker | MapCompanyMarker

/**
 * The properties one GeoJSON feature carries into MapLibre. Only what the source needs:
 * `id` for a click to resolve back to a record, `status` to colour a point, `approximate`
 * to mark the fallback pins. Everything else stays in the query cache.
 */
export interface MapFeatureProperties {
  id: string
  kind: MapEntityKind
  status: GeoStatus
  approximate: boolean
}

/**
 * The filter form the controls bind to, as the UI holds it — not what the API takes.
 *
 * Empty string is "no restriction" in the selects (an unselected `<option value="">`),
 * turned into an absent query param by the page (useGeoExplorer.ts). The radius is two
 * fields: a toggle and a distance, because a radius is only sent when the user asks for
 * one, and when they do it is centred on the current view rather than a typed coordinate.
 */
export interface MapFilterForm {
  opportunityType: OpportunityType | ''
  workplaceMode: WorkplaceMode | ''
  /** ISO-3166 alpha-2, or '' for any. Uppercased when it becomes a `country` param. */
  country: string
  remote: RemoteSelection
  radiusEnabled: boolean
  radiusKm: number
}

/** The form a fresh map opens with: no restrictions, remote excluded, radius off. */
export function defaultMapFilterForm(): MapFilterForm {
  return {
    opportunityType: '',
    workplaceMode: '',
    country: '',
    remote: 'exclude',
    radiusEnabled: false,
    radiusKm: 25,
  }
}
