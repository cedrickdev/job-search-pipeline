import { describe, expect, it } from 'vitest'
import { applicationEventDetail, applicationReason } from '~/utils/application-event-format'

describe('application audit localization', () => {
  it('translates the known manual hand-off without hiding its reason', () => {
    expect(applicationEventDetail('this channel has no automated path; a human must apply', 'fr'))
      .toBe('Ce canal ne permet pas l’envoi automatique ; vous devez candidater vous-même.')
    expect(applicationReason('UNSUPPORTED_CHANNEL', 'fr'))
      .toBe('Canal de candidature non automatisé')
    expect(applicationEventDetail('UNSUPPORTED_CHANNEL', 'fr'))
      .toBe('Canal de candidature non automatisé')
  })

  it('renders an opened target in French and preserves its identifier', () => {
    const id = '0770bc82-c03f-51ba-957b-868b1e7db2e2'
    expect(applicationEventDetail(`opened for opportunity:${id} via MANUAL`, 'fr'))
      .toBe(`Candidature ouverte pour l’offre ${id} par voie manuelle`)
  })

  it('preserves unknown audit details and English originals', () => {
    expect(applicationEventDetail('unrecognized provider detail', 'fr'))
      .toBe('unrecognized provider detail')
    expect(applicationEventDetail('this channel has no automated path; a human must apply', 'en'))
      .toBe('this channel has no automated path; a human must apply')
    expect(applicationReason('CUSTOM_REASON', 'fr')).toBe('CUSTOM_REASON')
  })
})
