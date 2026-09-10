// The company screens: the directory and one employer.
//
// Two cases carry a Phase 6 requirement rather than a preference.
//
// **An employer with nothing posted is still on the screen.** That is the acceptance
// criterion the phase was written around (§28), and it is asserted here the way a
// user meets it: pick "Without open roles", and the row is there with the request
// that fetched it carrying `has_opportunities=false`.
//
// **`UNKNOWN` is rendered as unknown.** Not as "no". A verdict nobody has formed and
// an employer that refuses unsolicited applications are different facts, and only the
// second is a reason not to write; a screen that collapsed them would be inventing a
// refusal no source ever gave.
//
// The detail page is asserted for what it must *not* show as much as for what it
// does: the API has no field for a provider's raw metadata, so the page cannot leak
// one, and this pins that the rendering never grows a passthrough of its own.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import CompaniesPage from '~/pages/companies/index.vue'
import CompanyPage from '~/pages/companies/[id].vue'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import {
  company,
  companyDetail,
  companyList,
  discoveryRun,
  signedIn,
} from '../support/v2-fixtures'
import type { FetchStub, Route } from '../support/http'
import type { Company, CompanyDetail, CompanyDiscoveryRun } from '~/types/v2'
import type { VueWrapper } from '@vue/test-utils'

const COMPANY_ID = '44444444-4444-4444-8444-444444444444'
const LIST = '/api/v2/companies?'
const RUN = '/api/v2/company-discovery/run'

/**
 * The routes both screens need.
 *
 * `extra` comes first because `stubFetch` answers from the first match, and the
 * detail route precedes the list route for the same reason: `/companies/{id}` also
 * contains `/companies`.
 *
 * The session route is here because both pages are guarded: `middleware: 'auth'` runs
 * on the navigation `mountSuspended` performs, and an anonymous answer would leave
 * the router at /login with no `id` for the detail page to read.
 */
