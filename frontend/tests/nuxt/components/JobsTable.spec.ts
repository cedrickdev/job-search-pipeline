// Ported from webapp/src/components/JobsTable.test.tsx.
//
// The added case is the track filter. It is client-side here as it was in V1 — the
// board and the table share one jobs cache entry, so pushing `track` into the
// query string would split that cache — and nothing in V1 covered it.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import JobsTable from '~/components/JobsTable.vue'
import { card } from '../support/fixtures'
import { stubFetch } from '../support/http'

const ROWS = {
  items: [
    card({ application_id: 1, job_id: 10, company: 'Alpha', title: 'Shift Lead' }),
    card({
      application_id: 2,
      job_id: 11,
      company: 'Beta',
      title: 'Sales',
      status: 'Applied',
      score: 70,
      phone_screen_pct: null,
    }),
  ],
}

describe('JobsTable', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('renders rows from the API', async () => {
    stubFetch([{ match: '/api/jobs', method: 'GET', json: ROWS }])
    const wrapper = await mountSuspended(JobsTable)
    await flushPromises()

    expect(wrapper.text()).toContain('Alpha')
    expect(wrapper.text()).toContain('Beta')
    expect(wrapper.text()).toContain('91')
    // The unscored screen-rate column falls back to an em dash rather than 0.
    expect(wrapper.findAll('tbody tr')[1]?.findAll('td')[4]?.text()).toBe('—')
  })

  it('emits open with the job id on row click', async () => {
    stubFetch([{ match: '/api/jobs', method: 'GET', json: ROWS }])
    const wrapper = await mountSuspended(JobsTable)
    await flushPromises()

    await wrapper.get('tbody tr').trigger('click')
    expect(wrapper.emitted('open')).toEqual([[10]])
  })

  it('filters by track client-side, keeping one shared jobs cache entry', async () => {
    const http = stubFetch([{
      match: '/api/jobs',
      method: 'GET',
      json: { items: [...ROWS.items, card({ application_id: 3, job_id: 12, company: 'Gamma', track: 'travail' })] },
    }])
    const wrapper = await mountSuspended(JobsTable, { props: { track: 'travail' } })
    await flushPromises()

    expect(wrapper.text()).toContain('Gamma')
    expect(wrapper.text()).not.toContain('Alpha')
    // `track` must not reach the server, or the board and table stop sharing a key.
    expect(http.callsTo('/api/jobs').every(c => !c.url.includes('track'))).toBe(true)
  })

  it('shows an empty state when nothing matches the filters', async () => {
    stubFetch([{ match: '/api/jobs', method: 'GET', json: { items: [] } }])
    const wrapper = await mountSuspended(JobsTable)
    await flushPromises()

    expect(wrapper.text()).toContain('No jobs match these filters.')
  })
})
