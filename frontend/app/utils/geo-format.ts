// Turning geo values into the few words the list, the cards and the legend show.
//
// One place for it so the same distance reads the same everywhere, and so the rules the
// phase brief is strict about live in testable functions rather than in templates:
//
//   * a distance of `null` is "—", never "0 km". A remote or unplaced row has no distance,
//     and a zero would read as "at your doorstep" (§8).
//   * `COMPANY_FALLBACK` says "approximate — company office", so a pin at the employer's
//     HQ is never mistaken for the role's own address (§7).
import type { GeoStatus, RemoteScope } from '~/types/v2'

/**
 * A distance as few words: metres below a kilometre, one decimal below 10 km, whole
 * kilometres above. `null` — a remote or unplaced row — is "—", never a number.
 */
export function distanceLabel(meters: number | null): string {
  if (meters === null) return '—'
  if (meters < 1000) return `${Math.round(meters)} m`
  const km = meters / 1000
  return `${km < 10 ? km.toFixed(1) : Math.round(km)} km`
}

const STATUS_LABELS: Record<GeoStatus, string> = {
  RESOLVED: 'Located',
  COMPANY_FALLBACK: 'Approximate — company office',
  REMOTE: 'Remote',
  UNRESOLVED: 'Location unknown',
}

/** The status as a phrase for a badge or a legend row. */
export function statusLabel(status: GeoStatus): string {
  return STATUS_LABELS[status]
}

const REMOTE_LABELS: Record<RemoteScope, string> = {
  REMOTE_ANYWHERE: 'Remote — anywhere',
  REMOTE_COUNTRY_RESTRICTED: 'Remote — within country',
  REMOTE_REGION_RESTRICTED: 'Remote — within region',
  HYBRID: 'Hybrid',
}

/** The remote scope as a phrase, or `null` when the role names no remote policy. */
export function remoteLabel(scope: RemoteScope | null): string | null {
  return scope ? REMOTE_LABELS[scope] : null
}
