// The words the map shows for a distance, a status and a remote scope.
//
// Two of these encode phase rules that a template would bury: a null distance is "—" and
// never "0 km" (§8), and a company fallback says it is approximate rather than implying the
// role's own address (§7). They are unit-tested because that is where the rule lives.
import { describe, expect, it } from 'vitest'
import { distanceLabel, remoteLabel, statusLabel } from '~/utils/geo-format'

describe('distanceLabel', () => {
  it('is a dash for null, never a zero', () => {
    expect(distanceLabel(null)).toBe('—')
  })

  it('reads in metres below a kilometre', () => {
    expect(distanceLabel(0)).toBe('0 m')
    expect(distanceLabel(450)).toBe('450 m')
    expect(distanceLabel(999)).toBe('999 m')
  })

  it('reads with one decimal below ten kilometres, whole above', () => {
    expect(distanceLabel(1500)).toBe('1.5 km')
    expect(distanceLabel(9900)).toBe('9.9 km')
    expect(distanceLabel(12300)).toBe('12 km')
  })
})

describe('statusLabel', () => {
  it('names each geo status', () => {
    expect(statusLabel('RESOLVED')).toBe('Located')
    expect(statusLabel('REMOTE')).toBe('Remote')
    expect(statusLabel('UNRESOLVED')).toBe('Location unknown')
  })

  // The pin sits on the employer's office; the words must not claim the role's address.
  it('says a company fallback is approximate', () => {
    expect(statusLabel('COMPANY_FALLBACK')).toBe('Approximate — company office')
  })
})

describe('remoteLabel', () => {
  it('is null when the role names no remote policy', () => {
    expect(remoteLabel(null)).toBeNull()
  })

  it('phrases each scope', () => {
    expect(remoteLabel('REMOTE_ANYWHERE')).toBe('Remote — anywhere')
    expect(remoteLabel('REMOTE_COUNTRY_RESTRICTED')).toBe('Remote — within country')
    expect(remoteLabel('REMOTE_REGION_RESTRICTED')).toBe('Remote — within region')
    expect(remoteLabel('HYBRID')).toBe('Hybrid')
  })
})
