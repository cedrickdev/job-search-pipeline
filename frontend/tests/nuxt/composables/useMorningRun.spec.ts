import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { isMorningRunActive, useMorningRun } from '~/composables/useMorningRun'
import type { MorningRunStatus } from '~/types/v2'
import { stubFetch } from '../support/http'
import { signedIn } from '../support/v2-fixtures'

const ID = '33333333-3333-4333-8333-333333333333'
const RUN = '55555555-5555-4555-8555-555555555555'
const SCHEDULE = '/api/v2/me/morning-schedule'
const RUNS = '/api/v2/me/morning-runs'
const LATEST = '/api/v2/me/morning-runs/latest'

function holder() {
  return defineComponent({
    setup() {
      const morning = useMorningRun()
      return () => h('div', [
        h('button', { onClick: () => void morning.runNow() }, 'Run V2'),
        h('button', { onClick: () => void morning.refreshRuns() }, 'Refresh status'),
        h('output', morning.runs.value.map(run => run.status).join(',')),
        h('span', morning.error.value ?? ''),
      ])
    },
  })
}

describe('V2 morning run client', () => {
  beforeEach(() => { vi.restoreAllMocks(); clearNuxtData() })

  it('keeps polling the browser handoff until a terminal application result', () => {
    const run = (state: string, task: string | null) => ({
      status: 'SUCCEEDED', preparations: [{ stage: 'DOCUMENTS_RENDERED',
        task_status: task, application_state: state }],
    }) as MorningRunStatus
    expect(isMorningRunActive(run('PREPARING', 'RUNNING'))).toBe(true)
    expect(isMorningRunActive(run('APPROVED', 'QUEUED'))).toBe(true)
    expect(isMorningRunActive(run('SUBMITTED', 'SUCCEEDED'))).toBe(false)
    expect(isMorningRunActive(run('SUBMISSION_STATE_UNKNOWN', 'SUCCEEDED'))).toBe(false)
  })

  it('posts only to the V2 endpoint and preserves the queued acknowledgement', async () => {
    const http = stubFetch([
      { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
      { match: SCHEDULE, method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
      { match: LATEST, method: 'GET', json: { runs: [] } },
      { match: RUNS, method: 'POST', status: 202, json: { runs: [{ run_id: RUN, search_profile_id: ID, status: 'QUEUED' }] } },
    ])
    const wrapper = await mountSuspended(holder())
    await flushPromises()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('output').text()).toBe('QUEUED')
    expect(http.callsTo('/api/v2/me/morning-runs').filter(call => call.method === 'POST')).toHaveLength(1)
    expect(http.callsTo('/api/runs/full')).toHaveLength(0)
    expect(http.unmatched).toEqual([])
    wrapper.unmount()
  })

  it('retains the previous result when a new launch fails', async () => {
    stubFetch([
      { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
      { match: SCHEDULE, method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
      { match: LATEST, method: 'GET', json: { runs: [{ run_id: RUN, search_profile_id: ID, status: 'PARTIAL', seen_count: 2, matched_count: 1, source_health: [], warning_codes: [] }] } },
      { match: RUNS, method: 'POST', status: 503, json: { error: 'unavailable', detail: 'worker offline' } },
    ])
    const wrapper = await mountSuspended(holder())
    await flushPromises()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('output').text()).toBe('PARTIAL')
    expect(wrapper.get('span').text()).toBe('worker offline')
    wrapper.unmount()
  })

  it('surfaces an unapproved-source refusal without claiming a queued run', async () => {
    stubFetch([
      { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
      { match: SCHEDULE, method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
      { match: LATEST, method: 'GET', json: { runs: [] } },
      { match: RUNS, method: 'POST', status: 409, json: {
        error: 'discovery_source_unapproved', detail: 'source access not approved',
      } },
    ])
    const wrapper = await mountSuspended(holder())
    await flushPromises()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('output').text()).toBe('')
    expect(wrapper.get('span').text()).toBe('discovery_source_unapproved')
    wrapper.unmount()
  })

  it('does not erase a queued acknowledgement when the latest read briefly lags', async () => {
    stubFetch([
      { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
      { match: SCHEDULE, method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
      { match: LATEST, method: 'GET', json: { runs: [] } },
      { match: RUNS, method: 'POST', status: 202, json: { runs: [{ run_id: RUN, search_profile_id: ID, status: 'QUEUED' }] } },
    ])
    const wrapper = await mountSuspended(holder())
    await flushPromises()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('output').text()).toBe('QUEUED')
    await wrapper.get('button:nth-of-type(2)').trigger('click')
    await flushPromises()
    expect(wrapper.get('output').text()).toBe('QUEUED')
    wrapper.unmount()
  })
})
