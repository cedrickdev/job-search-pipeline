// The page is the wiring: which read runs for which mode, the one-selection sync, and the
// explicit "search this area". MapLibre is never touched here — OpportunityMap is replaced
// with a stub that emits the same events a real map would (a click per feature, a settled
// move), so the test drives the page through its real seams with no WebGL (§51). Every
// request is stubbed; nothing reaches a live board (§52).
//
// The cases carry the phase's rules: the right endpoint per mode and per saved-search scope
// (§15), an unplaceable role still in the list (§8), selection shared between map and list
// (§12), and a pan that never re-queries until the button is pressed (§20).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import MapPage from '~/pages/map.vue'
import { useSessionStore } from '~/stores/session'
import { useMapExplorerStore } from '~/stores/mapExplorer'
import { stubFetch } from '../support/http'
import {
  companyGeoItem,
  companyGeoResponse,
  geoLocation,
  opportunityGeoItem,
  opportunityGeoResponse,
  signedIn,
} from '../support/v2-fixtures'
import type { FetchStub, Route } from '../support/http'
import type { VueWrapper } from '@vue/test-utils'

// The one component that owns a live map, replaced by a stub with the same event surface.
// It renders a button per feature (a marker click) and one that fires a settled move, and
// exposes fitTo/resize because the page calls them through its `mapRef`.
vi.mock('~/components/map/OpportunityMap.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      name: 'OpportunityMapStub',
      props: {
        features: { type: Object, required: true },
        radiusCircles: { type: Array, default: () => [] },
        selectedId: { type: String, default: null },
        center: { type: Array, required: true },
        zoom: { type: Number, required: true },
        styleUrl: { type: String, default: '' },
        attribution: { type: String, default: '' },
      },
      emits: ['select', 'moveend', 'ready', 'error'],
      setup(props, { emit, expose }) {
        expose({ fitTo: () => {}, resize: () => {} })
        return () => h('div', { 'data-testid': 'map-stub' }, [
          ...(props.features as { features: { properties: { id: string } }[] }).features.map(feature =>
            h('button', {
              'data-map-feature': feature.properties.id,
              'onClick': () => emit('select', feature.properties.id),
            }, feature.properties.id)),
          h('button', {
            'data-testid': 'fire-moveend',
            'onClick': () => emit('moveend', { center: [7, 47], zoom: 10, bounds: '48,46,9,6' }),
          }, 'move'),
        ])
      },
    }),
  }
})

const PROFILE_ID = '33333333-3333-4333-8333-333333333333'
const SESSION = '/api/v2/auth/session'
const OPPS = '/api/v2/geo/opportunities'
const COMPANIES = '/api/v2/geo/companies'
const SAVED = `/api/v2/me/search-profiles/${PROFILE_ID}/opportunities`

function routes(extra: Route[] = []): Route[] {
  return [
    ...extra,
    { match: SAVED, method: 'GET', json: opportunityGeoResponse() },
    { match: OPPS, method: 'GET', json: opportunityGeoResponse() },
    { match: COMPANIES, method: 'GET', json: companyGeoResponse() },
    { match: SESSION, method: 'GET', json: signedIn() },
  ]
}

function rowTexts(wrapper: VueWrapper): string[] {
  return wrapper.findAll('.map-row').map(row => row.text())
}

/** The URL of the most recent request to a geo endpoint. */
function lastUrl(http: FetchStub, fragment: string): string {
  const call = http.callsTo(fragment).at(-1)
  expect(call, `no request to ${fragment}`).toBeTruthy()
  return call!.url
}

