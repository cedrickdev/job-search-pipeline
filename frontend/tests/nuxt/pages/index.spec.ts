// Ported from webapp/src/routes/OverviewPage.test.tsx.
//
// The section-heading assertions are the point of this file: the overview is the
// command centre described in docs/V2_SPECIFICATION.md §220, and a migration that
// quietly dropped one of its nine blocks would look fine on screen and be a parity
// break. The KPI assertions pin the units, which differ per tile — readiness
// arrives as a percentage, response rate as a fraction, velocity as value/goal.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mockNuxtImport, mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import IndexPage from '~/pages/index.vue'
import { card, followup, overview } from '../support/fixtures'
import { stubFetch } from '../support/http'

// V1 asserted the navigation with a `useLocation` probe inside a MemoryRouter.
// The Nuxt equivalent is to intercept the auto-imported `navigateTo`: it pins the
// destination without depending on the test router's route table, and it keeps
// working when /jobs grows route middleware.
const navigateTo = vi.hoisted(() => vi.fn())
mockNuxtImport('navigateTo', () => navigateTo)

const OV = overview({
  kpis: {
    phone_screen_readiness: { value: 90, target: 90 },
    response_rate: 0.667,
    velocity: { value: 4, goal: 5 },
    in_flight: 12,
    replies_to_action: 3,
  },
  today: [card({
    application_id: 1,
    job_id: 101,
    company: 'Acme Corp',
    title: 'Senior Sales Assistant',
    score: 88,
    phone_screen_pct: 92,
  })],
  borderline: [card({
    application_id: 2,
    job_id: 102,
    company: 'Globex',
    title: 'Shift Lead',
    status: 'Borderline',
    score: 72,
    phone_screen_pct: 80,
  })],
  auto_approved_today: [],
  followups_due: [followup({ days: 5, since: '2026-06-10' })],
  replies_to_action: [card({
    application_id: 4,
    job_id: 104,
    company: 'Umbrella',
    title: 'Lead Sales',
    status: 'Recruiter reply',
    score: 91,
    phone_screen_pct: 95,
  })],
  upcoming_interviews: [{
    id: 7,
    job_id: 105,
    company: 'Soylent',
    title: 'Staff Sales',
    round_label: 'Round 1',
    scheduled_for: '2026-06-20 14:00',
  }],
  funnel: [
    { stage: 'Discovered', count: 40, pct: 1 },
    { stage: 'Applied', count: 12, pct: 0.3 },
    { stage: 'Interview', count: 2, pct: 0.05 },
  ],
  status_breakdown: [
    { status: 'Applied', count: 12 },
    { status: 'Recruiter reply', count: 3 },
  ],
  source_health: [{ source: 'LinkedIn', discovered: 30, applied: 8 }],
})

describe('overview page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    navigateTo.mockClear()
    clearNuxtData()
  })

  it('renders KPI tiles and every command-center section', async () => {
    stubFetch([{ match: '/api/overview', json: OV }])
    const wrapper = await mountSuspended(IndexPage)
    await flushPromises()
    const text = wrapper.text()

    // KPI units: readiness=percent, response_rate=fraction→%, velocity={value,goal}
    expect(text).toContain('Phone-screen readiness')
    expect(text).toContain('90%')
    expect(text).toContain('Response rate')
    expect(text).toContain('67%')
    expect(text).toContain('Velocity')
    expect(text).toContain('In flight')
    expect(text).toContain('Replies to action')

    // Section headings (spec §220 command-center)
    for (const heading of [
      'Today — do these first',
      'Borderline review',
      'Auto-approved today',
      'Recruiter replies',
      'Upcoming interviews',
      'Follow-ups due',
      'Funnel',
      'Pipeline by status',
      'Source health',
    ]) {
      expect(text).toContain(heading)
    }

    // Real rows + interview render
    expect(text).toContain('Acme Corp')
    expect(text).toContain('Soylent')

    // Empty section renders cleanly (auto_approved_today is empty)
    expect(text).toContain('Nothing here right now.')
  })

  it('navigates to /jobs when a job row is clicked', async () => {
    stubFetch([{ match: '/api/overview', json: OV }])
    const wrapper = await mountSuspended(IndexPage)
    await flushPromises()

    const row = wrapper.findAll('.job-row').find(r => r.text().includes('Acme Corp'))!
    await row.trigger('click')
    await flushPromises()

    expect(navigateTo).toHaveBeenCalledWith('/jobs')
  })

  it('sends every list — rows, replies and follow-ups — to the jobs page', async () => {
    // The overview deliberately owns no drawer: each of its three click surfaces
    // has to hand selection to /jobs, or one of them silently does nothing.
    stubFetch([{ match: '/api/overview', json: OV }])
    const wrapper = await mountSuspended(IndexPage)
    await flushPromises()

    const reply = wrapper.findAll('.job-row').find(r => r.text().includes('Umbrella'))!
    await reply.trigger('click')
    await wrapper.get('.fu-main').trigger('click')
    await flushPromises()

    expect(navigateTo.mock.calls).toEqual([['/jobs'], ['/jobs']])
  })

  it('shows an error state when the request fails', async () => {
    // V1 had no error-state test for this route; analytics had one, and the two
    // pages branch the same way, so the gap was an oversight rather than a decision.
    stubFetch([{ match: '/api/overview', status: 500, text: 'nope' }])
    const wrapper = await mountSuspended(IndexPage)
    await flushPromises()

    expect(wrapper.text()).toContain('Could not load overview.')
  })
})
