// The geo request strings and their cache keys, tested where a bug would be invisible on a
// map that still looks populated.
//
// Two rules carry the phase brief:
//
//   * **Array params are repeated, not joined.** `radius`, `country`, `remote_country`,
//     `opportunity_type`, `workplace_mode` each appear once per value — that is what
//     FastAPI's `Query(list)` reads (§24). Joining them with commas would send one value
//     the backend then treats as a single malformed code, and the map would quietly filter
//     to nothing. `bounds` and `remote`, by contrast, are single.
//   * **The cache key is the request string.** Two filter sets are two answers; one shared
//     entry would serve whichever landed last. `geoQuery` is exported so the key and the
//     URL are provably the same string rather than one inferred from the other.
//
// Nothing here filters by geography — PostGIS already did (§34); these only shape requests.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import {
  GEO_PAGE_SIZE,
  geoQuery,
  savedGeoQuery,
  useGeoCompaniesQuery,
  useGeoOpportunitiesQuery,
  useSavedSearchOpportunitiesQuery,
} from '~/composables/useGeoExplorer'
import { keysMatching } from '~/composables/useApiQuery'
import { stubFetch } from '../support/http'
import { companyGeoResponse, opportunityGeoResponse } from '../support/v2-fixtures'
import type { GeoFilters } from '~/composables/useGeoExplorer'

const PROFILE_ID = '33333333-3333-4333-8333-333333333333'
const OPPS = '/api/v2/geo/opportunities'
const COMPANIES = '/api/v2/geo/companies'
const SAVED = `/api/v2/me/search-profiles/${PROFILE_ID}/opportunities`

const ROUTES = [
  { match: SAVED, json: opportunityGeoResponse() },
  { match: OPPS, json: opportunityGeoResponse() },
  { match: COMPANIES, json: companyGeoResponse() },
]

describe('geoQuery', () => {
  it('sends only the window when nothing is filtered', () => {
    expect(geoQuery({})).toBe(`limit=${GEO_PAGE_SIZE}&offset=0`)
  })

  it('repeats each array param once per value, in list order', () => {
    const params = new URLSearchParams(geoQuery({
      radii: ['46.5,6.6,25', '47.4,8.5,10'],
      countries: ['CH', 'FR'],
      remoteCountries: ['CH'],
      opportunityTypes: ['FULL_TIME', 'PART_TIME'],
      workplaceModes: ['ON_SITE'],
    }))
    expect(params.getAll('radius')).toEqual(['46.5,6.6,25', '47.4,8.5,10'])
    expect(params.getAll('country')).toEqual(['CH', 'FR'])
    expect(params.getAll('remote_country')).toEqual(['CH'])
    expect(params.getAll('opportunity_type')).toEqual(['FULL_TIME', 'PART_TIME'])
    expect(params.getAll('workplace_mode')).toEqual(['ON_SITE'])
  })

  it('keeps bounds and remote single-valued', () => {
    const params = new URLSearchParams(geoQuery({ bounds: '47,46,8,6', remote: 'only' }))
    expect(params.getAll('bounds')).toEqual(['47,46,8,6'])
    expect(params.get('remote')).toBe('only')
  })

  it('omits an absent bounds and an absent remote rather than sending them empty', () => {
    const query = geoQuery({ bounds: null })
    expect(query).not.toContain('bounds')
    expect(query).not.toContain('remote')
  })

  it('carries a custom window through', () => {
    expect(geoQuery({ limit: 200, offset: 50 })).toBe('limit=200&offset=50')
  })
})

describe('savedGeoQuery', () => {
  it('is a viewport and a window, and nothing else — the scope is the saved search\'s', () => {
    expect(savedGeoQuery({})).toBe(`limit=${GEO_PAGE_SIZE}&offset=0`)
    expect(savedGeoQuery({ bounds: '47,46,8,6' })).toContain('bounds=47%2C46%2C8%2C6')
  })
})

/** A component that holds one geo query open, like the page would. */
function holder(run: () => { data: { value: unknown } }) {
  return defineComponent({
    setup() {
      const { data } = run()
      return () => h('output', String((data.value as { opportunities?: unknown[], companies?: unknown[] } | null)
        ?.opportunities?.length ?? (data.value as { companies?: unknown[] } | null)?.companies?.length ?? ''))
    },
  })
}

