// Ported from webapp/src/routes/AnalyticsPage.test.tsx.
//
// The bar charts are plain divs here, as they were in V1 — ECharts is in the V2
// target stack but introducing it during a parity migration would change the
// rendering of every trend at the same time as the framework. So these assert on
// section headings, the KPI units and the two empty states, which is what V1
// asserted and what a chart swap would have to keep true.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import AnalyticsPage from '~/pages/analytics.vue'
import { stubFetch } from '../support/http'

const ANALYTICS = {
  days: 30,
  kpis: {
    phone_screen_readiness: { value: 88, target: 90 },
    response_rate: 0.4,
    velocity: { value: 6, goal: 5, window_days: 7 },
  },
  funnel: [
    { stage: 'Discovered', count: 40, pct: 1 },
    { stage: 'Applied', count: 12, pct: 0.3 },
  ],
  status_breakdown: [
    { status: 'Applied', count: 12 },
    { status: 'Recruiter reply', count: 3 },
  ],
  applications_per_day: [
    { date: '2026-06-14', count: 0 },
    { date: '2026-06-15', count: 3 },
    { date: '2026-06-16', count: 1 },
  ],
  replies_per_day: [
    { date: '2026-06-14', count: 0 },
    { date: '2026-06-15', count: 2 },
    { date: '2026-06-16', count: 0 },
  ],
  phone_screen_trend: {
    target: 90,
    points: [
      { date: '2026-06-14', value: 85, n: 1 },
      { date: '2026-06-15', value: 92, n: 2 },
    ],
  },
}

describe('analytics page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('renders KPI tiles and every trend section', async () => {
    stubFetch([{ match: '/api/analytics', json: ANALYTICS }])
    const wrapper = await mountSuspended(AnalyticsPage)
    await flushPromises()
    const text = wrapper.text()

    // KPIs reused from the snapshot
    expect(text).toContain('Phone-screen readiness')
    expect(text).toContain('88%')
    expect(text).toContain('Response rate')
    expect(text).toContain('40%')
    expect(text).toContain('Velocity')

    // Trend section headings
    for (const heading of [
      'Applications per day',
      'Recruiter replies per day',
      'Phone-screen readiness trend',
      'Funnel',
      'Pipeline by status',
    ]) {
      expect(text).toContain(heading)
    }
  })

  it('requests the default 30-day window', async () => {
    // The window is part of the cache key, so a wrong default is both a wrong
    // chart and a second cache entry.
    const http = stubFetch([{ match: '/api/analytics', json: ANALYTICS }])
    await mountSuspended(AnalyticsPage)
    await flushPromises()

    expect(http.callsTo('/api/analytics')[0]?.url).toBe('/api/analytics?days=30')
  })

  it('renders an empty-state for a trend with no data points', async () => {
    stubFetch([{
      match: '/api/analytics',
      json: { ...ANALYTICS, phone_screen_trend: { target: 90, points: [] } },
    }])
    const wrapper = await mountSuspended(AnalyticsPage)
    await flushPromises()

    expect(wrapper.text()).toContain('No scored CVs in this window.')
  })

  it('renders an empty-state for a day series with no activity', async () => {
    stubFetch([{ match: '/api/analytics', json: { ...ANALYTICS, applications_per_day: [] } }])
    const wrapper = await mountSuspended(AnalyticsPage)
    await flushPromises()

    expect(wrapper.text()).toContain('No activity in this window.')
  })

  it('shows an error state when the request fails', async () => {
    stubFetch([{ match: '/api/analytics', status: 500, text: 'nope' }])
    const wrapper = await mountSuspended(AnalyticsPage)
    await flushPromises()

    expect(wrapper.text()).toContain('Could not load analytics.')
  })
})
