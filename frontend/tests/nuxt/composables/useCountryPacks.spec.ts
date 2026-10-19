import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import { useCountryPacksQuery } from '~/composables/useCountryPacks'
import { stubFetch } from '../support/http'

const SWISS_PACKS = {
  packs: [{
    country: 'CH',
    display_name: 'Switzerland',
    default_locale: 'fr-CH',
    locales: ['fr-CH', 'de-CH', 'it-CH', 'en-CH'],
    currency: 'CHF',
    languages: ['fr', 'de', 'it', 'en'],
    timezone: 'Europe/Zurich',
    map_viewport: {
      center: { latitude: 46.8182, longitude: 8.2275 },
      zoom: 6.2,
      bounds: { north: 47.8085, south: 45.8179, east: 10.4921, west: 5.9559 },
    },
    opportunity_types: ['FULL_TIME', 'INTERNSHIP'],
    enabled_source_count: 11,
  }],
}

function holder() {
  return defineComponent({
    setup() {
      const { data } = useCountryPacksQuery()
      return () => h('output', data.value?.packs.map(pack => pack.country).join(',') ?? '')
    },
  })
}

describe('useCountryPacksQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('loads the authenticated deployment catalogue once', async () => {
    const http = stubFetch([{ match: '/api/v2/country-packs', json: SWISS_PACKS }])
    const wrapper = await mountSuspended(holder())
    await flushPromises()

    expect(wrapper.text()).toBe('CH')
    expect(http.callsTo('/api/v2/country-packs')).toHaveLength(1)
    wrapper.unmount()
  })
})
