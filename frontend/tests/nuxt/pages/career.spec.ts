// The career-intelligence screen: the read-only funnel report and the write-once
// recommendations below it.
//
// Asserted the way the spine's "measure" and "recommend" links are meant to read: an
// account with no matured applications is told there is nothing to measure rather than
// shown a funnel of zeros; a populated report shows its stages and conversion rates; a
// recommendation renders with the evidence it cites; and "Generate" appends a fresh set
// without ever being an "apply" — acting on advice is the separate strategy surface,
// which this page only points at.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import CareerPage from '~/pages/career.vue'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import {
  careerAnalytics,
  careerRecommendation,
  careerRecommendationList,
  signedIn,
} from '../support/v2-fixtures'
import type { Route } from '../support/http'
import type { CareerAnalytics, CareerRecommendationList } from '~/types/v2'

const ANALYTICS = '/api/v2/career/analytics'
const RECOMMENDATIONS = '/api/v2/career/recommendations'

interface CareerOpts {
  analytics?: CareerAnalytics
  recommendations?: CareerRecommendationList
  extra?: Route[]
}

function routes(opts: CareerOpts = {}): Route[] {
  return [
    ...(opts.extra ?? []),
    { match: ANALYTICS, method: 'GET', json: opts.analytics ?? careerAnalytics() },
    {
      match: RECOMMENDATIONS,
      method: 'GET',
      json: opts.recommendations ?? careerRecommendationList([]),
    },
    { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
  ]
}

async function mountWith(opts: CareerOpts = {}) {
  const http = stubFetch(routes(opts))
  await useSessionStore().ensure()
  const wrapper = await mountSuspended(CareerPage, { route: '/career' })
  await flushPromises()
  return { http, wrapper }
}

describe('career page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('tells an account with nothing matured there is nothing to measure', async () => {
    const emptyWindow = { earliest_applied_at: null, latest_applied_at: null, is_empty: true }
    const empty = careerAnalytics({
      window: emptyWindow,
      funnel: {
        window: emptyWindow,
        censoring: {
          as_of: '2026-03-20T09:00:00Z',
          observation_horizon_days: 30,
          mature_count: 0,
          censored_count: 0,
          total_count: 0,
        },
        stages: [],
      },
    })
    const { wrapper } = await mountWith({ analytics: empty })
    expect(wrapper.text()).toContain('No applications to measure yet')
  })

  it('renders the funnel and its conversion rates', async () => {
    const { wrapper } = await mountWith()
    expect(wrapper.get('[data-test="funnel"]').text().toLowerCase()).toContain('submitted')
    // The response rate the default funnel produces, 6/10.
    expect(wrapper.text()).toContain('60%')
  })

  it('lists evidence-backed recommendations', async () => {
    const { wrapper } = await mountWith({
      recommendations: careerRecommendationList([careerRecommendation()]),
    })
    const recs = wrapper.get('[data-test="recommendations"]')
    expect(recs.text()).toContain('Lean into software engineering roles')
  })

  it('generates a fresh set on demand without touching the report', async () => {
    const { http, wrapper } = await mountWith({
      extra: [{
        match: RECOMMENDATIONS,
        method: 'POST',
        json: careerRecommendationList([careerRecommendation()]),
      }],
    })
    await wrapper.get('[data-test="generate"]').trigger('click')
    await flushPromises()
    expect(http.callsTo(RECOMMENDATIONS).filter(c => c.method === 'POST')).toHaveLength(1)
  })

  it('points acting-on-advice at the strategy surface', async () => {
    const { wrapper } = await mountWith()
    const link = wrapper.findAll('a').find(a => a.attributes('href') === '/strategy')
    expect(link).toBeTruthy()
  })
})