function routes(extra: Route[] = [], detail: CompanyDetail = companyDetail(),
                list = companyList()): Route[] {
  return [
    ...extra,
    { match: RUN, method: 'POST', json: discoveryRun() },
    { match: `/api/v2/companies/${COMPANY_ID}`, method: 'GET', json: detail },
    { match: '/api/v2/companies', method: 'GET', json: list },
    { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
  ]
}

function rows(wrapper: VueWrapper): string[] {
  return wrapper.findAll('.acct-search-name').map(row => row.text())
}

/** The URL of the most recent list request. */
function lastListUrl(http: FetchStub): string {
  const call = http.callsTo(LIST).at(-1)
  expect(call, 'no list request was made').toBeTruthy()
  return call!.url
}

/** Every offset the page has asked the server for, in order. */
function offsets(http: FetchStub): string[] {
  return http.callsTo(LIST)
    .map(call => new URLSearchParams(call.url.split('?')[1]).get('offset') ?? '')
}

/** Mount as the guard leaves it: the session already resolved. */
async function signIn(routeTable: Route[]) {
  const http = stubFetch(routeTable)
  await useSessionStore().ensure()
  return http
}

async function mountList(...args: Parameters<typeof routes>) {
  const http = await signIn(routes(...args))
  const wrapper = await mountSuspended(CompaniesPage, { route: '/companies' })
  await flushPromises()
  return { http, wrapper }
}

async function mountDetail(detail: CompanyDetail = companyDetail(), extra: Route[] = []) {
  const http = await signIn(routes(extra, detail))
  const wrapper = await mountSuspended(CompanyPage, { route: `/companies/${COMPANY_ID}` })
  await flushPromises()
  return { http, wrapper }
}

function buttonNamed(wrapper: VueWrapper, label: RegExp) {
  const found = wrapper.findAll('button').find(b => label.test(b.text()))
  if (!found) throw new Error(`no button matching ${label}`)
  return found
}

describe('companies page · the directory', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    // The store outlives a test — it is the app's, not the wrapper's.
    useSessionStore().$reset()
  })

  it('lists each employer with where it is and what is known about it', async () => {
    const detected: Company = company({
      name: 'Nestlé',
      identity_status: 'VERIFIED',
      detected_ats: {
        platform: 'GREENHOUSE',
        organization_id: 'nestle',
        status: 'CONFIRMED',
        detected_by: 'configured_ats',
        evidence: [],
      },
      locations: [{
        city: 'Vevey',
        region: null,
        postal_code: null,
        country: 'CH',
        raw: null,
        is_headquarters: true,
      }],
    })
    const { wrapper } = await mountList([], companyDetail(), companyList([detected]))

    expect(rows(wrapper)).toEqual(['Nestlé'])
    expect(wrapper.text()).toContain('Vevey, CH')
    expect(wrapper.text()).toContain('GREENHOUSE')
    expect(wrapper.text()).toContain('CONFIRMED')
    expect(wrapper.get('.acct-search-name').attributes('href'))
      .toBe(`/companies/${COMPANY_ID}`)
  })

  // The phase's own acceptance criterion, from the user's side of it.
  it('finds the employers with no open role at all', async () => {
    const quiet = companyList([company({ name: 'Quiet Employer SA' })], { total: 1 })
    const { http, wrapper } = await mountList([], companyDetail(), quiet)

    await wrapper.get('[aria-label="Open roles"]').setValue('false')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(lastListUrl(http)).toContain('has_opportunities=false')
    expect(rows(wrapper)).toEqual(['Quiet Employer SA'])
  })

  it('says unknown when nobody has decided about spontaneous applications', async () => {
    const { wrapper } = await mountList()

    // The fixture's channel is absent, which is the backend's "not decided".
    expect(wrapper.text()).toContain('spontaneous: unknown')
    expect(wrapper.text()).not.toContain('spontaneous: no')
  })

  it('does not turn a refusal into a shrug', async () => {
    const refuses = companyList([company({
      spontaneous_application: {
        support: 'NOT_SUPPORTED',
        url: null,
        observed_by: 'configured_ats',
        evidence: [],
      },
      accepts_spontaneous_applications: false,
    })])
    const { wrapper } = await mountList([], companyDetail(), refuses)

    expect(wrapper.text()).toContain('spontaneous: no')
    expect(wrapper.text()).not.toContain('spontaneous: unknown')
  })

  it('sends every filter the form offers', async () => {
    const { http, wrapper } = await mountList()

    await wrapper.get('input[type="search"]').setValue('logi')
    await wrapper.get('[aria-label="Country"]').setValue('ch')
    await wrapper.get('[aria-label="Platform"]').setValue('LEVER')
    await wrapper.get('[aria-label="Spontaneous"]').setValue('SUPPORTED')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    const query = new URLSearchParams(lastListUrl(http).split('?')[1])
    expect(query.get('text')).toBe('logi')
    // Upper-cased for the API's `^[A-Z]{2}$`, so a lower-case entry is not a 422.
    expect(query.get('country')).toBe('CH')
    expect(query.get('ats_platform')).toBe('LEVER')
    expect(query.get('spontaneous_support')).toBe('SUPPORTED')
  })

  it('pages forward and back, and stops at both ends', async () => {
    const many = companyList(
      Array.from({ length: 20 }, (_, index) => company({ name: `Employer ${index}` })),
      { total: 45 },
    )
    const { http, wrapper } = await mountList([], companyDetail(), many)

    expect(buttonNamed(wrapper, /Previous/).attributes('disabled')).toBeDefined()

    await buttonNamed(wrapper, /Next/).trigger('click')
    await flushPromises()
    expect(lastListUrl(http)).toContain('offset=20')

    await buttonNamed(wrapper, /Previous/).trigger('click')
    await flushPromises()

    // Back at the start, and the first page is served from the cache rather than
    // refetched — the request for offset 0 already happened on mount, and
    // `useApiQuery`'s staleness window is what makes a pager cheap.
    expect(offsets(http)).toEqual(['0', '20'])
    expect(buttonNamed(wrapper, /Previous/).attributes('disabled')).toBeDefined()
  })

  it('returns to the first page when the filters change', async () => {
    const many = companyList([company()], { total: 45 })
    const { http, wrapper } = await mountList([], companyDetail(), many)

    await buttonNamed(wrapper, /Next/).trigger('click')
    await flushPromises()
    await wrapper.get('input[type="search"]').setValue('logi')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    // A new filter makes the old window meaningless: offset 20 into a different
    // result set skips rows nobody has seen.
    expect(lastListUrl(http)).toContain('offset=0')
  })

  it('says so when nothing matches', async () => {
    const { wrapper } = await mountList([], companyDetail(), companyList([], { total: 0 }))

    expect(wrapper.get('.ov-empty').text()).toBe('No company matches these filters.')
  })
})