describe('useGeoOpportunitiesQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('requests the filtered page and keys the cache by the very same query', async () => {
    const http = stubFetch(ROUTES)
    const filters: GeoFilters = { opportunityTypes: ['FULL_TIME'], remote: 'include' }
    const query = geoQuery(filters)
    const wrapper = await mountSuspended(holder(() => useGeoOpportunitiesQuery(filters)))
    await flushPromises()

    const call = http.callsTo(OPPS)[0]
    expect(call?.url).toContain(`?${query}`)
    // The exported query is the key's tail, so the two cannot drift apart.
    expect(keysMatching('geo')).toContain(`geo:opportunities:${query}`)

    wrapper.unmount()
  })

  it('stays silent while disabled — a hidden mode makes no request', async () => {
    const http = stubFetch(ROUTES)
    const wrapper = await mountSuspended(
      holder(() => useGeoOpportunitiesQuery({}, { enabled: false })),
    )
    await flushPromises()

    expect(http.callsTo(OPPS)).toHaveLength(0)
    wrapper.unmount()
  })
})

describe('useGeoCompaniesQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('requests employers and keys them apart from opportunities', async () => {
    const http = stubFetch(ROUTES)
    const query = geoQuery({})
    const wrapper = await mountSuspended(holder(() => useGeoCompaniesQuery({})))
    await flushPromises()

    expect(http.callsTo(COMPANIES)).toHaveLength(1)
    expect(keysMatching('geo')).toContain(`geo:companies:${query}`)
    wrapper.unmount()
  })

  it('stays silent while disabled', async () => {
    const http = stubFetch(ROUTES)
    const wrapper = await mountSuspended(
      holder(() => useGeoCompaniesQuery({}, { enabled: false })),
    )
    await flushPromises()

    expect(http.callsTo(COMPANIES)).toHaveLength(0)
    wrapper.unmount()
  })
})

describe('useSavedSearchOpportunitiesQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('sends nothing while the saved-search id is unknown', async () => {
    const http = stubFetch(ROUTES)
    const wrapper = await mountSuspended(
      holder(() => useSavedSearchOpportunitiesQuery(null)),
    )
    await flushPromises()

    expect(http.calls).toHaveLength(0)
    wrapper.unmount()
  })

  it('runs the saved search on the backend, never reimplementing its logic', async () => {
    const http = stubFetch(ROUTES)
    const wrapper = await mountSuspended(
      holder(() => useSavedSearchOpportunitiesQuery(PROFILE_ID, { bounds: '47,46,8,6' })),
    )
    await flushPromises()

    const call = http.callsTo(SAVED)[0]
    expect(call?.url).toContain('bounds=47%2C46%2C8%2C6')
    // The owner is never in the path — it is the session's (docs/AUTHENTICATION.md).
    expect(call?.url).not.toContain('user')
    wrapper.unmount()
  })

  it('holds both gates: a real id but a hidden view makes no request', async () => {
    const http = stubFetch(ROUTES)
    const wrapper = await mountSuspended(
      holder(() => useSavedSearchOpportunitiesQuery(PROFILE_ID, {}, { enabled: false })),
    )
    await flushPromises()

    expect(http.callsTo(SAVED)).toHaveLength(0)
    wrapper.unmount()
  })

  // A stale id in a shared URL is an ordinary screen to render, not a failure.
  it('maps a missing saved search to null rather than throwing', async () => {
    const http = stubFetch([{
      match: SAVED,
      status: 404,
      json: { error: 'search_profile_not_found', detail: 'no such saved search' },
    }])
    let seen: unknown = 'unset'
    const probe = defineComponent({
      setup() {
        const { data, error } = useSavedSearchOpportunitiesQuery(PROFILE_ID)
        return () => {
          seen = { data: data.value, error: error.value }
          return h('output')
        }
      },
    })
    const wrapper = await mountSuspended(probe)
    await flushPromises()

    expect(http.callsTo(SAVED)).toHaveLength(1)
    // Null is the payload the not-found is mapped to, and no error is surfaced with it.
    expect((seen as { data: unknown }).data).toBeNull()
    expect((seen as { error: unknown }).error).toBeFalsy()
    wrapper.unmount()
  })
})
