// The application data layer, tested at the wire.
//
// Load-bearing and asserted from the request rather than the screen:
//   * the list is keyed under `applications`, the prefix every write invalidates;
//   * the five writes hit the right method and path — POST on the collection and on
//     each lifecycle sub-resource;
//   * the event trail is a lazy per-id query that does not fire until enabled.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, ref } from 'vue'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import {
  useApplicationActions,
  useApplicationEventsQuery,
  useApplicationsQuery,
} from '~/composables/useApplications'
import { keysMatching } from '~/composables/useApiQuery'
import { stubFetch } from '../support/http'
import { application, applicationEventList, applicationList } from '../support/v2-fixtures'

const APPLICATION_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const APPLICATIONS = '/api/v2/applications'

async function run<T>(composable: () => T): Promise<T> {
  let handle!: T
  await mountSuspended(defineComponent({
    setup() {
      handle = composable()
      return () => h('div')
    },
  }))
  return handle
}

describe('useApplicationsQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('reads the list and keys it under applications', async () => {
    const http = stubFetch([{ match: APPLICATIONS, json: applicationList() }])
    const q = await run(() => useApplicationsQuery())
    await flushPromises()

    expect(http.callsTo(APPLICATIONS)).toHaveLength(1)
    expect(q.data.value?.applications).toHaveLength(1)
    expect(keysMatching('applications')).not.toHaveLength(0)
  })
})

describe('useApplicationActions', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('opens an application with a POST to the collection', async () => {
    const http = stubFetch([{ match: APPLICATIONS, method: 'POST', json: application() }])
    const actions = await run(() => useApplicationActions())
    await actions.create.mutateAsync({ opportunity_id: '55555555-5555-4555-8555-555555555555' })

    const calls = http.callsTo(APPLICATIONS).filter(c => c.method === 'POST')
    expect(calls).toHaveLength(1)
  })

  it.each([
    ['prepare', 'prepare'],
    ['approve', 'approve'],
    ['submit', 'submit'],
    ['cancel', 'cancel'],
  ])('%s posts to the matching sub-resource', async (action, segment) => {
    const path = `${APPLICATIONS}/${APPLICATION_ID}/${segment}`
    const http = stubFetch([{ match: path, method: 'POST', json: application() }])
    const actions = await run(() => useApplicationActions())
    const byName = actions as unknown as Record<
      string, { mutateAsync: (id: string) => Promise<unknown> }>
    await byName[action]!.mutateAsync(APPLICATION_ID)

    expect(http.callsTo(path).filter(c => c.method === 'POST')).toHaveLength(1)
  })
})

describe('useApplicationEventsQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('does not fetch until enabled', async () => {
    const events = `${APPLICATIONS}/${APPLICATION_ID}/events`
    const http = stubFetch([{ match: events, json: applicationEventList() }])
    const enabled = ref(false)
    await run(() => useApplicationEventsQuery(APPLICATION_ID, { enabled }))
    await flushPromises()
    expect(http.callsTo(events)).toHaveLength(0)
  })

  it('fetches the trail for one id when enabled', async () => {
    const events = `${APPLICATIONS}/${APPLICATION_ID}/events`
    const http = stubFetch([{ match: events, json: applicationEventList() }])
    const q = await run(() => useApplicationEventsQuery(APPLICATION_ID, { enabled: ref(true) }))
    await flushPromises()
    expect(http.callsTo(events)).toHaveLength(1)
    expect(q.data.value?.events).toHaveLength(1)
  })
})