describe('companies page · running a discovery pass', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    useSessionStore().$reset()
  })

  it('posts the pass with no body of seeds, then reports what it did', async () => {
    const outcome: CompanyDiscoveryRun = discoveryRun({
      created: 3,
      matched: 7,
      ambiguous: 2,
      links: { examined: 12, linked: 9, ambiguous: 2, unresolved: 1 },
    })
    const { http, wrapper } = await mountList([{ match: RUN, method: 'POST', json: outcome }])

    await buttonNamed(wrapper, /Run discovery/).trigger('click')
    await flushPromises()

    const posts = http.callsTo(RUN)
    expect(posts).toHaveLength(1)
    // No seed reaches the server: a company is shared data, so a body that carried
    // one would let any account write into every other account's directory.
    expect(posts[0]!.init?.body).toBeUndefined()
    expect(wrapper.get('.settings-ok').text())
      .toContain('3 created, 7 matched, 2 left ambiguous, 9 postings linked')
  })

  // Ambiguity is an outcome, not a failure: it is what "we did not merge two
  // employers on a guess" looks like from the outside.
  it('reports an ambiguous pass as a result rather than an error', async () => {
    const { wrapper } = await mountList([{
      match: RUN,
      method: 'POST',
      json: discoveryRun({ ambiguous: 4 }),
    }])

    await buttonNamed(wrapper, /Run discovery/).trigger('click')
    await flushPromises()

    expect(wrapper.get('.settings-ok').text()).toContain('4 left ambiguous')
    expect(wrapper.find('.auth-error').exists()).toBe(false)
  })

  it('refreshes the list after a pass', async () => {
    const { http, wrapper } = await mountList()
    const before = http.callsTo(LIST).length

    await buttonNamed(wrapper, /Run discovery/).trigger('click')
    await flushPromises()

    expect(http.callsTo(LIST).length).toBeGreaterThan(before)
  })

  it('explains a refused pass in its own words', async () => {
    const { wrapper } = await mountList([{
      match: RUN,
      method: 'POST',
      status: 503,
      json: { error: 'database_unavailable', detail: 'no database' },
    }])

    await buttonNamed(wrapper, /Run discovery/).trigger('click')
    await flushPromises()

    expect(wrapper.get('.auth-error').text())
      .toBe('The service is temporarily unavailable. Try again in a moment.')
    expect(wrapper.find('.settings-ok').exists()).toBe(false)
  })
})

