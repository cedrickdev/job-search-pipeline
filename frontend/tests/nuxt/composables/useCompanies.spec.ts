// The company query string, tested where a bug would be invisible on screen.
//
// One case here is load-bearing: `has_opportunities=false`. "Employers with nothing
// posted" is the question the whole phase exists to make askable, and the natural
// `if (filters.has_opportunities)` drops exactly that value — leaving a page that
// looks like it filtered and quietly did not. So the assertion is on the emitted
// query string rather than on the rendered list, because that is where the value
// disappears.
//
// The rest pins the cache key. Two different filter sets must not share one entry
// (a page of Greenhouse employers is not a page of every employer), and every
// company key must start with `companies:` or the one `invalidate('companies')`
// after a discovery pass reaches nothing.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, ref } from 'vue'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import {
  COMPANY_PAGE_SIZE,
  companyQuery,
  useCompaniesQuery,
  useCompanyQuery,
} from '~/composables/useCompanies'
import { keysMatching } from '~/composables/useApiQuery'
import { stubFetch } from '../support/http'
import { companyDetail, companyList } from '../support/v2-fixtures'
import type { CompanyFilters } from '~/composables/useCompanies'

const ROUTES = [
  { match: '/api/v2/companies/', json: companyDetail() },
  { match: '/api/v2/companies', json: companyList() },
]

/** A component that holds one companies query open, like a page would. */
function listHolder(filters: CompanyFilters) {
  return defineComponent({
    setup() {
      const { data } = useCompaniesQuery(filters)
      return () => h('output', String(data.value?.total ?? ''))
    },
  })
}

describe('companyQuery', () => {
  it('sends only the filters that were set, with the window always', () => {
    expect(companyQuery({})).toBe(`limit=${COMPANY_PAGE_SIZE}&offset=0`)
  })

  // The one that matters: `false` is a filter, not an absent one.
  it('keeps has_opportunities=false, which is the point of the directory', () => {
    expect(companyQuery({ has_opportunities: false }))
      .toBe('has_opportunities=false&limit=20&offset=0')
    expect(companyQuery({ has_opportunities: true }))
      .toBe('has_opportunities=true&limit=20&offset=0')
    // Null and undefined are "do not restrict", and must not appear at all.
    expect(companyQuery({ has_opportunities: null })).not.toContain('has_opportunities')
    expect(companyQuery({})).not.toContain('has_opportunities')
  })

  it('carries every filter the endpoint accepts', () => {
    const query = companyQuery({
      text: 'Logitech',
      country: 'CH',
      ats_platform: 'GREENHOUSE',
      spontaneous_support: 'UNKNOWN',
      has_opportunities: false,
      limit: 50,
      offset: 100,
    })

    expect(Object.fromEntries(new URLSearchParams(query))).toEqual({
      text: 'Logitech',
      country: 'CH',
      ats_platform: 'GREENHOUSE',
      spontaneous_support: 'UNKNOWN',
      has_opportunities: 'false',
      limit: '50',
      offset: '100',
    })
  })

  it('leaves an empty text or country out rather than sending an empty value', () => {
    expect(companyQuery({ text: '', country: '' })).toBe('limit=20&offset=0')
  })
})

describe('useCompaniesQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('requests the filtered page and keys the cache by the same query', async () => {
    const http = stubFetch(ROUTES)
    const wrapper = await mountSuspended(listHolder({ has_opportunities: false }))
    await flushPromises()

    expect(wrapper.text()).toBe('1')
    expect(http.callsTo('/api/v2/companies')).toHaveLength(1)
    expect(http.callsTo('/api/v2/companies')[0]!.url)
      .toContain('has_opportunities=false')
    // `companies:` prefix, so one `invalidate('companies')` after a pass reaches it.
    expect(keysMatching('companies'))
      .toContain('companies:list:has_opportunities=false&limit=20&offset=0')

    wrapper.unmount()
  })

  it('gives two filter sets two cache entries', async () => {
    stubFetch(ROUTES)
    const ch = await mountSuspended(listHolder({ country: 'CH' }))
    const fr = await mountSuspended(listHolder({ country: 'FR' }))
    await flushPromises()

    // Both, distinctly: one shared entry would serve whichever page landed last.
    // Asserted by membership rather than by count, because a wrapper mounted in an
    // earlier case stays registered until the file ends.
    expect(keysMatching('companies')).toContain('companies:list:country=CH&limit=20&offset=0')
    expect(keysMatching('companies')).toContain('companies:list:country=FR&limit=20&offset=0')

    ch.unmount()
    fr.unmount()
  })

  it('refetches when a reactive filter changes', async () => {
    const http = stubFetch(ROUTES)
    const offset = ref(0)
    const holder = defineComponent({
      setup() {
        const { data } = useCompaniesQuery(() => ({ offset: offset.value }))
        return () => h('output', String(data.value?.total ?? ''))
      },
    })
    await mountSuspended(holder)
    await flushPromises()

    offset.value = COMPANY_PAGE_SIZE
    await flushPromises()

    const urls = http.callsTo('/api/v2/companies').map(call => call.url)
    expect(urls.some(url => url.includes('offset=0'))).toBe(true)
    expect(urls.some(url => url.includes(`offset=${COMPANY_PAGE_SIZE}`))).toBe(true)
  })
})

describe('useCompanyQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  /**
   * The guard, asserted from the wire.
   *
   * A detail page reads its id from the route, and a request for
   * `/companies/undefined` would be a 422 the user sees as a broken screen. `enabled`
   * is what keeps that request from leaving at all.
   */
  it('sends nothing while the id is unknown', async () => {
    const http = stubFetch(ROUTES)
    const holder = defineComponent({
      setup() {
        const { data } = useCompanyQuery(null)
        return () => h('output', String(data.value?.company.name ?? ''))
      },
    })
    await mountSuspended(holder)
    await flushPromises()

    expect(http.calls).toHaveLength(0)
  })

  it('fetches the one company once the id is known', async () => {
    const http = stubFetch(ROUTES)
    const holder = defineComponent({
      setup() {
        const { data } = useCompanyQuery('44444444-4444-4444-8444-444444444444')
        return () => h('output', String(data.value?.company.name ?? ''))
      },
    })
    const wrapper = await mountSuspended(holder)
    await flushPromises()

    expect(wrapper.text()).toBe('Logitech')
    expect(http.callsTo('/api/v2/companies/44444444-4444-4444-8444-444444444444'))
      .toHaveLength(1)
  })
})
