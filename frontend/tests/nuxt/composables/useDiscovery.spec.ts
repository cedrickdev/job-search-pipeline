import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { useDiscoveryRun } from '~/composables/useDiscovery'
import { discoveryMessage, sourceHealthMessage } from '~/utils/discovery-messages'
import { stubFetch } from '../support/http'

const ID = '33333333-3333-4333-8333-333333333333'
const RUN = '55555555-5555-4555-8555-555555555555'
const START = `/api/v2/me/search-profiles/${ID}/discovery-runs`
const STATUS = `/api/v2/me/discovery-runs/${RUN}`
const LATEST = `/api/v2/me/search-profiles/${ID}/discovery-runs/latest`

function holder() {
  return defineComponent({
    setup() {
      const discovery = useDiscoveryRun(() => ID)
      return () => h('div', [
        h('button', { onClick: discovery.refresh }, 'Refresh'),
        h('output', discovery.status.value),
        h('span', discovery.sourceHealth.value.length),
      ])
    },
  })
}

describe('Swiss discovery run', () => {
  beforeEach(() => { vi.restoreAllMocks(); clearNuxtData() })

  it('hydrates the last morning report after a new mount without starting a run', async () => {
    const http = stubFetch([{ match: LATEST, method: 'GET', json: {
      run_id: RUN, search_profile_id: ID, country: 'CH', trigger: 'MORNING',
      status: 'PARTIAL', started_at: '2026-10-04T09:00:00Z',
      finished_at: '2026-10-04T09:01:00Z', seen_count: 2, matched_count: 1,
      source_health: [{ source_key: 'swiss_board', status: 'UNAVAILABLE', reason: 'SOURCE_TIMEOUT' }],
      warning_codes: ['SOURCE_TIMEOUT'],
    } }])
    const wrapper = await mountSuspended(holder())
    await flushPromises()
    expect(wrapper.get('output').text()).toBe('PARTIAL')
    expect(wrapper.get('span').text()).toBe('1')
    expect(http.callsTo(START).filter(call => call.method === 'POST')).toHaveLength(0)
    wrapper.unmount()
  })

  it('queues with POST then reads a partial owner report without exposing raw metadata', async () => {
    const http = stubFetch([
      { match: LATEST, method: 'GET', status: 404 },
      { match: START, method: 'POST', status: 202, json: { run_id: RUN, status: 'QUEUED' } },
      { match: STATUS, method: 'GET', json: {
        run_id: RUN, search_profile_id: ID, country: 'CH', trigger: 'MANUAL',
        status: 'PARTIAL', started_at: '2026-10-04T09:00:00Z',
        finished_at: '2026-10-04T09:01:00Z', seen_count: 4, matched_count: 2,
        source_health: [{ source_key: 'swiss_board', status: 'UNAVAILABLE', reason: 'SOURCE_TIMEOUT' }],
        warning_codes: ['SOURCE_TIMEOUT'],
      } },
    ])
    const wrapper = await mountSuspended(holder())
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(http.callsTo(START).filter(call => call.method === 'POST')).toHaveLength(1)
    expect(http.callsTo(STATUS)).toHaveLength(1)
    expect(wrapper.get('output').text()).toBe('PARTIAL')
    expect(wrapper.get('span').text()).toBe('1')
    wrapper.unmount()
  })

  it('has explicit French and English partial-coverage wording', () => {
    expect(discoveryMessage('fr', 'PARTIAL')).toContain('incomplète')
    expect(discoveryMessage('en', 'PARTIAL')).toContain('incomplete')
    expect(sourceHealthMessage('fr', 'UNAVAILABLE', 'SOURCE_TIMEOUT')).toContain('délai')
    expect(sourceHealthMessage('en', 'UNAVAILABLE', 'SOURCE_TIMEOUT')).toContain('timed out')
  })

  it('keeps a queued run pending while its worker has not produced a report', async () => {
    const http = stubFetch([
      { match: LATEST, method: 'GET', status: 404 },
      { match: START, method: 'POST', status: 202, json: { run_id: RUN, status: 'QUEUED' } },
      { match: STATUS, method: 'GET', json: {
        run_id: RUN, search_profile_id: ID, country: 'CH', trigger: 'MANUAL',
        status: 'QUEUED', started_at: null, finished_at: null,
        seen_count: 0, matched_count: 0, source_health: [], warning_codes: [],
      } },
    ])
    const wrapper = await mountSuspended(holder())
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('output').text()).toBe('QUEUED')
    expect(http.callsTo(STATUS)).toHaveLength(1)
    wrapper.unmount()
  })

  it('reports a completed run and does not keep polling it', async () => {
    const http = stubFetch([
      { match: LATEST, method: 'GET', status: 404 },
      { match: START, method: 'POST', status: 202, json: { run_id: RUN, status: 'QUEUED' } },
      { match: STATUS, method: 'GET', json: {
        run_id: RUN, search_profile_id: ID, country: 'CH', trigger: 'MANUAL',
        status: 'SUCCEEDED', started_at: '2026-10-04T09:00:00Z',
        finished_at: '2026-10-04T09:01:00Z',
        seen_count: 1, matched_count: 1, source_health: [], warning_codes: [],
      } },
    ])
    const wrapper = await mountSuspended(holder())
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('output').text()).toBe('SUCCEEDED')
    expect(http.callsTo(STATUS)).toHaveLength(1)
    wrapper.unmount()
  })
})
