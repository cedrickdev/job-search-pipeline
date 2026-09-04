// Small display formatters shared by the Overview and Analytics pages, which in
// V1 each carried their own copy of `asPct`.
//
// Both keep V1's exact output, em dash included: a missing KPI reads "—" rather
// than "0%", because "no data yet" and "nobody replied" are different facts.

/** A 0–1 fraction as a whole-percent string. */
export function asPct(fraction: number | null | undefined): string {
  return fraction === null || fraction === undefined ? '—' : `${Math.round(fraction * 100)}%`
}

/** An already-percentage value (0–100), as used by phone-screen readiness. */
export function asScore(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${Math.round(value)}%`
}