describe('map page', () => {
  // The page seeds a shared (Pinia) store on mount; a page left mounted from an earlier test
  // would react to the next test's store writes and fire ghost reads. Unmount after each.
  const mounted: VueWrapper[] = []

  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    useSessionStore().$reset()
    useMapExplorerStore().$reset()
  })

  afterEach(() => {
    mounted.forEach(wrapper => wrapper.unmount())
    mounted.length = 0
  })

  /** Mount as the auth guard leaves it: the session already resolved. */
  async function mountMap(routeTable: Route[], path = '/map') {
    const http = stubFetch(routeTable)
    await useSessionStore().ensure()
    const wrapper = await mountSuspended(MapPage, { route: path })
    mounted.push(wrapper)
    await flushPromises()
    return { http, wrapper }
  }

  it('opens on opportunities: reads the open endpoint and lists what came back', async () => {
    const { http, wrapper } = await mountMap(routes([{
      match: OPPS,
      method: 'GET',
      json: opportunityGeoResponse([opportunityGeoItem({ id: 'a', title: 'Backend Engineer' })]),
    }]))

    expect(http.callsTo(OPPS)).toHaveLength(1)
    // No saved-search read fires without a profile in the URL.
    expect(http.callsTo('/search-profiles/')).toHaveLength(0)
    expect(rowTexts(wrapper).join(' ')).toContain('Backend Engineer')
  })

  // §8/§9 from the page's side: the map cannot place these, but the list is the full answer.
  it('keeps an unplaceable role in the list, tagged, with no pin', async () => {
    const { wrapper } = await mountMap(routes([{
      match: OPPS,
      method: 'GET',
      json: opportunityGeoResponse([
        opportunityGeoItem({ id: 'placed', title: 'On-site Role' }),
        opportunityGeoItem({
          id: 'remote',
          title: 'Remote Role',
          status: 'REMOTE',
          location: null,
          remote_scope: 'REMOTE_ANYWHERE',
          distance_meters: null,
        }),
      ]),
    }]))

    // Both rows in the list…
    expect(rowTexts(wrapper)).toHaveLength(2)
    expect(wrapper.get('[data-id="remote"]').text()).toContain('No location')
    // …but only the placeable one is a marker (a stub feature button).
    expect(wrapper.findAll('[data-map-feature]').map(b => b.attributes('data-map-feature')))
      .toEqual(['placed'])
  })

  it('shares one selection between the map and the list (§12)', async () => {
    const { wrapper } = await mountMap(routes([{
      match: OPPS,
      method: 'GET',
      json: opportunityGeoResponse([opportunityGeoItem({ id: 'a', title: 'Backend Engineer' })]),
    }]))

    // A marker click on the (stubbed) map…
    await wrapper.get('[data-map-feature="a"]').trigger('click')
    await flushPromises()

    // …opens the card and marks the list row — one store field drives both.
    expect(wrapper.get('[data-testid="map-card"]').text()).toContain('Backend Engineer')
    expect(wrapper.get('[data-id="a"]').attributes('aria-current')).toBe('true')
  })

  it('switches to employers and reads the companies endpoint', async () => {
    const { http, wrapper } = await mountMap(routes([{
      match: COMPANIES,
      method: 'GET',
      json: companyGeoResponse([companyGeoItem()]),
    }]))

    await wrapper.findAll('[role="tab"]').find(t => t.text() === 'Companies')!.trigger('click')
    await flushPromises()

    expect(http.callsTo(COMPANIES).length).toBeGreaterThanOrEqual(1)
    expect(rowTexts(wrapper).join(' ')).toContain('Logitech')
  })

  it('scopes to a saved search when the URL names a profile, and only then (§15)', async () => {
    const { http, wrapper } = await mountMap(routes([{
      match: SAVED,
      method: 'GET',
      json: opportunityGeoResponse([opportunityGeoItem({ id: 's', title: 'Saved Role' })]),
    }]), `/map?profile=${PROFILE_ID}`)

    expect(http.callsTo(SAVED)).toHaveLength(1)
    // The open read is silenced while a saved search scopes the map.
    expect(http.callsTo(OPPS)).toHaveLength(0)
    expect(wrapper.text()).toContain('Scoped to a saved search')
    expect(rowTexts(wrapper).join(' ')).toContain('Saved Role')
  })

  // §20: a pan never re-queries; the explicit button sends the current bounds.
  it('only searches the panned area when the button is pressed', async () => {
    const { http, wrapper } = await mountMap(routes())
    const before = http.callsTo(OPPS).length

    // The map settles on a new view — this alone must not fetch.
    await wrapper.get('[data-testid="fire-moveend"]').trigger('click')
    await flushPromises()
    expect(http.callsTo(OPPS)).toHaveLength(before)

    // Pressing "Search this area" sends the bounds the move reported.
    await wrapper.get('.map-search-area').trigger('click')
    await flushPromises()

    expect(http.callsTo(OPPS).length).toBeGreaterThan(before)
    expect(lastUrl(http, OPPS)).toContain('bounds=48%2C46%2C9%2C6')
  })

  it('says so when the view holds nothing (§28)', async () => {
    const { wrapper } = await mountMap(routes([{
      match: OPPS,
      method: 'GET',
      json: opportunityGeoResponse([]),
    }]))

    expect(wrapper.get('.ov-empty').text()).toContain('No results in this view')
  })

  it('shows an error without losing the list column (§30)', async () => {
    const { wrapper } = await mountMap(routes([{
      match: OPPS,
      method: 'GET',
      status: 503,
      json: { error: 'database_unavailable', detail: 'no database' },
    }]))

    expect(wrapper.find('.ov-error').exists()).toBe(true)
    // The list section is still on the page — the error is in it, not instead of it.
    expect(wrapper.find('.map-col--list').exists()).toBe(true)
  })
})

describe('map page · geocoded fixture sanity', () => {
  it('places a city-precise company office (fixture wiring)', () => {
    // Guards the fixture the page relies on: a company location carries a point.
    expect(companyGeoItem({ location: geoLocation({ precision: 'CITY' }) }).location.point).not.toBeNull()
  })
})