describe('company page · one employer', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    useSessionStore().$reset()
  })

  it('states the facts and where each one came from', async () => {
    const detail = companyDetail({
      company: company({
        careers_url: 'https://boards.greenhouse.invalid/logitech',
        detected_ats: {
          platform: 'GREENHOUSE',
          organization_id: 'logitech',
          status: 'CONFIRMED',
          detected_by: 'configured_ats',
          evidence: [{
            code: 'ATS_HOST_MATCH',
            detail: 'the careers URL is on the platform host',
            source_url: 'https://boards.greenhouse.invalid/logitech',
            observed_at: '2026-01-02T10:00:00Z',
          }],
        },
      }),
      aliases: [{
        alias: 'Logitech Europe S.A.',
        normalized_alias: 'logitech europe',
        source_key: 'stored_opportunities',
        first_seen_at: '2026-01-02T10:00:00Z',
        last_seen_at: '2026-01-03T10:00:00Z',
      }],
      career_sites: [{
        url: 'https://boards.greenhouse.invalid/logitech',
        kind: 'ATS_BOARD',
        platform: 'GREENHOUSE',
        source_key: 'configured_ats',
        verification_status: 'CONFIRMED',
        discovered_at: '2026-01-02T10:00:00Z',
        last_checked_at: null,
      }],
      discoveries: [{
        provider_key: 'configured_ats',
        external_id: 'greenhouse:logitech',
        seed_kind: 'ATS_ORGANIZATION',
        company_name: 'Logitech',
        source_url: 'https://boards.greenhouse.invalid/logitech',
        discovered_at: '2026-01-02T10:00:00Z',
        confidence: 'CONFIRMED',
      }],
      discovered_by: ['configured_ats'],
    })
    const { wrapper } = await mountDetail(detail)

    expect(wrapper.get('h1').text()).toBe('Logitech')
    // The claim and its status travel together (§10): "on Greenhouse" is worth
    // nothing on screen without how sure the backend is.
    expect(wrapper.text()).toContain('CONFIRMED')
    expect(wrapper.text()).toContain('ATS_HOST_MATCH')
    expect(wrapper.text()).toContain('the careers URL is on the platform host')
    expect(wrapper.text()).toContain('Logitech Europe S.A.')
    expect(wrapper.text()).toContain('ATS_BOARD')
    expect(wrapper.text()).toContain('greenhouse:logitech')
    expect(wrapper.text()).toContain('configured_ats')
    // Never checked is shown as never, not as a date the backend does not have.
    expect(wrapper.text()).toContain('checked never')
  })

  it('shows the normalized name identity resolution actually compares', async () => {
    const { wrapper } = await mountDetail()

    expect(wrapper.get('.co-facts').text()).toContain('logitech')
  })

  it('renders no provider metadata, because the API sends none', async () => {
    const { wrapper } = await mountDetail()

    expect(wrapper.html()).not.toMatch(/authorization|api[-_]?key|token|cookie|secret/i)
    expect(wrapper.html()).not.toContain('"raw"')
  })

  it('keeps an undecided spontaneous channel undecided', async () => {
    const { wrapper } = await mountDetail()

    expect(wrapper.get('.co-facts').text()).toContain('Unknown — nobody has checked')
  })

  it('names the provider that observed a supported channel', async () => {
    const { wrapper } = await mountDetail(companyDetail({
      company: company({
        spontaneous_application: {
          support: 'SUPPORTED',
          url: 'https://logitech.invalid/jobs/spontaneous',
          observed_by: 'configured_ats',
          evidence: [{
            code: 'SPONTANEOUS_URL_CONFIGURED',
            detail: 'the country pack lists a spontaneous application form',
            source_url: null,
            observed_at: null,
          }],
        },
        accepts_spontaneous_applications: true,
      }),
    }))

    expect(wrapper.get('.co-facts').text()).toContain('Supported')
    expect(wrapper.text()).toContain('Observed by configured_ats')
    expect(wrapper.text()).toContain('SPONTANEOUS_URL_CONFIGURED')
  })

  it('leaves the ATS section out entirely when no platform was detected', async () => {
    const { wrapper } = await mountDetail()

    expect(wrapper.text()).not.toContain('Applicant tracking system')
  })

  it('says the company is not in the directory on a 404', async () => {
    const { wrapper } = await mountDetail(companyDetail(), [{
      match: `/api/v2/companies/${COMPANY_ID}`,
      method: 'GET',
      status: 404,
      json: { error: 'company_not_found', detail: 'no company is stored under that id' },
    }])

    expect(wrapper.get('.ov-state').text()).toContain('That company is not in the directory')
    expect(wrapper.find('.ov-error').exists()).toBe(false)
  })
})
